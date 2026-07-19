"""
financial_results/config.py
---------------------------
Central configuration for the Financial Results package.

Two polling intervals:
    • DAY   (10:00–21:00 IST) — default 1 hour
    • NIGHT (21:00–10:00 IST) — default 2 hours

Both intervals are adjustable via environment variables.
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

# ─── Day Window ──────────────────────────────────────────────────────────────
_DAY_START = time(10, 0)   # 10:00 AM IST
_DAY_END   = time(21, 0)   # 9:00 PM IST

# ─── Fetch Intervals (seconds) ──────────────────────────────────────────────
INTERVAL_DAY   = int(os.getenv("RESULTS_INTERVAL_DAY",   "3600"))   # 1 hour
INTERVAL_NIGHT = int(os.getenv("RESULTS_INTERVAL_NIGHT", "7200"))   # 2 hours


class ScheduleState(str, Enum):
    DAY   = "DAY"
    NIGHT = "NIGHT"


def get_schedule_state(now: datetime | None = None) -> ScheduleState:
    """Determine whether current time falls in the DAY or NIGHT window."""
    if now is None:
        now = datetime.now(tz=IST)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=IST)

    t = now.time()
    if _DAY_START <= t < _DAY_END:
        return ScheduleState.DAY
    return ScheduleState.NIGHT


def get_fetch_interval_seconds(state: ScheduleState | None = None) -> int:
    """Return the polling interval in seconds for the given schedule state."""
    if state is None:
        state = get_schedule_state()
    return {
        ScheduleState.DAY:   INTERVAL_DAY,
        ScheduleState.NIGHT: INTERVAL_NIGHT,
    }[state]


def current_ist() -> datetime:
    """Return the current time in IST."""
    return datetime.now(tz=IST)


# ─── Results Settings ───────────────────────────────────────────────────────
class ResultsConfig:
    # ── Gemini ───────────────────────────────────────────────────────────────
    gemini_api_key: str   = os.getenv("GEMINI_API_KEY", "")
    gemini_model:   str   = os.getenv("GEMINI_MODEL",   "gemini-2.5-flash")
    temperature:    float = float(os.getenv("RESULTS_TEMPERATURE", "0.0"))
    top_p:          float = float(os.getenv("RESULTS_TOP_P",       "0.95"))
    max_retries:    int   = int(os.getenv("RESULTS_MAX_RETRIES",   "3"))

    # ── Database ─────────────────────────────────────────────────────────────
    db_path: str = os.getenv("RESULTS_DB_PATH", "results.db")

    # ── Retention ────────────────────────────────────────────────────────────
    retention_hours: int = int(os.getenv("RESULTS_RETENTION_HOURS", "168"))   # 7 days

    # ── Fetch pipeline ───────────────────────────────────────────────────────
    request_timeout: int = int(os.getenv("RESULTS_REQUEST_TIMEOUT", "30"))
    batch_size:      int = int(os.getenv("RESULTS_BATCH_SIZE",      "10"))


settings = ResultsConfig()
