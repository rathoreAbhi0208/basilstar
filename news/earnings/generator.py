"""
news/earnings/generator.py
---------------------------
Two-stage Earnings Intelligence Engine.

Earnings Stage 1 — Financial Data Collection
    _collect_financial_data()   → raw dict (validated into FinancialDataCollection)

Earnings Stage 2 — Professional Financial Analysis
    _run_financial_analysis()   → raw dict (validated into EarningsAnalysis)

Public entry point
    generate_earnings_report()  → full two-stage pipeline → NewsArticle

Reuses shared infrastructure from news/generator.py:
    _call_gemini(), _strip_markdown(), _parse_response()

Design rules
~~~~~~~~~~~~
• Stage 1 uses Google Search grounding (use_search=True).
• Stage 2 does NOT use Google Search (use_search=False) — analyses Stage 1 JSON only.
• Models mirror the LLM JSON schemas field-for-field (see .models), so both stage
  outputs are validated with a single `model_validate()` call — no manual
  sub-model reconstruction.
• Never modifies or imports from the regular news pipeline's article generation.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time

from google import genai
from google.genai import types
from pydantic import ValidationError

from ..config import settings, current_ist
from ..generator import _call_gemini, _strip_markdown, _parse_response
from ..models import EvaluatedItem, NewsArticle

from .models import (
    EarningsAnalysis,
    EarningsReport,
    FinancialDataCollection,
)
from .prompts import (
    build_earnings_data_collection_prompt,
    build_earnings_analysis_prompt,
    build_earnings_gap_fill_prompt,
)

logger = logging.getLogger(__name__)


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _make_earnings_id(company: str, quarter: str, fiscal_year: str) -> str:
    """Stable SHA-256 ID for an earnings report."""
    key = f"{company}:{quarter}:{fiscal_year}".strip().lower()
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


async def _gemini_call_raw(
    client:           genai.Client,
    model_name:       str,
    prompt:           str,
    use_search:       bool = False,
    use_url_context:  bool = False,
    max_output_tokens: int = 8192,
) -> str:
    """Raw Gemini call that returns the text response (not parsed JSON).

    Used because earnings prompts return nested objects, not arrays,
    so _call_gemini's top_key list extraction doesn't apply here.

    use_url_context: attaches the URL Context tool alongside Google Search
    grounding. Without this, the model can only find data that happens to
    surface in a generic web search — it has no way to actually open and
    read the specific source_url (e.g. the exchange filing / press release)
    embedded in the prompt, which is often where the freshest, most specific
    numbers actually live. Combining both tools lets Gemini fetch that exact
    page AND search further for anything the page doesn't cover.
    """
    tools = []
    if use_search:
        tools.append(types.Tool(google_search=types.GoogleSearch()))
    if use_url_context:
        tools.append(types.Tool(url_context=types.UrlContext()))

    try:
        response = await client.aio.models.generate_content(
            model    = model_name,
            contents = prompt,
            config   = types.GenerateContentConfig(
                temperature       = settings.temperature,
                top_p             = settings.top_p,
                tools             = tools,
                max_output_tokens = max_output_tokens,
            ),
        )
    except Exception as exc:
        logger.exception("[Earnings/Gemini] API error: %s", exc)
        return ""

    # ── Diagnostics: log WHY data might be incomplete, don't just guess later ──
    try:
        candidate     = response.candidates[0]
        finish_reason = getattr(candidate, "finish_reason", None)
        if finish_reason and str(finish_reason) not in ("STOP", "FinishReason.STOP"):
            logger.warning("[Earnings/Gemini] Non-STOP finish_reason=%s (output may be truncated/incomplete)", finish_reason)

        grounding = getattr(candidate, "grounding_metadata", None)
        if grounding is not None:
            queries = getattr(grounding, "web_search_queries", None) or []
            urls_read = getattr(grounding, "url_context_metadata", None)
            logger.info(
                "[Earnings/Gemini] Grounding used %d search queries: %s | url_context: %s",
                len(queries), queries, bool(urls_read),
            )
    except Exception:
        pass  # diagnostics must never break the actual call

    return (response.text or "").strip()


def _safe_parse_json(raw_text: str) -> dict:
    """Parse Gemini JSON response, stripping markdown fences."""
    cleaned = _strip_markdown(raw_text)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except Exception:
                pass
    logger.error("[Earnings] JSON parse failed. Snippet: %.500s", raw_text[:500])
    return {}


# ─── Gap-fill merge helper ────────────────────────────────────────────────────

def _fill_nulls(base: dict, patch: dict) -> dict:
    """Recursively fill None/missing leaf values in `base` with values from
    `patch`. Never overwrites a value `base` already has — this only fills
    gaps, so a worse gap-fill pass can't clobber a good first-pass value.
    """
    if not isinstance(base, dict) or not isinstance(patch, dict):
        return base
    for key, patch_val in patch.items():
        base_val = base.get(key)
        if isinstance(base_val, dict) and isinstance(patch_val, dict):
            _fill_nulls(base_val, patch_val)
        elif isinstance(base_val, list) and isinstance(patch_val, list):
            # Only replace a list if the original is empty or entirely nulls
            # (e.g. peer_comparison: [{"company_name": null, ...}]).
            base_is_empty = not base_val or all(
                (not v) or (isinstance(v, dict) and all(x is None for x in v.values()))
                for v in base_val
            )
            if base_is_empty and patch_val:
                base[key] = patch_val
        elif base_val is None and patch_val is not None:
            base[key] = patch_val
    return base


# ─── Stage 1: Financial Data Collection ──────────────────────────────────────

async def _collect_financial_data(
    client:       genai.Client,
    model_name:   str,
    company_name: str,
    headline:     str,
    summary:      str,
    source_url:   str,
) -> dict:
    """
    Earnings Stage 1: collect structured financial data via Gemini + Google
    Search + URL Context, with one targeted gap-fill retry if the model's own
    self-reported completeness is low.

    Returns the raw `financial_data` dict (unvalidated) — callers should
    validate it into FinancialDataCollection before persisting.
    """
    t0 = time.time()
    current_time = current_ist().strftime("%Y-%m-%d %H:%M:%S IST")

    prompt = build_earnings_data_collection_prompt(
        company_name = company_name,
        headline     = headline,
        summary      = summary,
        source_url   = source_url,
        current_time = current_time,
    )

    raw_text = await _gemini_call_raw(
        client, model_name, prompt, use_search=True, use_url_context=True
    )
    if not raw_text:
        logger.warning("[Earnings] Empty Stage 1 response for %s", company_name)
        return {}

    parsed = _safe_parse_json(raw_text)
    raw_data = parsed.get("financial_data", parsed)

    # ── Gap-fill retry ──────────────────────────────────────────────────────
    # The schema already asks the model to self-report `missing_fields` and
    # `data_completeness_pct` — previously nothing consumed them. One extra
    # targeted call (only when needed) meaningfully raises fill rates without
    # doubling cost on items that were already complete.
    completeness  = raw_data.get("data_completeness_pct")
    missing_fields = raw_data.get("missing_fields") or []
    try:
        completeness = float(completeness) if completeness is not None else None
    except (TypeError, ValueError):
        completeness = None

    needs_gap_fill = bool(missing_fields) and (
        completeness is None or completeness < settings.earnings_min_completeness_pct
    )

    if needs_gap_fill:
        logger.info(
            "[Earnings] Gap-fill pass for %s: completeness=%s, %d missing fields",
            company_name, completeness, len(missing_fields),
        )
        gap_prompt = build_earnings_gap_fill_prompt(
            company_name    = company_name,
            source_url      = source_url,
            known_data_json = json.dumps(raw_data, ensure_ascii=False),
            missing_fields  = missing_fields,
            current_time    = current_time,
        )
        gap_text = await _gemini_call_raw(
            client, model_name, gap_prompt, use_search=True, use_url_context=True
        )
        if gap_text:
            gap_parsed = _safe_parse_json(gap_text)
            gap_data = gap_parsed.get("financial_data", gap_parsed)
            raw_data = _fill_nulls(raw_data, gap_data)
        else:
            logger.warning("[Earnings] Gap-fill pass returned empty response for %s", company_name)

    logger.info(
        "[Earnings] Stage 1 (Collection) completed for %s in %.1fs",
        company_name, time.time() - t0
    )
    return raw_data

# ─── Stage 2: Professional Financial Analysis ──────────────────────────────────

async def _run_financial_analysis(
    client:       genai.Client,
    model_name:   str,
    company_name: str,
    raw_data:     dict,
) -> dict:
    """
    Earnings Stage 2: analyze the collected data and produce the final intelligence report.

    Returns the raw `earnings_analysis` dict (unvalidated) — callers should
    validate it into EarningsAnalysis before persisting.
    """
    t0 = time.time()
    current_time = current_ist().strftime("%Y-%m-%d %H:%M:%S IST")

    financial_data_json = json.dumps(raw_data, indent=2)

    prompt = build_earnings_analysis_prompt(
        company_name        = company_name,
        financial_data_json = financial_data_json,
        current_time        = current_time,
    )

    raw_text = await _gemini_call_raw(client, model_name, prompt, use_search=False)
    if not raw_text:
        logger.warning("[Earnings] Empty Stage 2 response for %s", company_name)
        return {}

    parsed = _safe_parse_json(raw_text)
    analysis = parsed.get("earnings_analysis", parsed)

    logger.info(
        "[Earnings] Stage 2 (Analysis) completed for %s in %.1fs",
        company_name, time.time() - t0
    )
    return analysis


# ─── Build EarningsReport from Stage 1 + Stage 2 output ─────────────────────

def _build_report(
    analysis:     dict,
    raw_data:     dict,
    company_name: str,
) -> EarningsReport | None:
    """
    Construct a validated EarningsReport from Stage 1 raw data + Stage 2 analysis.

    Both stage outputs are validated directly with `model_validate()` since the
    models mirror the LLM JSON schemas field-for-field — no manual sub-model
    reconstruction needed.
    """
    try:
        financial_data_obj = FinancialDataCollection.model_validate(raw_data)
    except ValidationError as exc:
        logger.error("[Earnings] Stage 1 data failed validation for %s: %s", company_name, exc)
        return None

    try:
        analysis_obj = EarningsAnalysis.model_validate(analysis)
    except ValidationError as exc:
        logger.error("[Earnings] Stage 2 analysis failed validation for %s: %s", company_name, exc)
        return None

    return EarningsReport(
        financial_data = financial_data_obj,
        analysis       = analysis_obj,
        generated_at   = current_ist().isoformat(),
    )


# ─── Public entry point ──────────────────────────────────────────────────────

async def generate_earnings_report(
    client:     genai.Client,
    model_name: str,
    item:       EvaluatedItem,
) -> NewsArticle | None:
    """
    Full two-stage Earnings Intelligence Pipeline for a single evaluated item.

    Stage 1: Financial Data Collection (with Google Search grounding)
    Stage 2: Professional Financial Analysis (no external lookup)

    Args:
        client:     Shared Gemini client.
        model_name: Model string (e.g. "gemini-2.5-flash").
        item:       EvaluatedItem that was classified as EARNINGS_RESULT.

    Returns:
        NewsArticle wrapping the EarningsReport, or None on failure.
    """
    t0 = time.time()

    # Extract company name from Stage 1 evaluation
    company_name = (
        item.evaluation.affected_companies[0]
        if item.evaluation.affected_companies
        else item.raw.title.split(" ")[0]  # fallback: first word of headline
    )

    logger.info(
        "[Earnings] Starting pipeline for '%s' (uid=%s)",
        company_name, item.raw.uid,
    )

    # ── Earnings Pipeline Stage 1: Data Collection ─────────────────────────
    raw_data = await _collect_financial_data(
        client       = client,
        model_name   = model_name,
        company_name = company_name,
        headline     = item.raw.title,
        summary      = item.raw.summary or item.evaluation.executive_summary,
        source_url   = item.raw.url,
    )

    if not raw_data:
        logger.error("[Earnings] Stage 1 failed completely for %s", company_name)
        return None

    # ── Earnings Pipeline Stage 2: Financial Analysis ──────────────────────
    analysis = await _run_financial_analysis(
        client       = client,
        model_name   = model_name,
        company_name = company_name,
        raw_data     = raw_data,
    )

    if not analysis:
        logger.error("[Earnings] Stage 2 failed completely for %s", company_name)
        return None

    # ── Build final report ────────────────────────────────────────────────
    report = _build_report(
        analysis     = analysis,
        raw_data     = raw_data,
        company_name = company_name,
    )

    if not report:
        logger.error("[Earnings] Report construction failed for %s", company_name)
        return None

    quarter     = report.financial_data.quarter
    fiscal_year = report.financial_data.fiscal_year

    report_id = _make_earnings_id(company_name, quarter or "", fiscal_year or "")

    logger.info(
        "[Earnings] Pipeline complete for '%s': recommendation=%s (%.1fs)",
        company_name, report.analysis.recommendation, time.time() - t0,
    )

    earnings_analysis_dump = report.model_dump(
        exclude={"analysis": {"headline", "executive_summary"}}
    )

    return NewsArticle(
        id=report_id,
        headline=report.analysis.headline,
        executive_summary=report.analysis.executive_summary,
        content_type="earnings",
        earnings_analysis=earnings_analysis_dump,
        story="",
        sentiment="Neutral",
        market_impact_level="Medium",
        market_relevance_score=item.evaluation.market_relevance_score,
        confidence_score=report.analysis.confidence,
        time_horizon=item.evaluation.time_horizon,
        market_impact="",
        retail_investor_impact="",
        institutional_impact="",
        trading_implications=None,
        risk_factors=None,
        future_outlook=None,
        affected_sectors=item.evaluation.affected_sectors,
        affected_companies=[company_name],
        market_indices=item.evaluation.market_indices_impact,
        tags=["Earnings", company_name, quarter or "Results"],
        source=item.raw.source_name,
        published_at=item.raw.published_at,
        primary_entity=company_name,
        entity_type="Company",
        event_category="Results",
    )