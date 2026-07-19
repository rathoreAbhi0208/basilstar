"""
financial_results/utils.py
--------------------------
Shared utility functions for the Financial Results package.

Contains:
    • derive_quarter()       — period-end date → Q1/Q2/Q3/Q4
    • make_uid()             — SHA-256 UID from URL
    • strip_markdown()       — remove markdown code fences
    • parse_json_response()  — JSON parsing with automatic recovery
"""
from __future__ import annotations

import hashlib
import json
import logging
import re

logger = logging.getLogger(__name__)


# ─── Quarter derivation ─────────────────────────────────────────────────────

# Month → Quarter mapping based on Indian financial year (April–March)
_MONTH_TO_QUARTER: dict[int, str] = {
    6:  "Q1",   # Apr–Jun  → 30-Jun
    9:  "Q2",   # Jul–Sep  → 30-Sep
    12: "Q3",   # Oct–Dec  → 31-Dec
    3:  "Q4",   # Jan–Mar  → 31-Mar
}


def derive_quarter(period_end: str | None) -> str | None:
    """Derive the reporting quarter from the period-end date.

    Accepts dates in common formats:
        • 2024-06-30
        • 30-Jun-2024
        • 30/06/2024
        • 2024-06-30T00:00:00

    Returns Q1, Q2, Q3, Q4 or None if unable to parse.
    """
    if not period_end:
        return None

    period_end = period_end.strip()

    # Try ISO format first: 2024-06-30 or 2024-06-30T00:00:00
    iso_match = re.match(r"(\d{4})-(\d{2})-(\d{2})", period_end)
    if iso_match:
        month = int(iso_match.group(2))
        return _MONTH_TO_QUARTER.get(month)

    # Try DD-Mon-YYYY: 30-Jun-2024
    mon_match = re.match(
        r"\d{1,2}[-/](Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[-/]\d{2,4}",
        period_end,
        re.IGNORECASE,
    )
    if mon_match:
        month_abbr = mon_match.group(1).capitalize()
        month_map = {
            "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4,
            "May": 5, "Jun": 6, "Jul": 7, "Aug": 8,
            "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
        }
        month = month_map.get(month_abbr)
        if month:
            return _MONTH_TO_QUARTER.get(month)

    # Try DD/MM/YYYY: 30/06/2024
    dmy_match = re.match(r"\d{1,2}[/](\d{2})[/]\d{2,4}", period_end)
    if dmy_match:
        month = int(dmy_match.group(1))
        return _MONTH_TO_QUARTER.get(month)

    logger.debug("[Utils] Could not derive quarter from period_end: %r", period_end)
    return None


# ─── UID generation ──────────────────────────────────────────────────────────

def make_uid(url: str) -> str:
    """Stable, collision-resistant UID: SHA-256 of normalised URL."""
    normalised = url.strip().lower().rstrip("/")
    return hashlib.sha256(normalised.encode()).hexdigest()


# ─── Markdown stripping ─────────────────────────────────────────────────────

def strip_markdown(text: str) -> str:
    """Remove leading/trailing markdown code fences from Gemini responses."""
    text = text.strip()
    text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
    text = re.sub(r"\n?```$", "", text)
    return text.strip()


# ─── JSON parsing with recovery ─────────────────────────────────────────────

def parse_json_response(raw_text: str) -> dict | None:
    """Parse a Gemini JSON response into a dict.

    Attempts:
        1. Direct JSON parse of the stripped text.
        2. Regex extraction of the outermost JSON object.

    Returns None on any parse failure (logged).
    """
    raw_text = strip_markdown(raw_text)
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        pass

    # Attempt regex recovery
    m = re.search(r"\{.*\}", raw_text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group())
        except Exception as inner:
            logger.exception(
                "[Utils] JSON recovery failed: %s | snippet: %.300s",
                inner, raw_text,
            )
            return None

    logger.error("[Utils] JSON parse failed | snippet: %.300s", raw_text)
    return None
