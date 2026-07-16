"""
news/rss_sources.py
-------------------
Centralised RSS source registry for all content pipelines.

Every pipeline that needs RSS feeds imports its source list from here.
The fetcher remains generic — it never hard-codes sources.

Source groups
~~~~~~~~~~~~~
    GOOGLE_NEWS_SOURCES  — Google News RSS (News pipeline)
    OFFICIAL_SOURCES     — NSE / BSE / SEBI direct feeds (Official Intelligence pipeline)

Adding a new pipeline (RBI, MCA, IRDAI …) only requires adding a new list here
and registering it in the scheduler — no other file needs to change.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class NewsSource:
    """Immutable descriptor for a single RSS feed."""
    name:     str
    tier:     int       # 1 = official regulator/exchange, 2 = media
    rss_url:  str
    category: str = "General"


# ── Google News RSS (News pipeline) ──────────────────────────────────────────
# fmt: off
GOOGLE_NEWS_SOURCES: list[NewsSource] = [
    # NewsSource("Google News Finance",      1, "https://news.google.com/rss/search?q=(India+OR+Indian)+(NSE+OR+BSE+OR+SEBI+OR+RBI+OR+stock+market+OR+finance+OR+economy)+when:1h&hl=en-IN&gl=IN&ceid=IN:en", "Indian Finance"),
    # NewsSource("Google News Stock Market", 1, "https://news.google.com/rss/search?q=(NSE+OR+BSE+OR+Sensex+OR+Nifty)+when:1h&hl=en-IN&gl=IN&ceid=IN:en", "Stock Market"),
    # NewsSource("Google News IPO",          1, "https://news.google.com/rss/search?q=IPO+India+when:1h&hl=en-IN&gl=IN&ceid=IN:en", "IPO"),
    # NewsSource("Google News SEBI",         1, "https://news.google.com/rss/search?q=SEBI+when:1h&hl=en-IN&gl=IN&ceid=IN:en", "SEBI"),
    # NewsSource("Google News RBI",          1, "https://news.google.com/rss/search?q=RBI+when:1h&hl=en-IN&gl=IN&ceid=IN:en", "RBI"),
    #NewsSource("Google News Quarterly Results", 1, "https://news.google.com/rss/search?q=%22quarterly+results%22+OR+Q1+OR+Q2+OR+Q3+OR+Q4+NSE+when:1d&hl=en-IN&gl=IN&ceid=IN:en", "Quarterly Results"),
    NewsSource("Google News Quarterly Results", 1, "https://news.google.com/rss/search?q=%22quarterly+results%22+OR+Q1+OR+Q2+OR+Q3+OR+Q4+NSE+when:1d&hl=en-IN&gl=IN&ceid=IN:en", "Quarterly Results"),
]

# ── Official Intelligence RSS (Official Intelligence pipeline) ────────────────
OFFICIAL_SOURCES: list[NewsSource] = [
    # NewsSource("NSE",  1, "https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml", "NSE"),
    # NewsSource("BSE",  1, "https://beta.bseindia.com/data/xml/notices.xml",                        "BSE"),
    # NewsSource("SEBI", 1, "https://www.sebi.gov.in/sebirss.xml",                                   "SEBI"),
]
# fmt: on

# Backward-compat alias (scheduler used SOURCES before rss_sources.py existed)
SOURCES = GOOGLE_NEWS_SOURCES
