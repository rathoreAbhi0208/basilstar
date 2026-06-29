"""
news/scheduler.py
-----------------
Background task scheduler for the News pipeline.

Each cycle:
  1. Fetch fresh articles from all RSS sources (fetcher.py).
  2. Enrich via Gemini AI (generator.py).
  3. Resolve images for each article (image_resolver.py).
  4. Persist to SQLite, skipping duplicates (db.py).
  5. Prune articles older than 24 h.
  6. Sleep until next interval (5 min market open, 15 min after, 30 min night).

Circuit breaker: exponential back-off with max_retries before giving up a cycle.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone, timedelta

from google import genai

from .config   import (
    settings, MarketState,
    get_fetch_interval_seconds, get_market_state, current_ist,
)
from .db               import NewsDB
from .fetcher          import fetch_all_sources
from .generator        import generate_news

logger = logging.getLogger(__name__)


class NewsScheduler:
    def __init__(
        self,
        db:            NewsDB,
        gemini_client: genai.Client,
        gemini_model:  str,
    ) -> None:
        self._db            = db
        self._gemini_client = gemini_client
        self._gemini_model  = gemini_model
        self._task: asyncio.Task | None = None
        self._running       = False
        self._next_run_at   = 0.0
        self._last_run_time: str | None = None
        self._last_fetch_at: str | None = None

    # ── Public interface ─────────────────────────────────────────────────

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        # Kick off immediately on startup
        self._task = asyncio.create_task(self._loop(), name="news_scheduler")
        logger.info("[Scheduler] Started")

    async def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("[Scheduler] Stopped")

    @property
    def is_running(self) -> bool:
        return self._running and self._task is not None and not self._task.done()

    def next_fetch_in_seconds(self) -> int | None:
        if not self.is_running:
            return None
        return max(0, int(self._next_run_at - time.time()))

    @property
    def last_fetch_at(self) -> str | None:
        return self._last_fetch_at

    # ── Internal loop ────────────────────────────────────────────────────

    async def _loop(self) -> None:
        while self._running:
            state    = get_market_state()
            interval = get_fetch_interval_seconds(state)

            logger.info("[Scheduler] Poll cycle started")
            cycle_start = time.time()

            try:
                await self._run_cycle()
            except Exception as exc:
                logger.exception("[Scheduler] Poll cycle failed: %s", exc)

            try:
                await self._db.prune_expired()
            except Exception as exc:
                logger.exception("[Scheduler] Prune failed: %s", exc)

            cycle_elapsed = time.time() - cycle_start
            self._next_run_at = time.time() + max(1, interval - int(cycle_elapsed))
            sleep_for = max(1, int(self._next_run_at - time.time()))

            logger.info("[Scheduler] Poll cycle completed in %.1fs", cycle_elapsed)
            logger.info("[Scheduler] Next poll in %d seconds", sleep_for)

            try:
                await asyncio.sleep(sleep_for)
            except asyncio.CancelledError:
                break

    async def _run_cycle(self) -> None:
        """One complete fetch → enrich → image → persist cycle."""
        max_retries = settings.max_retries

        for attempt in range(1, max_retries + 1):
            try:
                await self._do_fetch_and_store()
                return
            except Exception as exc:
                logger.warning(
                    "[Scheduler] Attempt %d/%d failed: %s",
                    attempt, max_retries, exc,
                )
                if attempt < max_retries:
                    await asyncio.sleep(2 ** attempt)
                else:
                    logger.error("[Scheduler] All %d attempts failed", max_retries)

    async def _do_fetch_and_store(self) -> None:
        """Core work: fetch → generate → image → save."""

        # ── 1. Get existing UIDs (for dedup) ─────────────────────────────
        existing_uids = await self._db.get_existing_uids()

        # ── 2. Compute "since" window ─────────────────────────────────────
        since = None
        if self._last_run_time:
            try:
                since = datetime.fromisoformat(self._last_run_time).replace(
                    tzinfo=timezone.utc
                )
                # small buffer: go back 5 extra minutes to avoid missing items
                since = since - timedelta(minutes=5)
            except Exception:
                since = None

        # ── 3. Fetch from RSS sources ─────────────────────────────────────
        raw_items = await fetch_all_sources(
            since         = since,
            existing_uids = existing_uids,
        )

        # ── 4. Generate enriched articles via Gemini ──────────────────────
        articles = await generate_news(
            client        = self._gemini_client,
            model_name    = self._gemini_model,
            raw_items     = raw_items if raw_items else None,
            last_run_time = self._last_run_time,
        )

        if not articles:
            self._last_run_time = current_ist().isoformat()
            return

        # ── 5. Persist ────────────────────────────────────────────────────
        inserted = await self._db.bulk_insert(articles)

        # ── 6. Update timestamps ──────────────────────────────────────────
        self._last_run_time = current_ist().isoformat()
        self._last_fetch_at = current_ist().strftime("%Y-%m-%d %H:%M:%S IST")
