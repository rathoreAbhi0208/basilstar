"""
news/news_api.py
----------------
Self-contained FastAPI sub-application for the News package.

Endpoints:
  GET  /            — paginated news list (with rich filtering)
  GET  /status      — scheduler health + DB stats
  GET  /{id}        — single article by ID
  POST /refresh     — manual trigger of a fetch cycle (dev/ops use)

Mount onto the parent app with:
    app.mount("/news", news_app)
"""
from __future__ import annotations

import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    force=True,
)
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request, BackgroundTasks
from google import genai

from .config    import settings, get_market_state, get_fetch_interval_seconds, current_ist
from .db        import NewsDB
from .scheduler import NewsScheduler
from .models    import (
    NewsListResponse,
    NewsSingleResponse,
    SchedulerStatusResponse,
)

logger = logging.getLogger(__name__)


# ─── Lifespan ────────────────────────────────────────────────────────────────

@asynccontextmanager
async def news_lifespan(app: FastAPI):
    logger.info("=== Initialising News Package ===")

    # Database
    db = NewsDB(db_path=settings.db_path)
    await db.init()
    app.state.news_db = db

    # Gemini client
    if not settings.gemini_api_key:
        logger.warning("[News] No GEMINI_API_KEY — news generation disabled")
        gemini_client = None
    else:
        gemini_client = genai.Client(api_key=settings.gemini_api_key)

    # Scheduler
    scheduler = NewsScheduler(
        db            = db,
        gemini_client = gemini_client,
        gemini_model  = settings.gemini_model,
    )
    app.state.news_scheduler = scheduler
    await scheduler.start()

    logger.info("=== News Package Ready ===")
    yield
    logger.info("=== Shutting down News Package ===")
    await scheduler.stop()


# ─── App ─────────────────────────────────────────────────────────────────────

news_app = FastAPI(
    title       = "Basilstar Financial News API",
    description = (
        "Real-time AI-enriched Indian financial news — "
        "stocks, IPOs, regulatory updates, economy, and more."
    ),
    version     = "2.0.0",
    lifespan    = news_lifespan,
    docs_url    = "/api/docs",
    redoc_url   = "/api/redoc",
)


# ─── Routes ──────────────────────────────────────────────────────────────────

@news_app.get(
    "/",
    response_model=NewsListResponse,
    summary="List latest financial news articles",
    description=(
        "Returns paginated, AI-enriched financial news articles. "
        "Filter by category, sentiment, impact, company, sector, tags, or full-text search."
    ),
)
async def list_news(
    request:   Request,
    page:      int            = Query(1,    ge=1,          description="Page number (1-indexed)"),
    page_size: int            = Query(20,   ge=1,  le=100, description="Items per page"),
    category:  Optional[str]  = Query(None,                description="e.g. IPO, Equity, Banking"),
    sentiment: Optional[str]  = Query(None,                description="Positive|Negative|Neutral|Mixed"),
    impact:    Optional[str]  = Query(None,                description="Low|Medium|High|Critical"),
    company:   Optional[str]  = Query(None,                description="Filter by company name"),
    sector:    Optional[str]  = Query(None,                description="Filter by sector"),
    tag:       Optional[str]  = Query(None,                description="Filter by tag"),
    search:    Optional[str]  = Query(None,                description="Full-text search"),
    sort:      Optional[str]  = Query(None,                description="newest|oldest|importance"),
) -> NewsListResponse:
    db: NewsDB = request.app.state.news_db
    offset = (page - 1) * page_size

    articles, total = await db.list_articles(
        category  = category,
        sentiment = sentiment,
        impact    = impact,
        company   = company,
        sector    = sector,
        tag       = tag,
        search    = search,
        sort      = sort,
        limit     = page_size,
        offset    = offset,
    )

    logger.info("[API] Returning %d articles", len(articles))

    return NewsListResponse(
        success          = True,
        cache_updated_at = db.cache_updated_at or current_ist().isoformat(),
        page             = page,
        page_size        = page_size,
        total            = total,
        articles         = articles,
    )


@news_app.get(
    "/status",
    response_model = SchedulerStatusResponse,
    summary        = "Scheduler and DB health status",
)
async def get_status(request: Request) -> SchedulerStatusResponse:
    db:        NewsDB        = request.app.state.news_db
    scheduler: NewsScheduler = request.app.state.news_scheduler

    state    = get_market_state()
    interval = get_fetch_interval_seconds(state) // 60
    total    = await db.count_live()

    return SchedulerStatusResponse(
        running               = scheduler.is_running,
        next_fetch_in_seconds = scheduler.next_fetch_in_seconds(),
        market_state          = state.value,
        interval_minutes      = interval,
        total_articles_in_db  = total,
        last_fetch_at         = scheduler.last_fetch_at,
    )


@news_app.post(
    "/refresh",
    summary     = "Manually trigger a news fetch cycle",
    description = "For ops/dev use. Queues an immediate background fetch cycle.",
)
async def manual_refresh(
    request:     Request,
    background:  BackgroundTasks,
) -> dict:
    scheduler: NewsScheduler = request.app.state.news_scheduler
    if not scheduler.is_running:
        raise HTTPException(status_code=503, detail="Scheduler is not running")

    async def _trigger():
        try:
            await scheduler._do_fetch_and_store()
        except Exception as exc:
            logger.exception("[API] Manual refresh failed: %s", exc)

    background.add_task(_trigger)
    return {"success": True, "message": "Manual refresh triggered in background"}


@news_app.get(
    "/{article_id}",
    response_model = NewsSingleResponse,
    summary        = "Get a single article by ID",
)
async def get_article(
    request:    Request,
    article_id: str,
) -> NewsSingleResponse:
    db: NewsDB = request.app.state.news_db
    article = await db.get_by_id(article_id)
    if not article:
        raise HTTPException(
            status_code=404,
            detail="Article not found or has expired (TTL = 24 h).",
        )
    return NewsSingleResponse(success=True, article=article)
