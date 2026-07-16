"""
news/scheduler.py
-----------------
Single background scheduler — one unified pipeline for ALL sources.

Architecture
~~~~~~~~~~~~
    NewsScheduler
        │
        └── NewsPipeline   (Google News + NSE/BSE/SEBI RSS → two-stage AI → news_articles)
                Stage 1: Market Intelligence Evaluation (with Google Search grounding)
                Filter:  Score-based threshold filtering  (configurable thresholds)
                Stage 2: Premium Article Generation      (with Google Search grounding)

All sources — Google News RSS AND official exchange/regulator feeds (NSE, BSE, SEBI) —
are evaluated and generated through the SAME two-stage pipeline. There is no separate
OfficialPipeline. Every article, regardless of origin, ends up in the news_articles table.

Adding a future pipeline source:
    1. Add new NewsSource entries to rss_sources.py.
    2. Append the source list to ALL_SOURCES in this file.
    Nothing else changes.

Shared infrastructure
~~~~~~~~~~~~~~~~~~~~~
    • fetch_sources()   — generic RSS fetcher
    • generate_news()   — two-stage Gemini pipeline
    • ImageResolver     — same instance, same DB
    • NewsDB            — same SQLite connection
    • Retry logic       — exponential back-off in _run_cycle()
    • Logging           — structured, consistent prefixes
"""
from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone

from google import genai

from .config         import settings, get_fetch_interval_seconds, get_market_state, current_ist
from .db             import NewsDB
from .fetcher        import fetch_sources
from .generator      import (
    evaluate_raw_items,
    filter_evaluated_items,
    generate_articles_from_evaluated,
    generate_news,
)
from .image_resolver import ImageResolver
from .models         import NewsArticle, NewsClassification
from .rss_sources    import GOOGLE_NEWS_SOURCES, OFFICIAL_SOURCES

# Earnings pipeline imports
from .earnings.generator import generate_earnings_report

logger = logging.getLogger(__name__)

# All RSS sources — Google News + official exchange/regulator feeds — unified.
ALL_SOURCES = GOOGLE_NEWS_SOURCES + OFFICIAL_SOURCES


# ─── Pipeline abstraction ────────────────────────────────────────────────────

class BasePipeline(ABC):
    """Base class for a content pipeline."""

    @property
    @abstractmethod
    def name(self) -> str:
        ...

    @abstractmethod
    async def run(
        self,
        db:             NewsDB,
        gemini_client:  genai.Client,
        gemini_model:   str,
        image_resolver: ImageResolver,
        last_run_time:  str | None,
    ) -> int:
        ...


# ─── Unified News Pipeline ───────────────────────────────────────────────────

class NewsPipeline(BasePipeline):
    """
    Unified pipeline: ALL RSS sources → two-stage AI → news_articles.

    Sources: GOOGLE_NEWS_SOURCES + OFFICIAL_SOURCES (combined as ALL_SOURCES)

    Every item — whether from Google News, NSE, BSE, or SEBI — is evaluated
    by Stage 1 (Market Intelligence Evaluation), filtered by relevance score,
    and then written as a full NewsArticle by Stage 2 (Premium Article Generation).

    No separate pipeline or table exists for official sources.
    """

    def __init__(self) -> None:
        self._last_stage1_evaluated: int = 0
        self._last_stage1_passed:    int = 0
        self._last_stage2_generated: int = 0
        self._last_earnings_detected: int = 0
        self._last_earnings_generated: int = 0

    @property
    def name(self) -> str:
        return "news"

    @property
    def last_stats(self) -> dict[str, int]:
        return {
            "stage1_evaluated":    self._last_stage1_evaluated,
            "stage1_passed":       self._last_stage1_passed,
            "stage2_generated":    self._last_stage2_generated,
            "earnings_detected":   self._last_earnings_detected,
            "earnings_generated":  self._last_earnings_generated,
        }

    async def run(
        self,
        db:             NewsDB,
        gemini_client:  genai.Client,
        gemini_model:   str,
        image_resolver: ImageResolver,
        last_run_time:  str | None,
    ) -> int:
        # ── 1. Seen UIDs for dedup ────────────────────────────────────────
        existing_uids = await db.get_raw_existing_uids()
        logger.debug("[NewsPipeline] %d UIDs in dedup set", len(existing_uids))

        # ── 2. Fetch from ALL sources concurrently ────────────────────────
        raw_items = await fetch_sources(
            ALL_SOURCES, existing_uids=existing_uids
        )
        logger.info(
            "[NewsPipeline] Fetched %d new raw items from %d sources",
            len(raw_items), len(ALL_SOURCES),
        )

        if not raw_items:
            # Standalone fallback (no raw items)
            articles, stats, evaluated_all = await generate_news(
                client=gemini_client, model_name=gemini_model,
                raw_items=None, last_run_time=last_run_time,
            )
            self._last_stage1_evaluated = 0
            self._last_stage1_passed    = 0
            self._last_stage2_generated = stats.get("stage2_generated", 0)
            self._last_earnings_detected  = 0
            self._last_earnings_generated = 0
            if articles:
                await _resolve_images(articles, image_resolver)
                return await db.bulk_insert(articles)
            return 0

        # ── 3. Stage 1: Evaluate ALL items ────────────────────────────────
        evaluated_all = await evaluate_raw_items(
            client=gemini_client, model_name=gemini_model, raw_items=raw_items,
        )
        self._last_stage1_evaluated = len(evaluated_all)

        if evaluated_all:
            await db.bulk_insert_raw_items(evaluated_all)

        # ── 4. Split by classification ────────────────────────────────────
        regular_items = []
        earnings_items = []
        for item in evaluated_all:
            if item.evaluation.classification == NewsClassification.EARNINGS_RESULT.value:
                earnings_items.append(item)
            else:
                regular_items.append(item)

        self._last_earnings_detected = len(earnings_items)
        logger.info(
            "[NewsPipeline] Classification: regular=%d earnings=%d",
            len(regular_items), len(earnings_items),
        )

        total_inserted = 0

        # ── 5a. Regular News → existing pipeline (filter + Stage 2) ──────
        if regular_items:
            passed, filter_stats, _ = filter_evaluated_items(
                items=regular_items,
                high_threshold=settings.stage1_high_threshold,
                medium_threshold=settings.stage1_medium_threshold,
                generate_medium=settings.stage1_generate_medium,
            )
            self._last_stage1_passed = filter_stats["passed"]

            if passed:
                articles = await generate_articles_from_evaluated(
                    client=gemini_client, model_name=gemini_model,
                    evaluated_items=passed,
                )
                self._last_stage2_generated = len(articles)
                if articles:
                    await _resolve_images(articles, image_resolver)
                    total_inserted += await db.bulk_insert(articles)
            else:
                self._last_stage2_generated = 0
        else:
            self._last_stage1_passed    = 0
            self._last_stage2_generated = 0

        # ── 5b. Earnings → dedicated earnings pipeline ───────────────────
        earnings_generated = 0
        earnings_articles = []
        for ei in earnings_items:
            try:
                report_article = await generate_earnings_report(
                    client=gemini_client, model_name=gemini_model, item=ei,
                )
                if report_article:
                    earnings_articles.append(report_article)
            except Exception as exc:
                logger.exception(
                    "[NewsPipeline/Earnings] Failed for '%s': %s",
                    ei.raw.title[:60], exc,
                )
                
        if earnings_articles:
            earnings_generated = len(earnings_articles)
            self._last_earnings_generated = earnings_generated
            await _resolve_images(earnings_articles, image_resolver)
            total_inserted += await db.bulk_insert(earnings_articles)
        else:
            self._last_earnings_generated = 0

        logger.info(
            "[NewsPipeline] Stage1=%d | Regular: passed=%d generated=%d | Earnings: detected=%d generated=%d",
            self._last_stage1_evaluated, self._last_stage1_passed,
            self._last_stage2_generated, self._last_earnings_detected,
            self._last_earnings_generated,
        )

        return total_inserted + earnings_generated


# ─── Shared helpers ──────────────────────────────────────────────────────────


async def _resolve_images(records: list, image_resolver: ImageResolver) -> None:
    """Resolve images for a list of articles in parallel."""
    async def _resolve_one(rec) -> None:
        result = await image_resolver.resolve(getattr(rec, "image_query", None))
        if result:
            rec.image_url = result.image_url

    await asyncio.gather(*(_resolve_one(r) for r in records))


# ─── Scheduler ───────────────────────────────────────────────────────────────

class NewsScheduler:
    """
    Single scheduler that runs the unified NewsPipeline each cycle.

    Args:
        db:            Shared NewsDB instance.
        gemini_client: Shared genai.Client.
        gemini_model:  Model name string.
        pipelines:     Pipeline list. Defaults to [NewsPipeline()].
                       Pass a custom list only for testing/overrides.
    """

    def __init__(
        self,
        db:            NewsDB,
        gemini_client: genai.Client,
        gemini_model:  str,
        pipelines:     list[BasePipeline] | None = None,
    ) -> None:
        self._db             = db
        self._gemini_client  = gemini_client
        self._gemini_model   = gemini_model
        self._news_pipeline  = NewsPipeline()
        self._pipelines      = pipelines or [self._news_pipeline]
        self._task:          asyncio.Task | None = None
        self._running        = False
        self._next_run_at    = 0.0
        self._last_run_time: str | None = None
        self._last_fetch_at: str | None = None
        self._image_resolver = ImageResolver(self._db)

    # ── Public interface ─────────────────────────────────────────────────

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task    = asyncio.create_task(self._loop(), name="news_scheduler")
        logger.info(
            "[Scheduler] Started | sources=%d | pipelines=%s",
            len(ALL_SOURCES), self.pipeline_names,
        )

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

    @property
    def pipeline_names(self) -> list[str]:
        return [p.name for p in self._pipelines]

    def next_fetch_in_seconds(self) -> int | None:
        if not self.is_running:
            return None
        return max(0, int(self._next_run_at - time.time()))

    @property
    def last_fetch_at(self) -> str | None:
        return self._last_fetch_at

    @property
    def last_pipeline_stats(self) -> dict[str, int]:
        """Return Stage 1/2 stats from the most recent NewsPipeline run."""
        for p in self._pipelines:
            if isinstance(p, NewsPipeline):
                return p.last_stats
        return {}

    # ── Internal loop ────────────────────────────────────────────────────

    async def _loop(self) -> None:
        while self._running:
            state    = get_market_state()
            interval = get_fetch_interval_seconds(state)

            logger.info("[Scheduler] Poll cycle started (market=%s)", state.value)
            cycle_start = time.time()

            try:
                await self._run_cycle()
            except Exception as exc:
                logger.exception("[Scheduler] Poll cycle failed: %s", exc)

            # Prune expired articles
            try:
                await self._db.prune_expired()
            except Exception as exc:
                logger.exception("[Scheduler] Prune failed: %s", exc)

            cycle_elapsed     = time.time() - cycle_start
            self._next_run_at = time.time() + max(1, interval - int(cycle_elapsed))
            sleep_for         = max(1, int(self._next_run_at - time.time()))

            logger.info("[Scheduler] Cycle done in %.1fs, next in %ds", cycle_elapsed, sleep_for)
            try:
                await asyncio.sleep(sleep_for)
            except asyncio.CancelledError:
                break

    async def _run_cycle(self) -> None:
        """Run all pipelines with exponential back-off retry per pipeline."""
        max_retries = settings.max_retries

        for pipeline in self._pipelines:
            logger.info("[Scheduler] Running pipeline: %s", pipeline.name)
            for attempt in range(1, max_retries + 1):
                try:
                    inserted = await pipeline.run(
                        db             = self._db,
                        gemini_client  = self._gemini_client,
                        gemini_model   = self._gemini_model,
                        image_resolver = self._image_resolver,
                        last_run_time  = self._last_run_time,
                    )
                    logger.info(
                        "[Scheduler] Pipeline '%s' inserted %d records",
                        pipeline.name, inserted,
                    )
                    break
                except Exception as exc:
                    logger.warning(
                        "[Scheduler] Pipeline '%s' attempt %d/%d failed: %s",
                        pipeline.name, attempt, max_retries, exc,
                    )
                    if attempt < max_retries:
                        await asyncio.sleep(2 ** attempt)
                    else:
                        logger.error(
                            "[Scheduler] Pipeline '%s' gave up after %d attempts",
                            pipeline.name, max_retries,
                        )

        self._last_run_time = current_ist().isoformat()
        self._last_fetch_at = current_ist().strftime("%Y-%m-%d %H:%M:%S IST")

    # ── Compatibility shim ───────────────────────────────────────────────

    async def _do_fetch_and_store(self) -> None:
        """Public shim for the manual /refresh endpoint."""
        await self._run_cycle()
