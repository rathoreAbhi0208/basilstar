"""
news/prompts.py
---------------
Prompts and output schemas for the Gemini-powered article generator.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


# ─── Pydantic schema that Gemini must match ─────────────────────────────────

class ArticleOutput(BaseModel):
    headline:                str
    short_summary:           str
    story:                   str
    category:                str
    subcategory:             str
    sentiment:               str
    impact:                  str
    importance_score:        int = Field(ge=0, le=100)
    confidence_score:        int = Field(ge=0, le=100)
    market_impact:           str
    retail_investor_impact:  str
    institutional_impact:    str
    affected_sectors:        list[str]
    affected_companies:      list[str]
    tags:                    list[str]
    primary_entity:          str | None
    entity_type:             str | None
    image_query:             str | None
    image_alt:               str | None


class ArticleListOutput(BaseModel):
    articles: list[ArticleOutput]


# ─── Prompt builder ──────────────────────────────────────────────────────────

def build_enrichment_prompt(
    raw_items_json: str,
    current_time:   str,
) -> str:
    """
    Build the Gemini prompt that takes raw fetched news items and
    turns them into fully-enriched, AI-crafted articles.

    Args:
        raw_items_json: JSON array of RawNewsItem-like dicts.
        current_time:   Current IST datetime string.

    Returns:
        Complete prompt string.
    """
    return f"""You are a Senior Financial Journalist and Market Analyst for a premium Indian financial news platform.
Current IST time: {current_time}

You have been given a batch of raw news items freshly fetched from official Indian financial sources (NSE, BSE, SEBI, RBI, MCA, PIB) and top-tier financial media (Economic Times, Moneycontrol, Mint, Business Standard, CNBC TV18, etc.).

RAW NEWS ITEMS (JSON array):
{raw_items_json}

YOUR TASK:
1. FILTER: Include only items that are genuinely relevant to Indian finance, markets, stocks, IPOs, corporate filings, regulatory changes, economic policy, banking, or commodities. Skip generic/off-topic items.
2. DEDUPLICATE: If multiple raw items cover the same event, merge them into ONE article (use the most authoritative source).
3. ENRICH: For each selected item, produce a rich, publication-quality article with:
   - An eye-catching, factual headline (not clickbait, but compelling)
   - A 2-3 sentence executive summary that hooks the reader
   - A full ~500-word story with sections: Overview, Why It Matters, Market Implications, Impact on Retail Investors, Impact on Institutions, Key Takeaways
   - Accurate classification, sentiment analysis, and impact scoring
   - Extract the `primary_entity` (e.g., Reliance Industries, SEBI, Nifty 50) and `entity_type` (e.g., Company, Regulator, Bank, Index, Commodity).
   - Generate ONE concise `image_query` for stock image providers such as Pexels, Unsplash and Pixabay.

IMAGE QUERY RULES:
- If the article is about a company, return only the official company name.
  Examples: Infosys, Tata Motors, HDFC Bank, Reliance Industries.
- If the article is about RBI, return "Reserve Bank of India".
- If the article is about SEBI, return "Securities and Exchange Board of India".
- If the article is about NSE, return "National Stock Exchange of India".
- If the article is about BSE, return "Bombay Stock Exchange".
- If the article is about an index, return "Nifty 50" or "Sensex".
- If the article is about a commodity, return its common name.
  Examples: Gold, Silver, Crude Oil.
- If there is no specific entity, return a short industry term.
  Examples: Logistics, Banking, Manufacturing, IPO.
- Return only a short searchable phrase (1-5 words).
- Do NOT describe an image.
- Do NOT generate artistic or AI image prompts.
- Do NOT return image URLs.

- Generate `image_alt` as a simple description of the expected image.

HEADLINE RULES:
✓ Good: "SEBI Tightens IPO Disclosure Norms — What It Means for Retail Investors"
✓ Good: "RBI Holds Repo Rate at 6.5%: Borrowers Get Relief, Markets Rally"
✗ Bad: "SEBI makes announcement" (too vague)
✗ Bad: "SHOCKING: Market crashes 90%" (clickbait)

STORY RULES:
- Write in a professional, engaging tone — think Bloomberg or Financial Times quality
- Include specific numbers, percentages, and dates from the source
- Explain the "so what" for retail investors in plain language
- End with clear Key Takeaways (3-5 bullet points as part of the story text)
- Minimum 400 words per story

CATEGORIES (use exactly one): IPO, Equity, Mutual Funds, Economy, Commodities, Forex, Banking, Corporate, Policy, Taxation, Startup, Cryptocurrency, International, Technology, Results, Earnings, Dividend, Bonus, Rights Issue, Regulation
SENTIMENT (use exactly one): Positive, Negative, Neutral, Mixed
IMPACT (use exactly one): Low, Medium, High, Critical

CRITICAL RULES:
- Return ONLY valid JSON — no markdown, no code fences, no prose outside JSON
- Do NOT fabricate facts, numbers, or quotes not present in source material
- Provide a concise `image_query` suitable for stock image providers like Pexels/Unsplash.

SCORING RULES (STRICT)

importance_score:
- Must be an INTEGER only.
- Allowed range: 0 to 100.
- Do NOT return decimals.
- Examples: 15, 40, 72, 95, 100

confidence_score:
- Must be an INTEGER only.
- Allowed range: 0 to 100.
- Represents confidence as a percentage.
- Do NOT return decimal values such as 0.95, 0.87, 0.5 or 1.0.
- Correct examples: 95, 87, 50, 100
- Incorrect examples: 0.95, 0.87, 0.5, 1.0

JSON SCHEMA (return exactly this structure):
{{
  "articles": [
    {{
      "headline": "string (max 120 chars)",
      "short_summary": "string (2-3 sentences)",
      "story": "string (~500 words with structured sections)",
      "category": "string",
      "subcategory": "string",
      "sentiment": "Positive|Negative|Neutral|Mixed",
      "impact": "Low|Medium|High|Critical",
      "importance_score": 92,
      "confidence_score": 96,
      "market_impact": "string (1-2 sentences)",
      "retail_investor_impact": "string (1-2 sentences)",
      "institutional_impact": "string (1-2 sentences)",
      "affected_sectors": ["string"],
      "affected_companies": ["string"],
      "tags": ["string"],
      "primary_entity": "string or null",
      "entity_type": "string or null",
      "image_query": "string or null",
      "image_alt": "description of the image or null"
    }}
  ]
}}

All string values must be properly JSON-escaped. Generate between 5 and {MAX_ARTICLES} articles."""


MAX_ARTICLES = 15


def build_standalone_prompt(current_time: str, last_run_time: str | None) -> str:
    """
    Fallback prompt when RSS fetcher yields nothing useful.
    Instructs Gemini to search the web itself for latest Indian financial news.
    """
    since_clause = ""
    if last_run_time:
        since_clause = (
            f"\nCRITICAL: Only generate articles for events that occurred "
            f"AFTER {last_run_time}. If there are no new events, return "
            f'{{\"articles\": []}}.'
        )

    return f"""You are a Senior Financial Journalist for a premium Indian financial news platform.
Current IST time: {current_time}.{since_clause}

Use your web search capability to find the LATEST breaking news from:

TIER-1 SOURCES (always check first):
- NSE India (nseindia.com) — circulars, new listings, corporate actions
- BSE India (bseindia.com) — announcements, filings
- SEBI (sebi.gov.in) — orders, circulars, press releases
- RBI (rbi.org.in) — monetary policy, notifications
- Ministry of Finance (finmin.nic.in) — budget, GST, taxation
- MCA (mca.gov.in) — corporate filings, insolvency

TIER-2 SOURCES (cross-reference):
- Economic Times Markets, Moneycontrol, Mint, Business Standard,
  CNBC TV18, Financial Express, The Hindu BusinessLine

TOPICS TO COVER:
- IPOs (new filings, allotments, listings, GMP)
- Market indices (Nifty, Sensex, Bank Nifty movements with reasons)
- RBI / SEBI regulatory actions
- Corporate earnings, results, dividends
- FII/DII flows
- Rupee, crude oil, gold
- Government economic policies
- Major corporate actions (mergers, acquisitions, buybacks)

Generate {MAX_ARTICLES} high-quality articles following this EXACT JSON schema:
{{
  "articles": [
    {{
      "headline": "string (max 120 chars, compelling but factual)",
      "short_summary": "string (2-3 sentence hook)",
      "story": "string (min 400 words with Overview/Why It Matters/Market Impact/Key Takeaways sections)",
      "category": "IPO|Equity|Mutual Funds|Economy|Commodities|Forex|Banking|Corporate|Policy|Taxation|Startup|Cryptocurrency|International|Technology|Results|Earnings|Dividend|Bonus|Rights Issue|Regulation",
      "subcategory": "string",
      "sentiment": "Positive|Negative|Neutral|Mixed",
      "impact": "Low|Medium|High|Critical",
      "importance_score": 0,
      "confidence_score": 0,
      "market_impact": "string",
      "retail_investor_impact": "string",
      "institutional_impact": "string",
      "affected_sectors": ["string"],
      "affected_companies": ["string"],
      "tags": ["string"],
      "primary_entity": "string or null",
      "entity_type": "string or null",
      "image_query": "string or null",
      "image_alt": "description of the image or null"
    }}
  ]
}}

Return ONLY valid JSON. No markdown, no code fences."""
