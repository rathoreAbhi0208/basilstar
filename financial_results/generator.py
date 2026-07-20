"""
financial_results/generator.py
------------------------------
Gemini-powered financial result analysis engine.

Contains:
    • analyze_financial_result()    — single-item analysis via Gemini
    • derive_recommendation()      — deterministic BUY/SELL/HOLD logic
    • _call_gemini()               — async Gemini API wrapper

The recommendation is NEVER produced by Gemini.  Gemini returns sentiment
and impact; the backend deterministically derives:
    BULLISH + HIGH   → BUY
    BEARISH + HIGH   → SELL
    Otherwise        → HOLD
"""
from __future__ import annotations

import logging
import time

from google import genai
from google.genai import types

from .config   import settings, current_ist
from .models   import (
    FinancialResultRecord,
    Forecast,
    Recommendation,
)
from .prompts  import FinancialAnalysisOutput, build_financial_analysis_prompt
from .schemas  import FinancialResultMetadata
from .utils    import parse_json_response

logger = logging.getLogger(__name__)


# ─── Recommendation logic ───────────────────────────────────────────────────

def derive_recommendation(sentiment: str, impact: str) -> str:
    """Derive a deterministic recommendation from sentiment and impact.

    Rules:
        BULLISH + HIGH   → BUY
        BEARISH + HIGH   → SELL
        Otherwise        → HOLD
    """
    s = sentiment.upper().strip()
    i = impact.upper().strip()

    if s == "BULLISH" and i == "HIGH":
        return Recommendation.BUY.value
    if s == "BEARISH" and i == "HIGH":
        return Recommendation.SELL.value
    return Recommendation.HOLD.value


# ─── Gemini API wrapper ─────────────────────────────────────────────────────

async def _call_gemini(
    client:     genai.Client,
    model_name: str,
    prompt:     str,
) -> dict | None:
    """Single Gemini API call with Google Search grounding.

    Returns:
        Parsed dict from the JSON response, or None on failure.
    """
    tools = [types.Tool(google_search=types.GoogleSearch())]

    try:
        response = await client.aio.models.generate_content(
            model    = model_name,
            contents = prompt,
            config   = types.GenerateContentConfig(
                temperature = settings.temperature,
                top_p       = settings.top_p,
                tools       = tools,
            ),
        )
    except Exception as exc:
        logger.exception("[Results/Gemini] API error: %s", exc)
        return None

    raw_text = (response.text or "").strip()
    if not raw_text:
        logger.warning("[Results/Gemini] Empty response")
        return None

    return parse_json_response(raw_text)


# ─── Single-item analysis ───────────────────────────────────────────────────

async def analyze_financial_result(
    client:     genai.Client,
    model_name: str,
    metadata:   FinancialResultMetadata,
) -> FinancialResultRecord | None:
    """Analyze a single financial result filing via Gemini.

    Pipeline:
        1. Build prompt from metadata.
        2. Call Gemini with Google Search grounding.
        3. Validate JSON response.
        4. Derive recommendation (backend logic).
        5. Construct FinancialResultRecord.

    Args:
        client:     Shared genai.Client.
        model_name: Model string e.g. "gemini-2.5-flash".
        metadata:   Parsed filing metadata.

    Returns:
        FinancialResultRecord on success, None on failure.
    """
    t0 = time.time()
    logger.info(
        "[Results/Gemini] Analyzing: %s (%s, %s)",
        metadata.company_name, metadata.exchange, metadata.quarter or "?",
    )

    # 1. Build prompt
    prompt = build_financial_analysis_prompt(metadata)

    # 2. Call Gemini
    result_dict = await _call_gemini(client, model_name, prompt)
    if not result_dict:
        logger.warning(
            "[Results/Gemini] No valid response for %s", metadata.company_name,
        )
        return None

    # 3. Validate
    try:
        analysis = FinancialAnalysisOutput.model_validate(result_dict)
    except Exception as exc:
        logger.exception(
            "[Results/Gemini] Validation failed for %s: %s",
            metadata.company_name, exc,
        )
        return None

    # 4. Derive recommendation
    recommendation = derive_recommendation(analysis.sentiment, analysis.impact)

    # 5. Build record
    now = time.time()

    forecast_short = None
    if analysis.forecast_short_term:
        forecast_short = Forecast(
            direction  = analysis.forecast_short_term.direction,
            confidence = analysis.forecast_short_term.confidence,
            reason     = analysis.forecast_short_term.reason,
        )

    forecast_medium = None
    if analysis.forecast_medium_term:
        forecast_medium = Forecast(
            direction  = analysis.forecast_medium_term.direction,
            confidence = analysis.forecast_medium_term.confidence,
            reason     = analysis.forecast_medium_term.reason,
        )

    record = FinancialResultRecord(
        id                      = metadata.uid,
        # Filing metadata (all from parsed document / RSS — never from Gemini)
        company_name            = metadata.company_name,
        symbol                  = metadata.symbol or "",
        exchange                = metadata.exchange,
        quarter                 = metadata.quarter or "",
        result_date             = analysis.result_date or "",
        announcement_date       = metadata.announcement_date,
        period_start            = metadata.period_start,
        period_end              = metadata.period_end,
        financial_year          = metadata.financial_year,
        standalone_consolidated = metadata.standalone_consolidated,
        filing_type             = metadata.filing_type,
        document_type           = metadata.document_type,
        source_url              = metadata.source_url,
        
        # Extracted Financials (from XBRL/HTML)
        revenue                 = metadata.financials.revenue if metadata.financials else None,
        profit_before_tax       = metadata.financials.profit_before_tax if metadata.financials else None,
        profit_net              = metadata.financials.profit_net if metadata.financials else None,
        basic_eps               = metadata.financials.basic_eps if metadata.financials else None,

        # Non-banking
        depreciation            = metadata.financials.depreciation if metadata.financials else None,

        # Banking
        operating_profit        = metadata.financials.operating_profit if metadata.financials else None,

        # Derived metrics
        ebitda                  = metadata.financials.ebitda if metadata.financials else None,
        ebitda_margin           = metadata.financials.ebitda_margin if metadata.financials else None,
        pat_margin              = metadata.financials.pat_margin if metadata.financials else None,
        operating_profit_margin = metadata.financials.operating_profit_margin if metadata.financials else None,

        # AI analysis (all from Gemini)
        headline                = analysis.headline,
        revenue_change_yoy      = analysis.revenue_change_yoy,
        profit_change_yoy       = analysis.profit_change_yoy,
        eps_change_yoy          = analysis.eps_change_yoy,
        executive_summary       = analysis.executive_summary,
        guidance                = analysis.guidance,
        sentiment               = analysis.sentiment,
        impact                  = analysis.impact,
        forecast_short_term     = forecast_short,
        forecast_medium_term    = forecast_medium,
        source_urls             = analysis.source,
        # Backend-derived
        recommendation          = recommendation,
        # Operational
        gemini_model            = model_name,
        created_at_unix         = now,
        updated_at_unix         = now,
    )

    logger.info(
        "[Results/Gemini] Analysis complete for %s: sentiment=%s impact=%s rec=%s (%.1fs)",
        record.company_name, record.sentiment, record.impact,
        record.recommendation, time.time() - t0,
    )
    return record
