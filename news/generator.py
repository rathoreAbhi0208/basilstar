"""
news/generator.py
-----------------
AI enrichment pipeline: RawNewsItem → NewsArticle

Two modes:
  A) Enrichment mode feed real raw items from the RSS fetcher to Gemini.
  B) Standalone mode Gemini searches the web itself (fallback when RSS empty).

Guarantees:
  • Every returned article has a unique ID (sha256 of headline).
  • Articles that fail Pydantic validation are logged and skipped.
  • Gemini response is cleaned of markdown fences before JSON parsing.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from datetime import datetime, timezone

from google import genai
from google.genai import types

from .config  import settings, current_ist
from .fetcher import RawNewsItem
from .models  import NewsArticle
from .prompts import (
    ArticleOutput,
    build_enrichment_prompt,
    build_standalone_prompt,
    MAX_ARTICLES,
)

logger = logging.getLogger(__name__)

_BATCH_SIZE = 20   # max raw items per Gemini call (token budget)


def _make_article_id(headline: str) -> str:
    """Stable, collision-resistant ID."""
    raw = f"{headline.strip().lower()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _strip_markdown(text: str) -> str:
    """Remove leading/trailing markdown code fences."""
    text = text.strip()
    text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
    text = re.sub(r"\n?```$", "", text)
    return text.strip()


def _parse_response(raw_text: str) -> list[dict]:
    """Parse and validate Gemini JSON response."""
    raw_text = _strip_markdown(raw_text)
    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        # Attempt to recover: find first '{' and last '}'
        m = re.search(r'\{.*\}', raw_text, re.DOTALL)
        if m:
            try:
                parsed = json.loads(m.group())
            except Exception as exc:
                logger.exception("[Gemini] JSON parse failed: %s | snippet: %s", exc, raw_text[:300])
                return []
        else:
            logger.exception("[Gemini] JSON parse failed: %s | snippet: %s", exc, raw_text[:300])
            return []
    return parsed.get("articles", [])


async def _call_gemini(
    client:     genai.Client,
    model_name: str,
    prompt:     str,
    use_search: bool = False,
) -> list[dict]:
    """Single Gemini call, returns parsed article dicts."""
    tools = [types.Tool(google_search=types.GoogleSearch())] if use_search else []

    try:
        logger.info("client=%r", client)
        logger.info("client.aio=%r", getattr(client, "aio", None))
        logger.info("model=%s", model_name)
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
        logger.exception("[Gemini] Gemini API error: %s", exc)
        return []

    raw_text = (response.text or "").strip()
    if not raw_text:
        logger.warning("[Gemini] Empty Gemini response")
        return []

    return _parse_response(raw_text)


def _raw_items_to_json(items: list[RawNewsItem]) -> str:
    """Serialise raw items for the Gemini prompt."""
    return json.dumps(
        [
            {
                "source_name":  item.source_name,
                "source_tier":  item.source_tier,
                "title":        item.title,
                "url":          item.url,
                "summary":      item.summary,
                "published_at": item.published_at.isoformat(),
                "category":     item.category,
            }
            for item in items
        ],
        ensure_ascii=False,
        indent=2,
    )


def _dict_to_article(
    item_dict:    dict,
) -> NewsArticle | None:
    """Validate and convert a raw dict to a NewsArticle."""
    try:
        item = ArticleOutput.model_validate(item_dict)
    except Exception as exc:
        headline = item_dict.get("headline", "unknown")
        logger.exception("[Gemini] Failed to summarize '%s': %s", headline, exc)
        return None

    art_id = _make_article_id(item.headline)

    return NewsArticle(
        id                   = art_id,
        headline             = item.headline,
        short_summary        = item.short_summary,
        story                = item.story,
        category             = item.category,
        subcategory          = item.subcategory,
        sentiment            = item.sentiment,
        impact               = item.impact,
        importance_score     = item.importance_score,
        confidence_score     = item.confidence_score,
        market_impact        = item.market_impact,
        retail_investor_impact = item.retail_investor_impact,
        institutional_impact = item.institutional_impact,
        affected_sectors     = item.affected_sectors,
        affected_companies   = item.affected_companies,
        tags                 = item.tags,
        image_url            = item.image_url,
        image_alt            = item.image_alt,
    )


async def generate_news(
    client:        genai.Client,
    model_name:    str,
    raw_items:     list[RawNewsItem] | None = None,
    last_run_time: str | None = None,
) -> list[NewsArticle]:
    """
    Main entry point.

    Args:
        client:        Initialised Gemini client.
        model_name:    Model string e.g. "gemini-2.5-flash".
        raw_items:     Fresh items from the RSS fetcher.
                       If None or empty, falls back to standalone/web-search mode.
        last_run_time: IST timestamp of last successful run (for dedup hint).

    Returns:
        List of unique NewsArticle objects (no images yet).
    """
    t0           = time.time()
    current_time = current_ist().strftime("%Y-%m-%d %H:%M:%S IST")

    article_dicts: list[dict] = []

    if raw_items:
        # ── Mode A: Enrich real fetched items ────────────────────────────
        logger.info("[Gemini] Starting article generation (%d articles)", len(raw_items))

        # Process in batches to stay within Gemini token limits
        for batch_start in range(0, len(raw_items), _BATCH_SIZE):
            batch = raw_items[batch_start : batch_start + _BATCH_SIZE]
            prompt = build_enrichment_prompt(
                raw_items_json = _raw_items_to_json(batch),
                current_time   = current_time,
            )
            logger.info("[Gemini] Request sent")
            dicts = await _call_gemini(client, model_name, prompt, use_search=True)
            logger.info("[Gemini] Response received")
            logger.info(
                "[Gemini] Batch %d/%d completed",
                batch_start // _BATCH_SIZE + 1,
                (len(raw_items) + _BATCH_SIZE - 1) // _BATCH_SIZE,
            )
            article_dicts.extend(dicts)

        # Map generated articles back to source items by position (best-effort)
        source_map: dict[int, RawNewsItem] = {
            i: raw_items[i] for i in range(len(raw_items))
        }
    else:
        # ── Mode B: Standalone (Gemini web search) ───────────────────────
        logger.info("[Gemini] Starting standalone article generation")
        prompt      = build_standalone_prompt(current_time, last_run_time)
        logger.info("[Gemini] Request sent")
        article_dicts = await _call_gemini(client, model_name, prompt, use_search=True)
        logger.info("[Gemini] Response received")
        source_map  = {}

    logger.info("[Gemini] Total dicts from Gemini: %d", len(article_dicts))

    # ── Validate and build NewsArticle objects ───────────────────────────
    articles:   list[NewsArticle] = []
    seen_ids:   set[str]          = set()
    failed = 0

    for idx, item_dict in enumerate(article_dicts):
        art = _dict_to_article(item_dict)
        if art is None:
            failed += 1
            continue

        if art.id in seen_ids:
            logger.debug("[Gemini] Skipping duplicate id=%s", art.id[:12])
            continue

        seen_ids.add(art.id)
        articles.append(art)

    elapsed = time.time() - t0
    logger.info("[Parser] Parsed %d articles (failed %d)", len(articles), failed)
    return articles
