"""
financial_results/fetcher.py
----------------------------
Async RSS fetcher for NSE and BSE Financial Results feeds.

Uses dedicated financial results RSS feeds:
    • NSE: Integrated_Filing_Financials.xml
    • BSE: FinancialResultsFeed.xml

These feeds contain ONLY financial result announcements — no keyword
filtering is needed.  Each RSS item provides a company name, filing URL,
and publication date.

Public API
~~~~~~~~~~
    fetch_results_feeds(existing_uids)  → list[RawResultItem]
    download_filing(url)                → tuple[bytes, str] | None
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse

import httpx
import feedparser

try:
    from dateutil import parser as _du_parser
    _HAS_DATEUTIL = True
except ImportError:  # pragma: no cover
    _HAS_DATEUTIL = False

from .config import settings
from .utils  import make_uid

logger = logging.getLogger(__name__)


# ─── RSS Source definitions ──────────────────────────────────────────────────

@dataclass(frozen=True)
class ResultsSource:
    """Immutable descriptor for a financial results RSS feed."""
    name:     str
    exchange: str
    rss_url:  str


RESULTS_SOURCES: list[ResultsSource] = [
    ResultsSource(
        name="NSE Financial Results",
        exchange="NSE",
        rss_url="https://nsearchives.nseindia.com/content/RSS/Integrated_Filing_Financials.xml",
    ),
    ResultsSource(
        name="BSE Financial Results",
        exchange="BSE",
        rss_url="https://beta.bseindia.com/Data/XML/FinancialResultsFeed.xml",
    ),
]


# ─── Raw result item (fetcher output) ───────────────────────────────────────

@dataclass
class RawResultItem:
    """Normalised RSS item from a financial results feed."""
    company_name: str
    filing_url:   str
    published_at: str           # ISO datetime string (UTC)
    exchange:     str           # NSE | BSE
    source_name:  str
    uid:          str           # SHA-256 of filing URL
    scrip_code:   str | None = None  # BSE scrip code parsed from title e.g. "523676"
    summary:      str = ""           # RSS description if available


# ─── HTTP Headers ────────────────────────────────────────────────────────────

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/xml,text/xml,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}

# IST offset for naive timestamps from NSE
_IST = timezone(timedelta(hours=5, minutes=30))


# ─── Feed parsing helpers ────────────────────────────────────────────────────

def _parse_feed_sync(xml_text: str) -> feedparser.FeedParserDict:
    """Wrap feedparser (sync) — called via executor."""
    return feedparser.parse(xml_text)


# BSE RSS titles follow the pattern: "Company Name (ScripCode)"
# e.g. "Golkunda Diamonds & Jewellery Ltd (523676)"
_BSE_TITLE_RE = re.compile(r"^(.+?)\s*\((\d{4,6})\)\s*$")


def _parse_bse_title(title: str) -> tuple[str, str | None]:
    """Extract company name and scrip code from a BSE RSS title.

    Args:
        title: Raw title string from the BSE RSS feed.

    Returns:
        (company_name, scrip_code) — scrip_code is None when the
        title does not follow the expected "Name (Code)" pattern.
    """
    m = _BSE_TITLE_RE.match(title)
    if m:
        return m.group(1).strip(), m.group(2)
    return title, None


def _clean_html(text: str) -> str:
    """Strip HTML tags from feed text."""
    return re.sub(r"<[^>]+>", "", text or "").strip()


def _parse_published(entry) -> datetime:
    """Return a UTC-aware datetime for the entry's published timestamp.

    Strategy:
    1. feedparser's published_parsed (struct_time, always UTC).
    2. Raw string via python-dateutil.
    3. Manual strptime patterns.
    4. Fallback: now(UTC).
    """
    try:
        pp = getattr(entry, "published_parsed", None)
        if pp:
            return datetime(*pp[:6], tzinfo=timezone.utc)
    except Exception:
        pass

    raw: str = getattr(entry, "published", "") or ""
    if not raw:
        return datetime.now(tz=timezone.utc)

    if _HAS_DATEUTIL:
        try:
            dt = _du_parser.parse(raw, fuzzy=True)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_IST)
            return dt.astimezone(timezone.utc)
        except Exception:
            pass

    _PATTERNS = [
        ("%a, %d %b %Y %H:%M:%S %Z",  None),
        ("%a, %d %b %Y %H:%M:%S %z",  None),
        ("%d %b, %Y %z",               None),
        ("%d %b, %Y",                  _IST),
        ("%d-%b-%Y %H:%M:%S",          _IST),
        ("%d-%b-%Y",                   _IST),
        ("%Y-%m-%dT%H:%M:%S%z",        None),
        ("%Y-%m-%dT%H:%M:%S",          timezone.utc),
        ("%Y-%m-%d %H:%M:%S",          timezone.utc),
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

    logger.warning("[Results/RSS] Could not parse date %r — using now(UTC)", raw)
    return datetime.now(tz=timezone.utc)


# ─── Per-source fetch ────────────────────────────────────────────────────────

async def _fetch_source(
    client: httpx.AsyncClient,
    loop:   asyncio.AbstractEventLoop,
    source: ResultsSource,
) -> list[RawResultItem]:
    """Fetch one RSS source; returns [] on any error."""
    logger.info("[Results/RSS] Fetching %s", source.name)
    try:
        resp = await client.get(
            source.rss_url,
            headers=_HEADERS,
            timeout=settings.request_timeout,
            follow_redirects=True,
        )
        resp.raise_for_status()
    except Exception as exc:
        logger.exception("[Results/RSS] %s failed: %s", source.name, exc)
        return []

    try:
        feed = await loop.run_in_executor(None, _parse_feed_sync, resp.text)
    except Exception as exc:
        logger.exception("[Results/RSS] %s parse error: %s", source.name, exc)
        return []

    items: list[RawResultItem] = []
    for entry in feed.entries:
        url = entry.get("link") or entry.get("id") or ""
        if not url or not urlparse(url).scheme:
            continue

        published = _parse_published(entry)
        raw_title = _clean_html(entry.get("title", "")).strip()
        summary   = _clean_html(
            entry.get("summary") or entry.get("description") or ""
        )[:1000]

        if not raw_title:
            continue

        # For BSE feeds, parse "Company Name (ScripCode)" from the title so
        # that company_name is set correctly from the RSS (primary source) and
        # the scrip code is available for downstream reconciliation.
        if source.exchange == "BSE":
            company_name, scrip_code = _parse_bse_title(raw_title)
        else:
            company_name, scrip_code = raw_title, None

        items.append(
            RawResultItem(
                company_name = company_name,
                filing_url   = url,
                published_at = published.isoformat(),
                exchange     = source.exchange,
                source_name  = source.name,
                uid          = make_uid(url),
                scrip_code   = scrip_code,
                summary      = summary,
            )
        )

    logger.info("[Results/RSS] Retrieved %d items from %s", len(items), source.name)
    return items


# ─── Public fetch function ───────────────────────────────────────────────────

async def fetch_results_feeds(
    existing_uids: set[str] | None = None,
) -> list[RawResultItem]:
    """Fetch all financial results RSS feeds and deduplicate.

    Args:
        existing_uids: UIDs already processed; skipped for dedup.

    Returns:
        Deduplicated list of RawResultItem sorted by newest-published first.
    """
    loop          = asyncio.get_event_loop()
    existing_uids = existing_uids or set()

    limits  = httpx.Limits(max_connections=10, max_keepalive_connections=5)
    timeout = httpx.Timeout(settings.request_timeout)

    async with httpx.AsyncClient(limits=limits, timeout=timeout) as client:
        tasks   = [_fetch_source(client, loop, src) for src in RESULTS_SOURCES]
        results = await asyncio.gather(*tasks, return_exceptions=False)

    # Flatten
    all_items: list[RawResultItem] = []
    for batch in results:
        all_items.extend(batch)

    # Deduplicate by UID
    seen:   set[str]             = set(existing_uids)
    unique: list[RawResultItem]  = []
    for item in all_items:
        if item.uid not in seen:
            seen.add(item.uid)
            unique.append(item)

    # Sort: newest first
    unique.sort(key=lambda x: x.published_at or "", reverse=True)

    total_raw = sum(len(b) for b in results)
    logger.info(
        "[Results/Dedup] Removed %d duplicates (kept %d / %d raw)",
        total_raw - len(unique), len(unique), total_raw,
    )
    return unique


# ─── Filing document download ───────────────────────────────────────────────

async def download_filing(url: str) -> tuple[bytes, str] | None:
    """Download the filing document at the given URL.

    Returns:
        (content_bytes, doc_type) where doc_type is 'xml' or 'html'.
        None if the download fails after retries.
    """
    max_retries = settings.max_retries

    for attempt in range(1, max_retries + 1):
        try:
            async with httpx.AsyncClient(timeout=settings.request_timeout) as client:
                resp = await client.get(
                    url,
                    headers=_HEADERS,
                    follow_redirects=True,
                )
                resp.raise_for_status()

            content = resp.content
            content_type = resp.headers.get("content-type", "").lower()

            # Determine document type
            if "xml" in content_type or "xbrl" in content_type:
                doc_type = "xml"
            elif "html" in content_type:
                doc_type = "html"
            else:
                # Sniff content
                snippet = content[:500].decode("utf-8", errors="ignore").lower()
                if "<?xml" in snippet or "<xbrl" in snippet:
                    doc_type = "xml"
                elif "<html" in snippet or "<!doctype" in snippet:
                    doc_type = "html"
                else:
                    doc_type = "xml"  # default assumption for exchange filings

            logger.info(
                "[Results/Download] OK (%s, %d bytes): %s",
                doc_type, len(content), url[:120],
            )
            return content, doc_type

        except Exception as exc:
            logger.warning(
                "[Results/Download] Attempt %d/%d failed for %s: %s",
                attempt, max_retries, url[:120], exc,
            )
            if attempt < max_retries:
                await asyncio.sleep(2 ** attempt)

    logger.error("[Results/Download] Gave up after %d attempts: %s", max_retries, url[:120])
    return None
