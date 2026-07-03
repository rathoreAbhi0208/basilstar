"""
news/config.py
--------------
Central configuration for the News package.
All tunable knobs live here — no magic strings scattered across files.

Two-stage AI pipeline settings
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
  • STAGE 1 thresholds control which items advance to Stage 2 article generation.
  • Both stages share the same Gemini client but may use different model configs.
"""
from __future__ import annotations

import os
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
dotenv_path = ROOT / ".env"
load_dotenv(dotenv_path, override=True)

from datetime import datetime, time
from enum import Enum
from zoneinfo import ZoneInfo

# ─── Timezone ────────────────────────────────────────────────────────────────
IST = ZoneInfo("Asia/Kolkata")

# ─── Market Window ───────────────────────────────────────────────────────────
_MARKET_OPEN  = time(9,  0)
_MARKET_CLOSE = time(15, 30)
_EVENING_END  = time(20, 0)

# ─── Fetch Intervals (seconds) ───────────────────────────────────────────────
INTERVAL_MARKET_OPEN   = int(os.getenv("NEWS_INTERVAL_OPEN",    "900"))   # 15 min
INTERVAL_MARKET_CLOSED = int(os.getenv("NEWS_INTERVAL_CLOSED",  "1800"))   # 30 min
INTERVAL_NIGHT         = int(os.getenv("NEWS_INTERVAL_NIGHT",  "3600"))   # 1 hour


class MarketState(str, Enum):
    OPEN   = "OPEN"
    CLOSED = "CLOSED"
    NIGHT  = "NIGHT"


def get_market_state(now: datetime | None = None) -> MarketState:
    if now is None:
        now = datetime.now(tz=IST)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=IST)

    t       = now.time()
    weekday = now.weekday()          # Mon=0 … Sun=6

    if weekday >= 5:                 # Weekend
        return MarketState.NIGHT

    if _MARKET_OPEN <= t < _MARKET_CLOSE:
        return MarketState.OPEN

    if _MARKET_CLOSE <= t < _EVENING_END:
        return MarketState.CLOSED

    return MarketState.NIGHT


def get_fetch_interval_seconds(state: MarketState | None = None) -> int:
    if state is None:
        state = get_market_state()
    return {
        MarketState.OPEN  : INTERVAL_MARKET_OPEN,
        MarketState.CLOSED: INTERVAL_MARKET_CLOSED,
        MarketState.NIGHT : INTERVAL_NIGHT,
    }[state]


def current_ist() -> datetime:
    return datetime.now(tz=IST)


# ─── News Settings ───────────────────────────────────────────────────────────
class NewsConfig:
    # ── Gemini ───────────────────────────────────────────────────────────────
    gemini_api_key: str   = os.getenv("GEMINI_API_KEY", "")
    gemini_model:   str   = os.getenv("GEMINI_MODEL",   "gemini-2.5-flash")
    temperature:    float = float(os.getenv("NEWS_TEMPERATURE", "0.0"))
    top_p:          float = float(os.getenv("NEWS_TOP_P",       "0.95"))
    max_retries:    int   = int(os.getenv("NEWS_MAX_RETRIES",   "3"))

    # ── Database ─────────────────────────────────────────────────────────────
    db_path: str = os.getenv("NEWS_DB_PATH", "news.db")

    # ── Retention ────────────────────────────────────────────────────────────
    retention_hours: int = int(os.getenv("NEWS_RETENTION_HOURS", "24"))

    # ── Image search (legacy, kept for compatibility) ─────────────────────────
    google_api_key: str = os.getenv("GOOGLE_API_KEY", "")
    google_cx:      str = os.getenv("GOOGLE_CX",      "")

    # ── Fetch pipeline ───────────────────────────────────────────────────────
    articles_per_cycle: int = int(os.getenv("NEWS_ARTICLES_PER_CYCLE", "15"))
    request_timeout:    int = int(os.getenv("NEWS_REQUEST_TIMEOUT",    "30"))

    # ── Image Providers Configuration ────────────────────────────────────────
    image_provider_priority: list[str] = [
        p.strip().lower()
        for p in os.getenv("IMAGE_PROVIDER_PRIORITY", "wikimedia,pexels,unsplash,pixabay").split(",")
        if p.strip()
    ]

    pexels_api_key:  str = os.getenv("PEXELS_API_KEY", "")
    pexels_base_url: str = os.getenv("PEXELS_BASE_URL", "https://api.pexels.com/v1")

    unsplash_access_key: str = os.getenv("UNSPLASH_ACCESS_KEY", "")
    unsplash_base_url:   str = os.getenv("UNSPLASH_BASE_URL", "https://api.unsplash.com")


    # ── Two-Stage Pipeline: Stage 1 Filtering Thresholds ────────────────────
    stage1_high_threshold:   int  = int(os.getenv("STAGE1_HIGH_THRESHOLD",   "80"))
    stage1_medium_threshold: int  = int(os.getenv("STAGE1_MEDIUM_THRESHOLD", "50"))
    stage1_generate_medium:  bool = os.getenv("STAGE1_GENERATE_MEDIUM", "true").lower() == "true"

    # Batch sizes for each stage (token budget management)
    stage1_batch_size: int = int(os.getenv("STAGE1_BATCH_SIZE", "15"))
    stage2_batch_size: int = int(os.getenv("STAGE2_BATCH_SIZE", "8"))


settings = NewsConfig()