"""
financial_results/models.py
---------------------------
Pydantic v2 data models for the Financial Results package.

Contains:
    • Enumerations           — Sentiment, Impact, Direction, Confidence, Recommendation
    • Forecast               — short/medium-term forecast sub-model
    • FinancialResultAnalysis — Gemini AI output schema
    • FinancialResultRecord   — database row model (metadata + analysis + recommendation)
    • API response wrappers  — ResultsListResponse, ResultSingleResponse
    • ResultsSchedulerStatus — scheduler health endpoint model
"""
from __future__ import annotations

from enum import Enum
from typing import List, Optional
from pydantic import BaseModel, Field


# ─── Enumerations ────────────────────────────────────────────────────────────

class Sentiment(str, Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"


class Impact(str, Enum):
    HIGH   = "HIGH"
    MEDIUM = "MEDIUM"
    LOW    = "LOW"


class Direction(str, Enum):
    UP    = "UP"
    DOWN  = "DOWN"
    MIXED = "MIXED"


class Confidence(str, Enum):
    HIGH   = "HIGH"
    MEDIUM = "MEDIUM"
    LOW    = "LOW"


class Recommendation(str, Enum):
    BUY  = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


# ─── Forecast sub-model ─────────────────────────────────────────────────────

class Forecast(BaseModel):
    """Short-term or medium-term directional forecast."""
    direction:  str = Field(description="UP | DOWN | MIXED")
    confidence: str = Field(description="HIGH | MEDIUM | LOW")
    reason:     str = Field(default="", description="Brief reasoning")


# ─── AI Analysis output ─────────────────────────────────────────────────────

class FinancialResultAnalysis(BaseModel):
    """Schema matching the expected Gemini JSON output.

    This is validated against the raw Gemini response.
    Only contains fields that require AI analysis — identity/period
    metadata is already extracted from the filing document itself.
    """
    headline:               str            = Field(default="", description="WSJ-style headline, max 120 chars")

    result_date:            str            = ""

    revenue_change_yoy:     Optional[float] = Field(None, description="YoY percentage change in revenue")
    profit_change_yoy:      Optional[float] = Field(None, description="YoY percentage change in net profit")
    eps_change_yoy:         Optional[float] = Field(None, description="YoY percentage change in EPS")

    executive_summary:      str            = ""

    guidance:               str            = ""

    sentiment:              str            = Field(default="NEUTRAL", description="BULLISH | BEARISH | NEUTRAL")
    impact:                 str            = Field(default="MEDIUM",  description="HIGH | MEDIUM | LOW")

    forecast_short_term:    Optional[Forecast] = None
    forecast_medium_term:   Optional[Forecast] = None

    source:                 List[str]      = Field(default_factory=list)


# ─── Database record ────────────────────────────────────────────────────────

class FinancialResultRecord(BaseModel):
    """Full record stored in the database.

    Combines filing metadata, raw financial data extracted from documents,
    AI analysis, backend-derived recommendation, and operational metadata.
    """
    id:                     str            = Field(description="SHA-256 UID of the filing URL")

    # ── Filing metadata ──────────────────────────────────────────────────
    company_name:           str
    symbol:                 str            = ""
    exchange:               str            = ""
    quarter:                str            = ""
    result_date:            str            = ""
    announcement_date:      Optional[str]  = None
    period_start:           Optional[str]  = None
    period_end:             Optional[str]  = None
    financial_year:         Optional[str]  = None
    standalone_consolidated: Optional[str] = None
    filing_type:            Optional[str]  = None
    document_type:          Optional[str]  = None
    source_url:             str            = Field(default="", exclude=True)

    # ── Extracted Financials (from XBRL/HTML) ────────────────────────────
    # Core (Common)
    revenue:                Optional[float] = Field(None, description="Total revenue = revenue_from_operations + other_income (₹ Cr)")
    profit_before_tax:      Optional[float] = Field(None, description="Profit before tax (₹ Cr)")
    profit_net:             Optional[float] = Field(None, description="Profit after tax (₹ Cr)")
    basic_eps:              Optional[float] = Field(None, description="Basic EPS (₹)")

    # Non-banking specific
    depreciation:           Optional[float] = Field(None, description="Depreciation & amortisation (₹ Cr)")

    # Banking specific
    operating_profit:       Optional[float] = Field(None, description="Operating profit pre-provision (₹ Cr) — banking only")

    # Derived metrics (non-banking)
    ebitda:                 Optional[float] = Field(None, description="EBITDA (₹ Cr) — non-banking only")
    ebitda_margin:          Optional[float] = Field(None, description="EBITDA margin % on revenue from operations — non-banking only")
    pat_margin:             Optional[float] = Field(None, description="PAT margin % on total revenue")
    operating_profit_margin: Optional[float] = Field(None, description="Operating profit margin % — banking: OP/total income; non-banking: EBIT/revenue from ops")

    # ── AI analysis ──────────────────────────────────────────────────
    headline:               str            = Field(default="", description="WSJ-style headline")
    executive_summary:      str            = ""
    guidance:               str            = ""
    sentiment:              str            = "NEUTRAL"
    impact:                 str            = "MEDIUM"
    
    revenue_change_yoy:     Optional[float] = None
    profit_change_yoy:      Optional[float] = None
    eps_change_yoy:         Optional[float] = None

    forecast_short_term:    Optional[Forecast] = None
    forecast_medium_term:   Optional[Forecast] = None

    source_urls:            List[str]      = Field(default_factory=list, exclude=True)

    # ── Backend-derived ──────────────────────────────────────────────────
    recommendation:         str            = "HOLD"

    # ── Operational ──────────────────────────────────────────────────────
    gemini_model:           str            = Field(default="", exclude=True)
    created_at_unix:        float          = Field(default=0.0, exclude=True)
    updated_at_unix:        float          = Field(default=0.0, exclude=True)


# ─── API response wrappers ──────────────────────────────────────────────────

class ResultsListResponse(BaseModel):
    """Paginated response for listing financial results."""
    success:   bool
    page:      int
    page_size: int
    total:     int
    results:   List[FinancialResultRecord]


class ResultSingleResponse(BaseModel):
    """Single financial result response."""
    success: bool = True
    result:  FinancialResultRecord


# ─── Scheduler status ───────────────────────────────────────────────────────

class ResultsSchedulerStatus(BaseModel):
    """Health status for the financial results scheduler."""
    running:               bool
    next_fetch_in_seconds: Optional[int]   = None
    schedule_state:        str             = ""
    interval_minutes:      int             = 0
    total_results_in_db:   int             = 0
    last_fetch_at:         Optional[str]   = None
    last_results_processed: Optional[int]  = None
