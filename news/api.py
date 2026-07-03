"""
news/api.py
-----------
Single FastAPI application — the ONLY API entry point for this service.

All sources (Google News + NSE/BSE/SEBI) are processed by the unified
two-stage pipeline and stored in news_articles. There is one unified endpoint.

Routes:
    GET  /news                  — paginated news list (all sources unified)
    GET  /news/status           — scheduler status + Stage 1/2 pipeline stats
    GET  /news/{id}             — single news article
    POST /news/refresh          — manual fetch trigger

Mount onto the parent app with:
    app.mount("/", news_app)

There is exactly ONE lifespan, ONE scheduler, ONE DB connection, ONE Gemini client.
"""
from __future__ import annotations

import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    force=True,
)

from typing import Optional

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request, BackgroundTasks
from google import genai

from .config    import settings, get_market_state, get_fetch_interval_seconds, current_ist
from .db        import NewsDB
from .models    import (
    NewsListResponse,
    NewsSingleResponse,
    SchedulerStatusResponse,
    RawNewsListResponse,
)
from .scheduler import NewsScheduler

logger = logging.getLogger(__name__)


# ─── Lifespan ────────────────────────────────────────────────────────────────

async def init_news_module(app: FastAPI):
    """
    Start-up: initialise ONE DB, ONE Gemini client, ONE scheduler.
    """
    logger.info("=== Basilstar News Service Starting ===")

    # ── Database ──────────────────────────────────────────────────────────
    db = NewsDB(db_path=settings.db_path)
    await db.init()
    app.state.db = db

    # ── Gemini client ─────────────────────────────────────────────────────
    if not settings.gemini_api_key:
        logger.warning("[API] No GEMINI_API_KEY — generation disabled")
        gemini_client = None
    else:
        gemini_client = genai.Client(api_key=settings.gemini_api_key)

    # ── Scheduler (unified pipeline) ──────────────────────────────────────
    scheduler = NewsScheduler(
        db            = db,
        gemini_client = gemini_client,
        gemini_model  = settings.gemini_model,
    )
    app.state.scheduler = scheduler

    if gemini_client:
        await scheduler.start()
    else:
        logger.warning("[API] Scheduler not started (no Gemini key)")

    logger.info("=== Basilstar News Service Ready ===")


async def close_news_module(app: FastAPI):
    """
    Shut-down: gracefully stop the scheduler.
    """
    logger.info("=== Basilstar News Service Shutting Down ===")
    scheduler = getattr(app.state, "scheduler", None)
    if scheduler:
        await scheduler.stop()


# ─── App ─────────────────────────────────────────────────────────────────────

router = APIRouter(
    prefix="/news",
    tags=["News"]
)

# ═══════════════════════════════════════════════════════════════════════════════
# NEWS ROUTES
# ═══════════════════════════════════════════════════════════════════════════════

@router.get(
    "",
    response_model = NewsListResponse,
    summary        = "List latest financial news articles (all sources)",
    description    = (
        "Returns paginated, AI-enriched financial news articles from all sources "
        "(Google News, NSE, BSE, SEBI). Filter by sentiment, market_impact_level, "
        "time_horizon, company, sector, source, tags, or full-text search."
    ),
)
async def list_news(
    request:   Request,
    page:      int           = Query(1,    ge=1,         description="Page number (1-indexed)"),
    page_size: int           = Query(20,   ge=1, le=100, description="Items per page"),
    sentiment:           Optional[str] = Query(None,               description="Positive|Negative|Neutral|Mixed"),
    market_impact_level: Optional[str] = Query(None,               description="Low|Medium|High|Critical"),
    time_horizon:        Optional[str] = Query(None,               description="short_term_catalyst|long_term_structural|both"),
    company:             Optional[str] = Query(None,               description="Filter by company name"),
    sector:    Optional[str] = Query(None,               description="Filter by sector"),
    tag:       Optional[str] = Query(None,               description="Filter by tag"),
    search:    Optional[str] = Query(None,               description="Full-text search"),
    sort:      Optional[str] = Query(None,               description="newest|oldest|importance"),
    source:    Optional[str] = Query(None,               description="Filter by source (e.g. NSE, BSE, SEBI, Google News RBI)"),
) -> NewsListResponse:
    db: NewsDB = request.app.state.db
    offset = (page - 1) * page_size

    articles, total = await db.list_articles(
        sentiment           = sentiment,
        market_impact_level = market_impact_level,
        time_horizon        = time_horizon,
        company   = company,
        sector    = sector,
        tag       = tag,
        search    = search,
        sort      = sort,
        source    = source,
        limit     = page_size,
        offset    = offset,
    )

    logger.info("[API/News] Returning %d articles (total=%d)", len(articles), total)
    return NewsListResponse(
        success          = True,
        cache_updated_at = db.cache_updated_at or current_ist().isoformat(),
        page             = page,
        page_size        = page_size,
        total            = total,
        articles         = articles,
    )


@router.get(
    "/raw",
    response_model = RawNewsListResponse,
    summary        = "List raw fetched news with Stage 1 scores",
    description    = "Returns all fetched raw items and their Stage 1 evaluation data, including items that were discarded.",
)
async def list_raw_news(
    request:   Request,
    page:      int           = Query(1,    ge=1,         description="Page number (1-indexed)"),
    page_size: int           = Query(50,   ge=1, le=200, description="Items per page"),
    source:    Optional[str] = Query(None,               description="Filter by source (e.g. NSE, BSE, SEBI)"),
    decision:  Optional[str] = Query(None,               description="Filter by decision (generate, maybe, discard)"),
    time_horizon: Optional[str] = Query(None,            description="short_term_catalyst|long_term_structural|both"),
) -> RawNewsListResponse:
    db: NewsDB = request.app.state.db
    offset = (page - 1) * page_size

    items, total = await db.list_raw_items(
        source_name  = source,
        decision     = decision,
        time_horizon = time_horizon,
        limit        = page_size,
        offset      = offset,
    )

    logger.info("[API/News] Returning %d raw items (total=%d)", len(items), total)
    return RawNewsListResponse(
        success   = True,
        page      = page,
        page_size = page_size,
        total     = total,
        items     = items,
    )


@router.get(
    "/status",
    response_model = SchedulerStatusResponse,
    summary        = "Scheduler and pipeline health status",
)
async def get_status(request: Request) -> SchedulerStatusResponse:
    db:        NewsDB        = request.app.state.db
    scheduler: NewsScheduler = request.app.state.scheduler

    state           = get_market_state()
    interval        = get_fetch_interval_seconds(state) // 60
    total_articles  = await db.count_live()
    pipeline_stats  = scheduler.last_pipeline_stats

    return SchedulerStatusResponse(
        running               = scheduler.is_running,
        next_fetch_in_seconds = scheduler.next_fetch_in_seconds(),
        market_state          = state.value,
        interval_minutes      = interval,
        total_articles_in_db  = total_articles,
        last_fetch_at         = scheduler.last_fetch_at,
        pipelines             = scheduler.pipeline_names,
        last_stage1_evaluated = pipeline_stats.get("stage1_evaluated"),
        last_stage1_passed    = pipeline_stats.get("stage1_passed"),
        last_stage2_generated = pipeline_stats.get("stage2_generated"),
    )


@router.post(
    "/refresh",
    summary     = "Manually trigger a full fetch cycle",
    description = "For ops/dev use. Immediately queues a background cycle across all sources.",
)
async def manual_refresh(
    request:    Request,
    background: BackgroundTasks,
) -> dict:
    scheduler: NewsScheduler = request.app.state.scheduler
    if not scheduler.is_running:
        raise HTTPException(status_code=503, detail="Scheduler is not running")

    async def _trigger():
        try:
            await scheduler._do_fetch_and_store()
        except Exception as exc:
            logger.exception("[API] Manual refresh failed: %s", exc)

    background.add_task(_trigger)
    return {"success": True, "message": "Unified pipeline refresh triggered in background"}


@router.get(
    "/{article_id}",
    response_model = NewsSingleResponse,
    summary        = "Get a single news article by ID",
)
async def get_article(
    request:    Request,
    article_id: str,
) -> NewsSingleResponse:
    db: NewsDB = request.app.state.db
    article    = await db.get_by_id(article_id)
    if not article:
        raise HTTPException(
            status_code=404,
            detail="Article not found or has expired (TTL = 24 h).",
        )
    return NewsSingleResponse(success=True, article=article)
