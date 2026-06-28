"""
news/fetcher.py
---------------
Production-grade multi-source news fetcher.

Pipeline:
  1. Fetch RSS feeds from Tier-1 (SEBI, NSE, BSE, RBI, MCA) and
     Tier-2 (Moneycontrol, ET Markets, Mint, Business Standard, etc.)
  2. Parse and normalise each item into a RawNewsItem.
  3. Deduplicate by canonical URL before passing to the AI generator.

Design notes:
  • All HTTP calls are async via httpx with configurable timeouts.
  • Each source is fetched concurrently (asyncio.gather).
  • We parse RSS/Atom feeds using feedparser (sync, wrapped in executor).
  • Connection errors on individual sources are swallowed; others succeed.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

import httpx
import feedparser                       # pip install feedparser

from .config import settings, current_ist, IST

logger = logging.getLogger(__name__)

# ─── Source Registry ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class NewsSource:
    name:      str
    tier:      int           # 1 = official regulator/exchange, 2 = media
    rss_url:   str
    category:  str = "General"

# fmt: off
SOURCES: list[NewsSource] = [
    NewsSource("Google News Finance", 1, "https://news.google.com/rss/search?q=(India+OR+Indian)+(NSE+OR+BSE+OR+SEBI+OR+RBI+OR+stock+market+OR+finance+OR+economy)+when:1h&hl=en-IN&gl=IN&ceid=IN:en", "Indian Finance"),
    NewsSource("Google News Stock Market", 1, "https://news.google.com/rss/search?q=(NSE+OR+BSE+OR+Sensex+OR+Nifty)+when:1h&hl=en-IN&gl=IN&ceid=IN:en", "Stock Market"),
    NewsSource("Google News IPO", 1, "https://news.google.com/rss/search?q=IPO+India+when:1h&hl=en-IN&gl=IN&ceid=IN:en", "IPO"),
    NewsSource("Google News SEBI", 1, "https://news.google.com/rss/search?q=SEBI+when:1h&hl=en-IN&gl=IN&ceid=IN:en", "SEBI"),
    NewsSource("Google News RBI", 1, "https://news.google.com/rss/search?q=RBI+when:1h&hl=en-IN&gl=IN&ceid=IN:en", "RBI"),
]
# fmt: on


# ─── Raw Item ───────────────────────────────────────────────────────────────

@dataclass
class RawNewsItem:
    source_name:  str
    source_tier:  int
    title:        str
    url:          str
    summary:      str
    published_at: datetime          # UTC aware
    category:     str
    uid:          str = field(init=False)

    def __post_init__(self) -> None:
        # Canonical uid: sha256(normalised_url)
        normalised = self.url.strip().lower().rstrip("/")
        self.uid = hashlib.sha256(normalised.encode()).hexdigest()


# ─── Fetcher ────────────────────────────────────────────────────────────────

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; BasilstarNewsBot/2.0; "
        "+https://basilstar.app/bot)"
    ),
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}


def _parse_feed_sync(xml_text: str) -> feedparser.FeedParserDict:
    """Wraps feedparser (sync) — called via executor."""
    return feedparser.parse(xml_text)


def _clean_html(text: str) -> str:
    """Strip HTML tags from feed summaries."""
    return re.sub(r"<[^>]+>", "", text or "").strip()


def _parse_published(entry) -> datetime:
    """Best-effort published timestamp → UTC aware datetime."""
    try:
        if hasattr(entry, "published_parsed") and entry.published_parsed:
            return datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
    except Exception:
        pass
    return datetime.now(tz=timezone.utc)


async def _fetch_source(
    client: httpx.AsyncClient,
    loop: asyncio.AbstractEventLoop,
    source: NewsSource,
    since: datetime | None,
) -> list[RawNewsItem]:
    """Fetch one RSS source; returns [] on any error."""
    logger.info("[RSS] Fetching %s feeds", source.name)
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

        # Skip articles older than our since threshold
        if since and published < since:
            continue

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
                published_at = published,
                category     = source.category,
            )
        )

    logger.info("[RSS] Retrieved %d articles from %s", len(items), source.name)
    return items


async def fetch_all_sources(
    since: datetime | None = None,
    existing_uids: set[str] | None = None,
) -> list[RawNewsItem]:
    """
    Fetch all registered sources concurrently.

    Args:
        since:         Only return items published after this UTC datetime.
        existing_uids: Set of article UIDs already in DB (deduplicate).

    Returns:
        De-duplicated list of RawNewsItem, newest first.
    """
    loop = asyncio.get_event_loop()
    existing_uids = existing_uids or set()

    limits  = httpx.Limits(max_connections=20, max_keepalive_connections=10)
    timeout = httpx.Timeout(settings.request_timeout)

    async with httpx.AsyncClient(limits=limits, timeout=timeout) as client:
        tasks = [
            _fetch_source(client, loop, source, since)
            for source in SOURCES
        ]
        results = await asyncio.gather(*tasks, return_exceptions=False)

    # Flatten
    all_items: list[RawNewsItem] = []
    for batch in results:
        all_items.extend(batch)

    # Deduplicate by UID
    seen: set[str] = set(existing_uids)
    unique: list[RawNewsItem] = []
    for item in all_items:
        if item.uid not in seen:
            seen.add(item.uid)
            unique.append(item)

    # Sort: Tier-1 first, then newest first within tier
    unique.sort(key=lambda x: (x.source_tier, -x.published_at.timestamp()))

    total_raw   = sum(len(b) for b in results)
    logger.info(
        "[Dedup] Removed %d duplicates (kept %d)",
        total_raw - len(unique), len(unique)
    )
    return unique
