"""
financial_results/api.py
------------------------
FastAPI router for the Financial Results Analysis module.

Routes:
    GET  /financial-results             — paginated list with filters
    GET  /financial-results/search      — full-text search
    GET  /financial-results/{symbol}    — results for a specific symbol
    GET  /financial-results/status      — scheduler health
    POST /financial-results/refresh     — manual trigger

Lifecycle hooks:
    init_results_module(app)   — start-up: init DB, Gemini, scheduler
    close_results_module(app)  — shut-down: stop scheduler
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request, BackgroundTasks
from google import genai

from .config    import settings, get_schedule_state, get_fetch_interval_seconds, current_ist
from .db        import ResultsDB
from .models    import (
    ResultsListResponse,
    ResultSingleResponse,
    ResultsSchedulerStatus,
)
from .scheduler import ResultsScheduler

logger = logging.getLogger(__name__)


# ─── Lifespan hooks ─────────────────────────────────────────────────────────

async def init_results_module(app: FastAPI) -> None:
    """Start-up: initialise DB, Gemini client, and scheduler."""
    logger.info("=== Basilstar Financial Results Module Starting ===")

    # ── Database ─────────────────────────────────────────────────────────
    db = ResultsDB(db_path=settings.db_path)
    await db.init()
    app.state.results_db = db

    # ── Gemini client ────────────────────────────────────────────────────
    if not settings.gemini_api_key:
        logger.warning("[Results/API] No GEMINI_API_KEY — analysis disabled")
        gemini_client = None
    else:
        gemini_client = genai.Client(api_key=settings.gemini_api_key)

    # ── Scheduler ────────────────────────────────────────────────────────
    scheduler = ResultsScheduler(
        db            = db,
        gemini_client = gemini_client,
        gemini_model  = settings.gemini_model,
    )
    app.state.results_scheduler = scheduler

    if gemini_client:
        await scheduler.start()
    else:
        logger.warning("[Results/API] Scheduler not started (no Gemini key)")

    logger.info("=== Basilstar Financial Results Module Ready ===")


async def close_results_module(app: FastAPI) -> None:
    """Shut-down: gracefully stop the scheduler."""
    logger.info("=== Basilstar Financial Results Module Shutting Down ===")
    scheduler = getattr(app.state, "results_scheduler", None)
    if scheduler:
        await scheduler.stop()


# ─── Router ─────────────────────────────────────────────────────────────────

router = APIRouter(
    prefix="/financial-results",
    tags=["Financial Results"],
)


# ═══════════════════════════════════════════════════════════════════════════════
# ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════════

@router.get(
    "",
    response_model=ResultsListResponse,
    summary="List financial results with filters",
    description=(
        "Returns paginated AI-powered financial result analyses. "
        "Filter by exchange, quarter, sentiment, impact, or recommendation."
    ),
)
async def list_results(
    request:        Request,
    page:           int           = Query(1,  ge=1,         description="Page number (1-indexed)"),
    page_size:      int           = Query(20, ge=1, le=100, description="Items per page"),
    exchange:       Optional[str] = Query(None,             description="NSE | BSE"),
    quarter:        Optional[str] = Query(None,             description="Q1 | Q2 | Q3 | Q4"),
    sentiment:      Optional[str] = Query(None,             description="BULLISH | BEARISH | NEUTRAL"),
    impact:         Optional[str] = Query(None,             description="HIGH | MEDIUM | LOW"),
    recommendation: Optional[str] = Query(None,             description="BUY | SELL | HOLD"),
) -> ResultsListResponse:
    db: ResultsDB = request.app.state.results_db
    offset = (page - 1) * page_size

    results, total = await db.list_results(
        exchange       = exchange,
        quarter        = quarter,
        sentiment      = sentiment,
        impact         = impact,
        recommendation = recommendation,
        limit          = page_size,
        offset         = offset,
    )

    logger.info("[Results/API] Returning %d results (total=%d)", len(results), total)
    return ResultsListResponse(
        success   = True,
        page      = page,
        page_size = page_size,
        total     = total,
        results   = results,
    )


@router.get(
    "/search",
    response_model=ResultsListResponse,
    summary="Search financial results",
    description="Full-text search across company name, symbol, and executive summary.",
)
async def search_results(
    request:   Request,
    q:         str = Query(..., min_length=1, description="Search query"),
    page:      int = Query(1,  ge=1,         description="Page number (1-indexed)"),
    page_size: int = Query(20, ge=1, le=100, description="Items per page"),
) -> ResultsListResponse:
    db: ResultsDB = request.app.state.results_db
    offset = (page - 1) * page_size

    results, total = await db.search_results(
        query  = q,
        limit  = page_size,
        offset = offset,
    )

    logger.info("[Results/API] Search '%s' → %d results (total=%d)", q, len(results), total)
    return ResultsListResponse(
        success   = True,
        page      = page,
        page_size = page_size,
        total     = total,
        results   = results,
    )


@router.get(
    "/status",
    response_model=ResultsSchedulerStatus,
    summary="Scheduler health and stats",
)
async def get_status(request: Request) -> ResultsSchedulerStatus:
    db:        ResultsDB        = request.app.state.results_db
    scheduler: ResultsScheduler = request.app.state.results_scheduler

    state          = get_schedule_state()
    interval       = get_fetch_interval_seconds(state) // 60
    total_results  = await db.count_live()

    return ResultsSchedulerStatus(
        running                = scheduler.is_running,
        next_fetch_in_seconds  = scheduler.next_fetch_in_seconds(),
        schedule_state         = state.value,
        interval_minutes       = interval,
        total_results_in_db    = total_results,
        last_fetch_at          = scheduler.last_fetch_at,
        last_results_processed = scheduler.last_results_processed,
    )


@router.post(
    "/refresh",
    summary="Manually trigger a pipeline cycle",
    description="For ops/dev use. Immediately queues a background analysis cycle.",
)
async def manual_refresh(
    request:    Request,
    background: BackgroundTasks,
) -> dict:
    scheduler: ResultsScheduler = request.app.state.results_scheduler
    if not scheduler.is_running:
        raise HTTPException(status_code=503, detail="Results scheduler is not running")

    async def _trigger():
        try:
            await scheduler._do_fetch_and_store()
        except Exception as exc:
            logger.exception("[Results/API] Manual refresh failed: %s", exc)

    background.add_task(_trigger)
    return {"success": True, "message": "Financial results pipeline refresh triggered in background"}


@router.get(
    "/{symbol}",
    response_model=ResultsListResponse,
    summary="Get results for a specific stock symbol",
    description="Returns all financial results for the given trading symbol or company name.",
)
async def get_by_symbol(
    request: Request,
    symbol:  str,
) -> ResultsListResponse:
    db: ResultsDB = request.app.state.results_db
    results = await db.get_by_symbol(symbol)

    if not results:
        raise HTTPException(
            status_code=404,
            detail=f"No financial results found for symbol '{symbol}'.",
        )

    return ResultsListResponse(
        success   = True,
        page      = 1,
        page_size = len(results),
        total     = len(results),
        results   = results,
    )
