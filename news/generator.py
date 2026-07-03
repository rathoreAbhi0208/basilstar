"""
news/generator.py
-----------------
Unified two-stage AI generation engine for ALL content pipelines.

Both Google News RSS items AND official exchange/regulator RSS items
(NSE, BSE, SEBI) are processed through the same pipeline.

Stage 1 — Market Intelligence Evaluation
    evaluate_raw_items()     — RawNewsItem list → EvaluatedItem list
    _evaluate_batch()        — single Gemini call for one batch

Filtering
    filter_evaluated_items() — score-based threshold filtering between stages

Stage 2 — Premium Article Generation
    generate_articles_from_evaluated() — EvaluatedItem list → NewsArticle list

Public entry point
    generate_news()          — full two-stage pipeline (or standalone fallback)

Shared infrastructure
    _call_gemini()           — single async Gemini API wrapper
    _strip_markdown()        — markdown fence removal
    _parse_response()        — JSON parsing with automatic recovery
    _raw_items_to_json()     — RawNewsItem → JSON string for Stage 1 prompt
    _evaluated_to_json()     — EvaluatedItem → JSON string for Stage 2 prompt
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time

from google import genai
from google.genai import types

from .config  import settings, current_ist
from .models  import (
    EvaluatedItem,
    EvaluationResult,
    FilterDecision,
    NewsArticle,
    RawNewsItem,
)
from .prompts import (
    ArticleOutput,
    EvaluationOutput,
    build_evaluation_prompt,
    build_article_generation_prompt,
    build_standalone_prompt,
    MAX_ARTICLES,
)

logger = logging.getLogger(__name__)


# ─── Shared helpers ──────────────────────────────────────────────────────────

def _make_id(text: str) -> str:
    """Stable SHA-256 ID from a normalised string."""
    return hashlib.sha256(text.strip().lower().encode("utf-8")).hexdigest()


def _strip_markdown(text: str) -> str:
    """Remove leading/trailing markdown code fences from Gemini responses."""
    text = text.strip()
    text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
    text = re.sub(r"\n?```$", "", text)
    return text.strip()


def _parse_response(raw_text: str, top_key: str = "articles") -> list[dict]:
    """Parse Gemini JSON response and return the list under *top_key*.

    Returns [] on any parse failure (logged at exception level).
    """
    raw_text = _strip_markdown(raw_text)
    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        m = re.search(r"\{.*\}", raw_text, re.DOTALL)
        if m:
            try:
                parsed = json.loads(m.group())
            except Exception as inner:
                logger.exception("[Gemini] JSON recovery failed: %s | snippet: %.300s", inner, raw_text)
                return []
        else:
            logger.exception("[Gemini] JSON parse failed: %s | snippet: %.300s", exc, raw_text)
            return []
    return parsed.get(top_key, [])


async def _call_gemini(
    client:     genai.Client,
    model_name: str,
    prompt:     str,
    use_search: bool = False,
    top_key:    str  = "articles",
) -> list[dict]:
    """Single Gemini API call shared by all pipeline stages.

    Args:
        client:     Shared genai.Client.
        model_name: Model string e.g. "gemini-2.5-flash".
        prompt:     Complete prompt string.
        use_search: Attach Google Search grounding tool.
        top_key:    Top-level JSON key in the response.

    Returns:
        List of raw dicts, or [] on any error.
    """
    tools = [types.Tool(google_search=types.GoogleSearch())] if use_search else []

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
        logger.exception("[Gemini] API error: %s", exc)
        return []

    raw_text = (response.text or "").strip()
    if not raw_text:
        logger.warning("[Gemini] Empty response (top_key=%s)", top_key)
        return []

    return _parse_response(raw_text, top_key=top_key)


def _raw_items_to_json(items: list[RawNewsItem]) -> str:
    """Serialise RawNewsItem list → compact JSON for Gemini prompts."""
    return json.dumps(
        [
            {
                "uid":          item.uid,
                "source_name":  item.source_name,
                "source_tier":  item.source_tier,
                "title":        item.title,
                "url":          item.url,
                "summary":      item.summary,
                "published_at": item.published_at,
                "category":     item.category,
            }
            for item in items
        ],
        ensure_ascii=False,
        indent=2,
    )


def _evaluated_to_json(items: list[EvaluatedItem]) -> str:
    """Serialise EvaluatedItem list → compact JSON for Stage 2 prompt.

    Combines raw news data with Stage 1 evaluation so Stage 2 can reference
    executive_summary, scores, affected entities, and historical context.
    """
    payload = []
    for ei in items:
        payload.append({
            "uid":                    ei.raw.uid,
            "source_name":            ei.raw.source_name,
            "title":                  ei.raw.title,
            "url":                    ei.raw.url,
            "summary":                ei.raw.summary,
            "published_at":           ei.raw.published_at,
            "category":               ei.raw.category,
            # Stage 1 enrichment
            "market_relevance_score": ei.evaluation.market_relevance_score,
            "confidence_score":       ei.evaluation.confidence_score,
            "event_category":         ei.evaluation.event_category,
            "executive_summary":      ei.evaluation.executive_summary,
            "market_indices_impact":  ei.evaluation.market_indices_impact,
            "stage1_affected_companies": ei.evaluation.affected_companies,
            "stage1_affected_sectors":   ei.evaluation.affected_sectors,
        })
    return json.dumps(payload, ensure_ascii=False, indent=2)


# ─── Filtering ───────────────────────────────────────────────────────────────

def filter_evaluated_items(
    items:            list[EvaluatedItem],
    high_threshold:   int  = 80,
    medium_threshold: int  = 60,
    generate_medium:  bool = True,
) -> tuple[list[EvaluatedItem], dict[str, int], list[EvaluatedItem]]:
    """Apply Stage 1 score thresholds to decide which items proceed to Stage 2.

    Args:
        items:            All evaluated items from Stage 1.
        high_threshold:   Scores >= this → always generate (FilterDecision.GENERATE).
        medium_threshold: Scores >= this but < high → FilterDecision.MAYBE.
        generate_medium:  If True, MAYBE items are also sent to Stage 2.

    Returns:
        Tuple of:
            • List of EvaluatedItem that will proceed to Stage 2.
            • Stats dict with keys: total, high, medium, low, passed.
            • List of ALL EvaluatedItems (updated with their FilterDecision).
    """
    stats = {"total": len(items), "high": 0, "medium": 0, "low": 0, "passed": 0}
    passed:      list[EvaluatedItem] = []
    updated_all: list[EvaluatedItem] = []

    for item in items:
        score = item.evaluation.market_relevance_score
        if score >= high_threshold:
            decision = FilterDecision.GENERATE
            stats["high"] += 1
        elif score >= medium_threshold:
            decision = FilterDecision.MAYBE
            stats["medium"] += 1
        else:
            decision = FilterDecision.DISCARD
            stats["low"] += 1

        # Mutate decision field (EvaluatedItem is a Pydantic model — use model_copy)
        updated = item.model_copy(update={"decision": decision.value})
        updated_all.append(updated)

        if decision == FilterDecision.GENERATE or (
            decision == FilterDecision.MAYBE and generate_medium
        ):
            passed.append(updated)
            stats["passed"] += 1
        else:
            logger.debug(
                "[Filter] DISCARDED '%s' (score=%d, threshold=%d)",
                item.raw.title[:60], score, medium_threshold,
            )

    logger.info(
        "[Filter] Stage 1 results: total=%d high=%d medium=%d low=%d passed=%d",
        stats["total"], stats["high"], stats["medium"], stats["low"], stats["passed"],
    )
    return passed, stats, updated_all


# ─── Stage 1: Market Intelligence Evaluation ─────────────────────────────────

def _dict_to_evaluation(d: dict) -> EvaluationResult | None:
    """Validate a Gemini dict as an EvaluationResult."""
    try:
        out = EvaluationOutput.model_validate(d)
        return EvaluationResult(
            uid                    = out.uid,
            market_relevance_score = out.market_relevance_score,
            confidence_score       = out.confidence_score,
            time_horizon           = out.time_horizon,
            reason                 = out.reason,
            event_category         = out.event_category,
            executive_summary      = out.executive_summary,
            market_indices_impact  = out.market_indices_impact,
            affected_companies     = out.affected_companies,
            affected_sectors       = out.affected_sectors,
        )
    except Exception as exc:
        logger.exception("[Gemini/Stage1] Validation failed for uid '%s': %s", d.get("uid", "?"), exc)
        return None


async def _evaluate_batch(
    client:     genai.Client,
    model_name: str,
    batch:      list[RawNewsItem],
    current_time: str,
    batch_num:  int,
) -> list[EvaluationResult]:
    """Run one Stage 1 Gemini call for a batch of raw items."""
    prompt = build_evaluation_prompt(
        raw_items_json = _raw_items_to_json(batch),
        current_time   = current_time,
    )
    dicts = await _call_gemini(client, model_name, prompt, use_search=True, top_key="evaluations")
    logger.info("[Gemini/Stage1] Batch %d → %d evaluations", batch_num, len(dicts))

    results: list[EvaluationResult] = []
    for d in dicts:
        ev = _dict_to_evaluation(d)
        if ev:
            results.append(ev)
    return results


async def evaluate_raw_items(
    client:      genai.Client,
    model_name:  str,
    raw_items:   list[RawNewsItem],
) -> list[EvaluatedItem]:
    """
    Stage 1: RawNewsItem list → EvaluatedItem list.

    Each item is evaluated for market relevance, scored, and enriched with
    an executive summary and historical context via Google Search grounding.

    Args:
        client:     Shared Gemini client.
        model_name: Model string.
        raw_items:  Fresh RSS items from the fetcher.

    Returns:
        List of EvaluatedItem (one per raw item that Gemini returned).
        Items Gemini omits (malformed) are silently skipped and logged.
    """
    if not raw_items:
        logger.info("[Gemini/Stage1] No raw items — skipping evaluation")
        return []

    t0           = time.time()
    current_time = current_ist().strftime("%Y-%m-%d %H:%M:%S IST")
    batch_size   = settings.stage1_batch_size

    logger.info("[Gemini/Stage1] Evaluating %d raw items (batch_size=%d)", len(raw_items), batch_size)

    # Build uid → RawNewsItem map for correlation
    uid_map: dict[str, RawNewsItem] = {item.uid: item for item in raw_items}
    all_evaluations: list[EvaluationResult] = []

    for i, batch_start in enumerate(range(0, len(raw_items), batch_size)):
        batch = raw_items[batch_start : batch_start + batch_size]
        evals = await _evaluate_batch(client, model_name, batch, current_time, batch_num=i + 1)
        all_evaluations.extend(evals)

    # Correlate evaluations back to raw items
    evaluated: list[EvaluatedItem] = []
    for ev in all_evaluations:
        raw = uid_map.get(ev.uid)
        if raw is None:
            # Gemini returned a uid we don't recognise — try to match by index
            logger.warning("[Gemini/Stage1] Unknown uid '%s' in evaluation — skipping", ev.uid)
            continue
        evaluated.append(
            EvaluatedItem(
                raw        = raw,
                evaluation = ev,
                decision   = FilterDecision.GENERATE.value,  # placeholder; set by filter step
            )
        )

    logger.info(
        "[Gemini/Stage1] Evaluation complete: %d/%d items correlated in %.1fs",
        len(evaluated), len(raw_items), time.time() - t0,
    )
    return evaluated


# ─── Stage 2: Premium Article Generation ─────────────────────────────────────

def _dict_to_article(d: dict, uid_map: dict[str, EvaluatedItem]) -> NewsArticle | None:
    """Validate a Gemini dict and construct a NewsArticle, carrying Stage 1 data."""
    try:
        item = ArticleOutput.model_validate(d)
    except Exception as exc:
        logger.exception("[Gemini/Stage2] Validation failed for '%s': %s", d.get("headline", "?"), exc)
        return None

    # Retrieve Stage 1 data for traceability.
    # source is always set to the RSS feed name (e.g. "Google News Finance", "NSE", "SEBI")
    # carried from RawNewsItem.source_name — NOT the article source Gemini may infer.
    evaluated = uid_map.get(item.uid or "")
    market_relevance = evaluated.evaluation.market_relevance_score if evaluated else 0
    event_category   = evaluated.evaluation.event_category         if evaluated else None
    market_indices   = (
        evaluated.evaluation.market_indices_impact
        if evaluated else item.market_indices or []
    )
    # RSS feed name: "Google News Finance", "NSE", "BSE", "SEBI", etc.
    rss_source = evaluated.raw.source_name if evaluated else item.source

    # Resolve published_at
    pub_at = evaluated.raw.published_at if evaluated else item.published_at

    return NewsArticle(
        id                     = _make_id(item.headline),
        headline               = item.headline,
        executive_summary      = item.executive_summary,
        story                  = item.story,
        sentiment              = item.sentiment,
        market_impact_level    = item.market_impact_level,
        market_relevance_score = market_relevance,
        confidence_score       = item.confidence_score,
        market_impact          = item.market_impact,
        retail_investor_impact = item.retail_investor_impact,
        institutional_impact   = item.institutional_impact,
        trading_implications   = item.trading_implications,
        risk_factors           = item.risk_factors,
        future_outlook         = item.future_outlook,
        affected_sectors       = item.affected_sectors,
        affected_companies     = item.affected_companies,
        market_indices         = market_indices,
        tags                   = item.tags,
        time_horizon           = evaluated.evaluation.time_horizon if evaluated else "both",
        source                 = rss_source,
        published_at           = pub_at,
        primary_entity         = item.primary_entity,
        entity_type            = item.entity_type,
        image_query            = item.image_query,
        image_url              = None,
        image_alt              = item.image_alt,
        event_category         = event_category,
    )


async def generate_articles_from_evaluated(
    client:          genai.Client,
    model_name:      str,
    evaluated_items: list[EvaluatedItem],
) -> list[NewsArticle]:
    """
    Stage 2: EvaluatedItem list → NewsArticle list.

    All items here have already passed Stage 1 filtering.
    Gemini writes premium articles enriched with Stage 1 context.

    Args:
        client:          Shared Gemini client.
        model_name:      Model string.
        evaluated_items: Items that passed Stage 1 filtering.

    Returns:
        Deduplicated list of NewsArticle (image_url not yet set).
    """
    if not evaluated_items:
        logger.info("[Gemini/Stage2] No items to generate articles for")
        return []

    t0           = time.time()
    current_time = current_ist().strftime("%Y-%m-%d %H:%M:%S IST")
    batch_size   = settings.stage2_batch_size

    logger.info("[Gemini/Stage2] Generating articles for %d items (batch_size=%d)", len(evaluated_items), batch_size)

    # uid → EvaluatedItem for Stage 1 score lookup
    uid_map: dict[str, EvaluatedItem] = {ei.raw.uid: ei for ei in evaluated_items}
    all_dicts: list[dict] = []

    for i, batch_start in enumerate(range(0, len(evaluated_items), batch_size)):
        batch  = evaluated_items[batch_start : batch_start + batch_size]
        prompt = build_article_generation_prompt(
            evaluated_items_json = _evaluated_to_json(batch),
            current_time         = current_time,
        )
        dicts = await _call_gemini(client, model_name, prompt, use_search=True, top_key="articles")
        logger.info("[Gemini/Stage2] Batch %d → %d articles", i + 1, len(dicts))
        all_dicts.extend(dicts)

    articles:  list[NewsArticle] = []
    seen_ids:  set[str]          = set()
    failed = 0

    for d in all_dicts:
        art = _dict_to_article(d, uid_map)
        if art is None:
            failed += 1
            continue
        if art.id in seen_ids:
            continue
        seen_ids.add(art.id)
        articles.append(art)

    logger.info(
        "[Gemini/Stage2] Produced %d articles (failed=%d) in %.1fs",
        len(articles), failed, time.time() - t0,
    )
    return articles


# ─── News pipeline (public interface) ────────────────────────────────────────

async def generate_news(
    client:        genai.Client,
    model_name:    str,
    raw_items:     list[RawNewsItem] | None = None,
    last_run_time: str | None = None,
) -> tuple[list[NewsArticle], dict[str, int], list[EvaluatedItem]]:
    """
    Full two-stage News pipeline: RawNewsItem list → NewsArticle list.

    Stage 1: evaluate_raw_items()
    Filter: filter_evaluated_items()
    Stage 2: generate_articles_from_evaluated()

    Falls back to standalone mode (single Gemini call) when raw_items is empty.

    Args:
        client:        Shared Gemini client.
        model_name:    Model string.
        raw_items:     Fresh RSS items. If empty/None, falls back to standalone mode.
        last_run_time: IST timestamp of last run (used in standalone dedup).

    Returns:
        Tuple of (articles list, pipeline stats dict, evaluated_items list).
    """
    stats: dict[str, int] = {
        "stage1_evaluated": 0,
        "stage1_passed":    0,
        "stage2_generated": 0,
    }

    if not raw_items:
        # ── Standalone fallback ───────────────────────────────────────────────
        logger.info("[News] Standalone mode (no raw items from RSS)")
        current_time = current_ist().strftime("%Y-%m-%d %H:%M:%S IST")
        prompt = build_standalone_prompt(current_time, last_run_time)
        dicts  = await _call_gemini(client, model_name, prompt, use_search=True, top_key="articles")

        articles:  list[NewsArticle] = []
        seen_ids:  set[str]          = set()
        for d in dicts:
            art = _dict_to_article(d, uid_map={})
            if art and art.id not in seen_ids:
                seen_ids.add(art.id)
                articles.append(art)

        stats["stage2_generated"] = len(articles)
        return articles, stats, []

    # ── Stage 1: Evaluate ─────────────────────────────────────────────────────
    evaluated_all = await evaluate_raw_items(client, model_name, raw_items)
    stats["stage1_evaluated"] = len(evaluated_all)

    # ── Filter ────────────────────────────────────────────────────────────────
    passed, filter_stats, evaluated_all_updated = filter_evaluated_items(
        items            = evaluated_all,
        high_threshold   = settings.stage1_high_threshold,
        medium_threshold = settings.stage1_medium_threshold,
        generate_medium  = settings.stage1_generate_medium,
    )
    stats["stage1_passed"] = filter_stats["passed"]

    if not passed:
        logger.info("[News] No items passed Stage 1 filtering — no articles generated")
        return [], stats, evaluated_all_updated

    # ── Stage 2: Generate ─────────────────────────────────────────────────────
    articles = await generate_articles_from_evaluated(client, model_name, passed)
    stats["stage2_generated"] = len(articles)

    return articles, stats, evaluated_all_updated


