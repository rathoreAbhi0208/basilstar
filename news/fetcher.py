"""
news/fetcher.py
---------------
Generic multi-source RSS fetcher used by ALL content pipelines.

There is exactly ONE RSS parser in this module.
Pipelines select their feeds by passing different source lists — they never
create their own fetcher.

Public API
~~~~~~~~~~
    fetch_sources(sources, since, existing_uids)
        Generic entry point.  Pass any list[NewsSource].

    fetch_all_sources(since, existing_uids)
        Convenience wrapper — passes GOOGLE_NEWS_SOURCES (News pipeline).

Design notes
~~~~~~~~~~~~
  • All HTTP calls are async (httpx) with configurable timeouts.
  • Each source is fetched concurrently (asyncio.gather).
  • feedparser (sync) is wrapped in an executor so it never blocks the loop.
  • Connection errors on individual sources are swallowed; others still succeed.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse

import httpx
import feedparser
try:
    from dateutil import parser as _du_parser
    _HAS_DATEUTIL = True
except ImportError:  # pragma: no cover
    _HAS_DATEUTIL = False

from .config      import settings, current_ist, IST
from .models      import RawNewsItem
from .rss_sources import NewsSource, GOOGLE_NEWS_SOURCES, OFFICIAL_SOURCES, SOURCES

logger = logging.getLogger(__name__)

# ─── HTTP Headers ────────────────────────────────────────────────────────────

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/xml,text/xml,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}


# ─── Feed parsing helpers ────────────────────────────────────────────────────

def _parse_feed_sync(xml_text: str) -> feedparser.FeedParserDict:
    """Wrap feedparser (sync) — called via executor."""
    return feedparser.parse(xml_text)


def _clean_html(text: str) -> str:
    """Strip HTML tags from feed summaries."""
    return re.sub(r"<[^>]+>", "", text or "").strip()


# IST offset used as default when a timestamp carries no timezone info
# (e.g. NSE: "03-Jul-2026 11:20:57").
_IST = timezone(timedelta(hours=5, minutes=30))


def _parse_published(entry) -> datetime:
    """Return a UTC-aware datetime for the entry's published timestamp.

    Strategy (in order):
    1. feedparser's ``published_parsed`` (time.struct_time, always UTC)
       — works for standard RFC-2822 feeds (Google News, BSE).
    2. Raw ``published`` string parsed with python-dateutil (fuzzy=True)
       — handles non-standard formats like SEBI ("02 Jul, 2026 +0530")
         and NSE ("03-Jul-2026 11:20:57").
    3. If dateutil is unavailable or fails, try a list of known strptime
       patterns as a final manual fallback.
    4. Last resort: return datetime.now(UTC) so the pipeline never crashes.
    """
    # ── Strategy 1: feedparser struct_time (UTC) ─────────────────────────
    try:
        pp = getattr(entry, "published_parsed", None)
        if pp:
            return datetime(*pp[:6], tzinfo=timezone.utc)
    except Exception:
        pass

    raw: str = getattr(entry, "published", "") or ""
    if not raw:
        return datetime.now(tz=timezone.utc)

    # ── Strategy 2: dateutil fuzzy parser ───────────────────────────────
    if _HAS_DATEUTIL:
        try:
            dt = _du_parser.parse(raw, fuzzy=True)
            # Timestamps with no tz info come from NSE; assume IST.
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_IST)
            return dt.astimezone(timezone.utc)
        except Exception:
            pass

    # ── Strategy 3: known strptime patterns (no-dateutil fallback) ───────
    _PATTERNS = [
        ("%a, %d %b %Y %H:%M:%S %Z",  None),          # RFC-2822 GMT/UTC
        ("%a, %d %b %Y %H:%M:%S %z",  None),          # RFC-2822 +offset
        ("%d %b, %Y %z",              None),           # SEBI: "02 Jul, 2026 +0530"
        ("%d %b, %Y",                 _IST),           # SEBI no-time
        ("%d-%b-%Y %H:%M:%S",         _IST),           # NSE:  "03-Jul-2026 11:20:57"
        ("%d-%b-%Y",                  _IST),           # NSE date-only
        ("%Y-%m-%dT%H:%M:%S%z",       None),           # ISO-8601 with tz
        ("%Y-%m-%dT%H:%M:%S",         timezone.utc),  # ISO-8601 no tz
        ("%Y-%m-%d %H:%M:%S",         timezone.utc),  # common SQL-style
    ]
    raw_stripped = raw.strip()
    for fmt, fallback_tz in _PATTERNS:
        try:
            dt = datetime.strptime(raw_stripped, fmt)
            if dt.tzinfo is None and fallback_tz is not None:
                dt = dt.replace(tzinfo=fallback_tz)
            return dt.astimezone(timezone.utc)
        except ValueError:
            continue

    logger.warning("[RSS] Could not parse date %r — using now(UTC)", raw)
    return datetime.now(tz=timezone.utc)


def _make_uid(url: str) -> str:
    """Stable, collision-resistant UID: sha256 of normalised URL."""
    normalised = url.strip().lower().rstrip("/")
    return hashlib.sha256(normalised.encode()).hexdigest()


# ─── Per-source fetch ────────────────────────────────────────────────────────

async def _fetch_source(
    client: httpx.AsyncClient,
    loop:   asyncio.AbstractEventLoop,
    source: NewsSource,
) -> list[RawNewsItem]:
    """Fetch one RSS source; returns [] on any error.

    NOTE: We do NOT filter by publication timestamp here. Official feeds
    (NSE, SEBI) frequently publish entries with stale or static `pubDate`
    values — a time-based filter would silently discard every item after the
    first run.  Deduplication is handled exclusively by UID (SHA-256 of URL)
    in `fetch_sources`, with the seen-UID set sourced from `raw_news_items`.
    """
    logger.info("[RSS] Fetching %s", source.name)
    try:
        resp = await client.get(
            source.rss_url,
            headers=_HEADERS,
            timeout=settings.request_timeout,
            follow_redirects=True,
        )
        resp.raise_for_status()
    except Exception as exc:
        logger.exception("[RSS] %s failed: %s", source.name, exc)
        return []

    try:
        feed = await loop.run_in_executor(None, _parse_feed_sync, resp.text)
    except Exception as exc:
        logger.exception("[RSS] %s parse error: %s", source.name, exc)
        return []

    items: list[RawNewsItem] = []
    for entry in feed.entries:
        url = entry.get("link") or entry.get("id") or ""
        if not url or not urlparse(url).scheme:
            continue

        published = _parse_published(entry)
        title   = _clean_html(entry.get("title", "")).strip()
        summary = _clean_html(
            entry.get("summary") or entry.get("description") or ""
        )[:1000]

        if not title:
            continue

        items.append(
            RawNewsItem(
                source_name  = source.name,
                source_tier  = source.tier,
                title        = title,
                url          = url,
                summary      = summary,
                published_at = published.isoformat(),
                category     = source.category,
                uid          = _make_uid(url),
            )
        )

    logger.info("[RSS] Retrieved %d items from %s", len(items), source.name)
    return items


# ─── Public fetch functions ──────────────────────────────────────────────────

async def fetch_sources(
    sources:       list[NewsSource],
    since:         datetime | None = None,   # kept for API compat; no longer used
    existing_uids: set[str] | None = None,
) -> list[RawNewsItem]:
    """
    Generic fetcher — the single RSS parser used by every pipeline.

    De-duplicates by URL-derived UID against ``existing_uids``.
    The ``since`` parameter is retained for backward-compatibility but is no
    longer applied as a time filter; UID-based dedup is the sole guard against
    re-processing seen items.

    Args:
        sources:       Any list of NewsSource objects.
        since:         Ignored. Kept only so existing call sites don't break.
        existing_uids: UIDs already seen (e.g. from raw_news_items); skipped.

    Returns:
        De-duplicated list of RawNewsItem sorted by (tier ASC, newest-published first).
    """
    loop          = asyncio.get_event_loop()
    existing_uids = existing_uids or set()

    limits  = httpx.Limits(max_connections=20, max_keepalive_connections=10)
    timeout = httpx.Timeout(settings.request_timeout)

    async with httpx.AsyncClient(limits=limits, timeout=timeout) as client:
        tasks   = [_fetch_source(client, loop, src) for src in sources]
        results = await asyncio.gather(*tasks, return_exceptions=False)

    # Flatten
    all_items: list[RawNewsItem] = []
    for batch in results:
        all_items.extend(batch)

    # Deduplicate by UID (URL-hash) against already-processed items
    seen:   set[str]          = set(existing_uids)
    unique: list[RawNewsItem] = []
    for item in all_items:
        if item.uid not in seen:
            seen.add(item.uid)
            unique.append(item)

    # Sort: Tier-1 first, then newest-published first within tier
    unique.sort(key=lambda x: (x.source_tier, x.published_at or ""))

    total_raw = sum(len(b) for b in results)
    logger.info(
        "[Dedup] Removed %d duplicates (kept %d / %d raw)",
        total_raw - len(unique), len(unique), total_raw,
    )
    return unique


async def fetch_all_sources(
    since:         datetime | None = None,
    existing_uids: set[str] | None = None,
) -> list[RawNewsItem]:
    """
    Convenience wrapper — fetches the News pipeline (GOOGLE_NEWS_SOURCES).

    Existing callers in the scheduler are unaffected by this abstraction.
    """
    return await fetch_sources(
        GOOGLE_NEWS_SOURCES, since=since, existing_uids=existing_uids
    )
