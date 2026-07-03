"""
news/models.py
--------------
Pydantic v2 data models for the entire News package.

Contains ALL domain models:
    • RawNewsItem           — normalised RSS feed item (fetcher output)
    • EvaluationResult      — Stage 1 Market Intelligence Evaluation output
    • EvaluatedItem         — RawNewsItem + its Stage 1 evaluation (pipeline wire type)
    • NewsArticle           — AI-enriched news article (Stage 2 output → news_articles table)
    • OfficialInformation   — AI-analysed regulatory announcement (OI pipeline output)
    • ImageResult           — image provider response
    • Response wrappers     — NewsListResponse, OfficialInfoListResponse, etc.

Two-Stage Architecture
~~~~~~~~~~~~~~~~~~~~~~
  Stage 1: RawNewsItem  → EvaluationResult   (Market Intelligence Evaluation)
  Stage 2: EvaluatedItem → NewsArticle        (Premium Article Generation)

  The EvaluatedItem is the wire type between stages — it carries both the
  original RawNewsItem and its Stage 1 evaluation so Stage 2 can reference
  context, executive_summary, and scores.

Design rules
~~~~~~~~~~~~
• OfficialInformation has NO ``story`` field. It is intelligence, not editorial.
• RawNewsItem is the shared wire type between the fetcher and Stage 1 for all pipelines.
• Both pipelines share ImageResult for image resolution output.
"""
from __future__ import annotations

import hashlib
from enum import Enum
from typing import List, Optional
from pydantic import BaseModel, Field


# ─── Shared enumerations ─────────────────────────────────────────────────────

class Sentiment(str, Enum):
    POSITIVE = "Positive"
    NEGATIVE = "Negative"
    NEUTRAL  = "Neutral"
    MIXED    = "Mixed"


class Impact(str, Enum):
    LOW      = "Low"
    MEDIUM   = "Medium"
    HIGH     = "High"
    CRITICAL = "Critical"


class Category(str, Enum):
    IPO            = "IPO"
    EQUITY         = "Equity"
    MUTUAL_FUNDS   = "Mutual Funds"
    ECONOMY        = "Economy"
    COMMODITIES    = "Commodities"
    FOREX          = "Forex"
    BANKING        = "Banking"
    CORPORATE      = "Corporate"
    POLICY         = "Policy"
    TAXATION       = "Taxation"
    STARTUP        = "Startup"
    CRYPTOCURRENCY = "Cryptocurrency"
    INTERNATIONAL  = "International"
    TECHNOLOGY     = "Technology"
    RESULTS        = "Results"
    EARNINGS       = "Earnings"
    DIVIDEND       = "Dividend"
    BONUS          = "Bonus"
    RIGHTS_ISSUE   = "Rights Issue"
    REGULATION     = "Regulation"


class EventCategory(str, Enum):
    """Stage 1 detected event categories."""
    RESULTS             = "Results"
    DIVIDEND            = "Dividend"
    BONUS               = "Bonus"
    RIGHTS_ISSUE        = "Rights Issue"
    IPO                 = "IPO"
    MERGER              = "Merger"
    ACQUISITION         = "Acquisition"
    SEBI_CIRCULAR       = "SEBI Circular"
    NSE_CIRCULAR        = "NSE Circular"
    BSE_NOTICE          = "BSE Notice"
    RBI_POLICY          = "RBI Policy"
    REPO_RATE           = "Repo Rate"
    INFLATION           = "Inflation"
    GDP                 = "GDP"
    BUDGET              = "Budget"
    CRUDE_OIL           = "Crude Oil"
    FII                 = "FII"
    DII                 = "DII"
    BLOCK_DEAL          = "Block Deal"
    BULK_DEAL           = "Bulk Deal"
    CREDIT_RATING       = "Credit Rating"
    CORPORATE_GOVERNANCE= "Corporate Governance"
    REGULATORY_ACTION   = "Regulatory Action"
    MANAGEMENT_CHANGE   = "Management Change"
    BANKRUPTCY          = "Bankruptcy"
    LEGAL_ACTION        = "Legal Action"
    OTHER               = "Other"


class FilterDecision(str, Enum):
    """Result of Stage 1 score-based filtering."""
    GENERATE  = "generate"    # score >= high_threshold
    MAYBE     = "maybe"       # medium_threshold <= score < high_threshold
    DISCARD   = "discard"     # score < medium_threshold


# ─── Raw feed item (shared by all pipelines) ─────────────────────────────────

class RawNewsItem(BaseModel):
    """
    Normalised RSS feed item produced by the fetcher.

    This is the wire type between the fetcher and Stage 1 for all pipelines.
    Both the News and Official Intelligence pipelines consume RawNewsItem.
    """
    source_name:  str
    source_tier:  int
    title:        str
    url:          str
    summary:      str
    published_at: str         # ISO datetime string (UTC)
    category:     str
    uid:          str         # sha256 of canonical URL


# ─── Stage 1: Market Intelligence Evaluation ─────────────────────────────────

class EvaluationResult(BaseModel):
    """
    Output of Stage 1 — Market Intelligence Evaluation.

    Gemini acts as a Senior Market Intelligence Analyst.
    This is NOT an article — it is a structured evaluation + executive summary.

    Fields
    ------
    uid                     : Mirrors the RawNewsItem.uid for correlation.
    market_relevance_score  : 0-100 importance for traders/investors.
    confidence_score        : 0-100 confidence in the evaluation.
    time_horizon            : Short-term catalyst, long-term structural, or both.
    reason                  : One concise sentence explaining the score.
    event_category          : Detected event type from the EventCategory enum.
    executive_summary       : 3 short paragraphs combining current event,
                              historical context, and market implication.
    market_indices_impact   : Indices likely affected (e.g. Nifty 50, Sensex).
    affected_companies      : Companies likely affected (empty list if none).
    affected_sectors        : Sectors likely affected (empty list if none).
    """
    uid:                    str
    market_relevance_score: int   = Field(ge=0, le=100)
    confidence_score:       int   = Field(ge=0, le=100)
    time_horizon:           str   = Field(description="short_term_catalyst | long_term_structural | both")
    reason:                 str   = Field(description="One concise sentence explaining the score.")
    event_category:         str   = Field(description="Detected event category string.")
    executive_summary:      str   = Field(
        description=(
            "4 short paragraphs: (1) what happened, (2) historical context & why it matters, "
            "(3) risks, opportunities & time horizon, (4) actionable conclusion."
        )
    )
    market_indices_impact:  List[str] = Field(
        default_factory=list,
        description="Indices likely impacted: Nifty 50, Nifty 500, Sensex, Bank Nifty, etc.",
    )
    affected_companies:     List[str] = Field(default_factory=list)
    affected_sectors:       List[str] = Field(default_factory=list)


class EvaluationBatchOutput(BaseModel):
    """Wrapper for the Stage 1 batch JSON response."""
    evaluations: List[EvaluationResult]


class EvaluatedItem(BaseModel):
    """
    Wire type between Stage 1 and Stage 2.

    Combines the original RawNewsItem with its Stage 1 EvaluationResult
    so Stage 2 can enrich the article with historical context, scores, and
    the pre-written executive_summary.
    """
    raw:        RawNewsItem
    evaluation: EvaluationResult
    decision:   str = Field(description="FilterDecision value: generate | maybe | discard")


# ─── Stage 2: News Article (Premium Article Generation output) ────────────────

class NewsArticle(BaseModel):
    """AI-enriched news article produced by Stage 2."""
    id: str = Field(description="SHA-256 of the canonical headline")

    # Content
    headline:         str = Field(description="AI-crafted, engaging headline (max 120 chars)")
    executive_summary: str = Field(
        description=(
            "3-paragraph executive summary combining the current event, historical context, "
            "and market implication. Primary text shown on the UI."
        )
    )
    # Classification
    story:               str = Field(description="Full ~500-word article body")
    sentiment:           str = Field(description="Positive | Negative | Neutral | Mixed")
    market_impact_level: str = Field(description="Low | Medium | High | Critical")

    # Scores (0-100) — carried forward from Stage 1
    market_relevance_score: int = Field(ge=0, le=100, default=0)
    confidence_score:       int = Field(ge=0, le=100, default=0)
    time_horizon:           str = Field(default="both")

    # Stage 2 impact narratives
    market_impact:           str
    retail_investor_impact:  str
    institutional_impact:    str

    # Trading/Risk analysis
    trading_implications: Optional[str] = Field(None, description="Short-term trading implications.")
    risk_factors:         Optional[str] = Field(None, description="Key risk factors investors should consider.")
    future_outlook:       Optional[str] = Field(None, description="Medium-term outlook for the event.")

    # Entity lists
    affected_sectors:   List[str]
    affected_companies: List[str]
    market_indices:     List[str] = Field(default_factory=list)
    tags:               List[str]

    # Source
    source:       Optional[str] = Field(None, description="RSS feed / platform the article originated from")
    published_at: Optional[str] = Field(None, description="Original publication timestamp from RSS")

    # Media & Entities
    primary_entity: Optional[str] = Field(None)
    entity_type:    Optional[str] = Field(None)
    image_query:    Optional[str] = Field(None)
    image_url:      Optional[str] = Field(None)
    image_alt:      Optional[str] = Field(None)

    # Stage 1 traceability
    event_category:  Optional[str] = Field(None, description="Event category detected in Stage 1.")



# ─── Image Provider Models ───────────────────────────────────────────────────

class ImageResult(BaseModel):
    image_url:        str
    thumbnail_url:    str
    provider:         str
    photographer:     str
    photographer_url: str
    width:            int
    height:           int
    license:          str


# ─── API Response Wrappers — News ────────────────────────────────────────────

class NewsListResponse(BaseModel):
    success:          bool
    cache_updated_at: str
    page:             int
    page_size:        int
    total:            int
    articles:         List[NewsArticle]


class NewsSingleResponse(BaseModel):
    success: bool = True
    article: NewsArticle


# ─── API Response Wrappers — Raw News ────────────────────────────────────────

class RawNewsRecord(BaseModel):
    uid:                     str
    title:                   str
    url:                     str
    summary:                 Optional[str] = None
    source_name:             str
    source_tier:             int
    published_at:            Optional[str] = None
    category:                Optional[str] = None
    market_relevance_score:  Optional[int] = None
    confidence_score:        Optional[int] = None
    decision:                Optional[str] = None
    reason:                  Optional[str] = None
    event_category:          Optional[str] = None
    time_horizon:            Optional[str] = None
    executive_summary:       Optional[str] = None
    market_indices_impact:   List[str] = Field(default_factory=list)
    affected_companies:      List[str] = Field(default_factory=list)
    affected_sectors:        List[str] = Field(default_factory=list)
    created_at_unix:         float

class RawNewsListResponse(BaseModel):
    success:   bool
    page:      int
    page_size: int
    total:     int
    items:     List[RawNewsRecord]




# ─── Scheduler Status ────────────────────────────────────────────────────────

class SchedulerStatusResponse(BaseModel):
    running:               bool
    next_fetch_in_seconds: Optional[int]
    market_state:          str
    interval_minutes:      int
    total_articles_in_db:  int
    last_fetch_at:         Optional[str] = None
    pipelines:             List[str]     = Field(default_factory=list)
    # Two-stage pipeline stats (optional, populated when available)
    last_stage1_evaluated: Optional[int] = Field(None, description="Items evaluated in last Stage 1 run.")
    last_stage1_passed:    Optional[int] = Field(None, description="Items that passed Stage 1 filtering.")
    last_stage2_generated: Optional[int] = Field(None, description="Articles generated in last Stage 2 run.")
