"""
financial_results/scheduler.py
------------------------------
Background scheduler for the Financial Results analysis pipeline.

Architecture
~~~~~~~~~~~~
    ResultsScheduler
        │
        └── ResultsPipeline
                1. Fetch NSE/BSE Financial Results RSS
                2. Deduplicate against existing UIDs
                3. For each new item:
                   a. Download filing document (XML/XBRL or HTML)
                   b. Parse metadata from document
                   c. Analyze via Gemini with Google Search Grounding
                   d. Store result in SQLite
                4. Prune expired records

Two polling intervals:
    • DAY   (10:00–21:00 IST) — default 1 hour
    • NIGHT (21:00–10:00 IST) — default 2 hours
"""
from __future__ import annotations

import asyncio
import logging
import time

from google import genai

from .config    import settings, get_fetch_interval_seconds, get_schedule_state, current_ist
from .db        import ResultsDB
from .fetcher   import fetch_results_feeds, download_filing, RawResultItem
from .parser    import parse_filing
from .generator import analyze_financial_result

logger = logging.getLogger(__name__)


# ─── Pipeline ────────────────────────────────────────────────────────────────

class ResultsPipeline:
    """Financial Results analysis pipeline.

    For each new RSS item:
        1. Download the filing document.
        2. Parse metadata from the document.
        3. Send metadata to Gemini for AI analysis.
        4. Store the result.
    """

    def __init__(self) -> None:
        self._last_processed: int = 0

    @property
    def name(self) -> str:
        return "financial_results"

    @property
    def last_processed(self) -> int:
        return self._last_processed

    async def run(
        self,
        db:            ResultsDB,
        gemini_client: genai.Client,
        gemini_model:  str,
    ) -> int:
        """Execute one pipeline cycle.

        Fetches ALL new filings and processes them in batches of
        `settings.batch_size`.  Every item is processed regardless of
        whether earlier items succeed or fail.

        Dedup is two-layered:
            1. URL-level  — skip UIDs already in DB (cheap, pre-download).
            2. Business-level — skip items whose (company_name, period_end,
               standalone_consolidated) already exists, even if URL differs.
               This prevents processing the same result twice when NSE/BSE
               re-publishes with a different URL (common within minutes of
               the original announcement).

        Returns:
            Number of results successfully inserted.
        """
        # 1. Load existing UIDs and business keys for dedup
        existing_uids     = await db.get_existing_uids()
        existing_biz_keys = await db.get_existing_business_keys()
        logger.debug(
            "[ResultsPipeline] Dedup set: %d UIDs, %d business keys",
            len(existing_uids), len(existing_biz_keys),
        )

        # 2. Fetch and deduplicate RSS feeds (URL-level)
        raw_items = await fetch_results_feeds(existing_uids=existing_uids)
        total_fetched = len(raw_items)
        logger.info("[ResultsPipeline] Fetched: %d unique new items", total_fetched)

        if not raw_items:
            self._last_processed = 0
            return 0

        # 3. Split into batches and process ALL of them.
        batch_size   = settings.batch_size
        batches      = [raw_items[i:i + batch_size] for i in range(0, len(raw_items), batch_size)]
        total_batches = len(batches)

        inserted  = 0
        succeeded = 0
        failed    = 0
        skipped   = 0

        for batch_num, batch in enumerate(batches, start=1):
            logger.info(
                "[ResultsPipeline] Processing batch %d/%d (%d items)",
                batch_num, total_batches, len(batch),
            )
            for item in batch:
                try:
                    result = await self._process_item(
                        item, gemini_client, gemini_model, existing_biz_keys,
                    )
                    if result is None:
                        skipped += 1
                        succeeded += 1
                        continue
                    was_inserted = await db.insert_result(result)
                    if was_inserted:
                        inserted += 1
                        # Add the newly inserted key so subsequent items in the
                        # same batch are also blocked (in-memory update).
                        biz_key = (
                            result.company_name or "",
                            result.period_end   or "",
                            result.standalone_consolidated or "",
                        )
                        existing_biz_keys.add(biz_key)
                    succeeded += 1
                except Exception as exc:
                    failed += 1
                    logger.exception(
                        "[ResultsPipeline] Item failed (%s): %s",
                        item.company_name, exc,
                    )
                    # Continue — never abort remaining items due to one failure

        self._last_processed = inserted
        logger.info(
            "[ResultsPipeline] Processing complete. "
            "Processed: %d  Succeeded: %d  Skipped(biz-dedup): %d  Failed: %d  Inserted: %d",
            total_fetched, succeeded, skipped, failed, inserted,
        )
        return inserted

    async def _process_item(
        self,
        item:             RawResultItem,
        gemini_client:    genai.Client,
        gemini_model:     str,
        existing_biz_keys: set[tuple[str, str, str]] | None = None,
    ):
        """Process a single RSS item through the full pipeline.

        Returns None (without calling Gemini) if the item is a business-level
        duplicate — same company/period/standalone already in the DB.
        """
        logger.info(
            "[ResultsPipeline] Processing: %s (%s)",
            item.company_name, item.exchange,
        )

        # a. Download filing document
        download_result = await download_filing(item.filing_url)
        if download_result:
            content, doc_type = download_result
        else:
            content, doc_type = None, None
            logger.warning(
                "[ResultsPipeline] Filing download failed for %s — proceeding with RSS metadata only",
                item.company_name,
            )

        # b. Parse metadata — pass the full RSS context including scrip_code
        #    so the parser can treat RSS as the primary identity source.
        rss_meta = {
            "company_name": item.company_name,
            "scrip_code":   item.scrip_code,   # None for NSE; str for BSE
            "filing_url":   item.filing_url,
            "published_at": item.published_at,
            "exchange":     item.exchange,
            "uid":          item.uid,
        }
        metadata = parse_filing(content, doc_type, rss_meta)

        # c. Business-level dedup: skip if (company, period_end, standalone/consolidated)
        #    already exists in DB — prevents duplicate analysis when NSE/BSE
        #    re-publishes the same result with a slightly different URL.
        if existing_biz_keys and metadata.period_end:
            biz_key = (
                metadata.company_name or "",
                metadata.period_end   or "",
                (metadata.standalone_consolidated or "").strip(),
            )
            if biz_key in existing_biz_keys:
                logger.info(
                    "[ResultsPipeline] Business-dedup skip: %s | period_end=%s | %s",
                    metadata.company_name, metadata.period_end,
                    metadata.standalone_consolidated or "unknown",
                )
                return None

        # d. Gemini analysis
        record = await analyze_financial_result(
            client     = gemini_client,
            model_name = gemini_model,
            metadata   = metadata,
        )

        return record


# ─── Scheduler ───────────────────────────────────────────────────────────────

class ResultsScheduler:
    """Background scheduler for the Financial Results pipeline.

    Two polling intervals (adjustable via env vars):
        DAY   (10:00–21:00 IST) — RESULTS_INTERVAL_DAY   (default 3600s)
        NIGHT (21:00–10:00 IST) — RESULTS_INTERVAL_NIGHT  (default 7200s)
    """

    def __init__(
        self,
        db:            ResultsDB,
        gemini_client: genai.Client,
        gemini_model:  str,
    ) -> None:
        self._db             = db
        self._gemini_client  = gemini_client
        self._gemini_model   = gemini_model
        self._pipeline       = ResultsPipeline()
        self._task:          asyncio.Task | None = None
        self._running        = False
        self._next_run_at    = 0.0
        self._last_fetch_at: str | None = None

    # ── Public interface ─────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the background scheduler loop."""
        if self._running:
            return
        self._running = True
        self._task    = asyncio.create_task(self._loop(), name="results_scheduler")
        logger.info("[Results/Scheduler] Started")

    async def stop(self) -> None:
        """Gracefully stop the scheduler."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("[Results/Scheduler] Stopped")

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

    @property
    def last_results_processed(self) -> int | None:
        return self._pipeline.last_processed

    # ── Internal loop ────────────────────────────────────────────────────

    async def _loop(self) -> None:
        """Main scheduler loop."""
        while self._running:
            state    = get_schedule_state()
            interval = get_fetch_interval_seconds(state)

            logger.info("[Results/Scheduler] Poll cycle started (state=%s)", state.value)
            cycle_start = time.time()

            try:
                await self._run_cycle()
            except Exception as exc:
                logger.exception("[Results/Scheduler] Poll cycle failed: %s", exc)

            # Prune expired
            try:
                await self._db.prune_expired()
            except Exception as exc:
                logger.exception("[Results/Scheduler] Prune failed: %s", exc)

            cycle_elapsed     = time.time() - cycle_start
            self._next_run_at = time.time() + max(1, interval - int(cycle_elapsed))
            sleep_for         = max(1, int(self._next_run_at - time.time()))

            logger.info(
                "[Results/Scheduler] Cycle done in %.1fs, next in %ds",
                cycle_elapsed, sleep_for,
            )
            try:
                await asyncio.sleep(sleep_for)
            except asyncio.CancelledError:
                break

    async def _run_cycle(self) -> None:
        """Run the pipeline with exponential back-off retry."""
        max_retries = settings.max_retries

        for attempt in range(1, max_retries + 1):
            try:
                inserted = await self._pipeline.run(
                    db            = self._db,
                    gemini_client = self._gemini_client,
                    gemini_model  = self._gemini_model,
                )
                logger.info(
                    "[Results/Scheduler] Pipeline inserted %d results", inserted,
                )
                break
            except Exception as exc:
                logger.warning(
                    "[Results/Scheduler] Attempt %d/%d failed: %s",
                    attempt, max_retries, exc,
                )
                if attempt < max_retries:
                    await asyncio.sleep(2 ** attempt)
                else:
                    logger.error(
                        "[Results/Scheduler] Gave up after %d attempts",
                        max_retries,
                    )

        self._last_fetch_at = current_ist().strftime("%Y-%m-%d %H:%M:%S IST")

    async def _do_fetch_and_store(self) -> None:
        """Public shim for the manual /refresh endpoint."""
        await self._run_cycle()
