"""
news/models.py
--------------
Pydantic data models for the News package.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional, List
from pydantic import BaseModel, Field


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


class NewsArticle(BaseModel):
    id: str = Field(description="SHA-256 of canonical URL + publish date")

    # Content
    headline:      str = Field(description="AI-crafted, engaging headline (max 120 chars)")
    short_summary: str = Field(description="2–3 sentence executive summary")
    story:         str = Field(description="Full ~500-word article body")

    # Classification
    category:    str = Field(description="Primary category")
    subcategory: str = Field(description="Specific sub-topic")
    sentiment:   str = Field(description="Positive | Negative | Neutral | Mixed")
    impact:      str = Field(description="Low | Medium | High | Critical")

    # Scores  (0-100)
    importance_score:  int = Field(ge=0, le=100)
    confidence_score:  int = Field(ge=0, le=100)

    # Impact narratives
    market_impact:           str
    retail_investor_impact:  str
    institutional_impact:    str

    # Entity lists
    affected_sectors:   List[str]
    affected_companies: List[str]
    tags:               List[str]

    # Media & Entities
    primary_entity: Optional[str] = Field(None, description="Main company, person, or organization")
    entity_type:    Optional[str] = Field(None, description="E.g., Company, Regulatory Body, Person")
    image_query:    Optional[str] = Field(None, description="High-quality search query for image providers")
    image_url:      Optional[str] = Field(None, description="Resolved image URL from providers")
    image_alt:      Optional[str] = Field(None, description="Alt text for the image")

# ─── Image Provider Models ──────────────────────────────────────────────────

class ImageResult(BaseModel):
    image_url: str
    thumbnail_url: str
    provider: str
    photographer: str
    photographer_url: str
    width: int
    height: int
    license: str

# ─── API Response wrappers ───────────────────────────────────────────────────

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


class SchedulerStatusResponse(BaseModel):
    running:               bool
    next_fetch_in_seconds: Optional[int]
    market_state:          str
    interval_minutes:      int
    total_articles_in_db:  int
    last_fetch_at:         Optional[str] = None
