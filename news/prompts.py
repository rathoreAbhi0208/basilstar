"""
Two-stage Gemini prompt system for the Market Intelligence Pipeline.

Stage 1 — Market Intelligence Evaluation
    build_evaluation_prompt()   → per-item scoring, executive summary, filtering signal

Stage 2 — Premium Article Generation
    build_article_generation_prompt()  → full article from pre-evaluated items

Legacy prompts (OI pipeline, standalone) are preserved at the bottom.

NOTE ON FILTERING:
Stage 1 no longer emits a `should_generate_article` boolean. Gemini returns a
calibrated `market_relevance_score` only; the generate/discard decision is made
downstream in the pipeline against settings.stage1_high_threshold /
settings.stage1_medium_threshold / settings.stage1_generate_medium
(see news/config.py). This avoids two independent, potentially conflicting
gates (a hardcoded prompt rule vs. tunable config).
"""
from __future__ import annotations

from pydantic import BaseModel, Field


# ═══════════════════════════════════════════════════════════════════════════════
# STAGE 1 SCHEMAS
# ═══════════════════════════════════════════════════════════════════════════════

class EvaluationOutput(BaseModel):
    """Gemini output schema for one evaluated RSS item (Stage 1)."""
    uid:                     str
    market_relevance_score:  int   = Field(ge=0, le=100)
    confidence_score:        int   = Field(ge=0, le=100)
    reason:                  str
    event_category:          str
    classification:          str   = Field(
        default="REGULAR_NEWS",
        description="REGULAR_NEWS or EARNINGS_RESULT",
    )
    time_horizon:            str   # "short_term_catalyst" | "long_term_structural" | "both"
    executive_summary:       str
    market_indices_impact:   list[str]
    affected_companies:      list[str]
    affected_sectors:        list[str]


class EvaluationBatchOutput(BaseModel):
    evaluations: list[EvaluationOutput]


# ═══════════════════════════════════════════════════════════════════════════════
# STAGE 2 SCHEMAS
# ═══════════════════════════════════════════════════════════════════════════════

class ArticleOutput(BaseModel):
    """Gemini output schema for one generated article (Stage 2)."""
    uid:                     str          # mirrors EvaluatedItem uid for correlation
    headline:                str
    executive_summary:       str          # refined from Stage 1
    story:                   str
    sentiment:                str
    market_impact_level:     str
    confidence_score:        int = Field(ge=0, le=100)
    published_at:            str | None = None
    market_impact:           str
    retail_investor_impact:  str
    institutional_impact:    str
    trading_implications:    str | None = None
    risk_factors:            str | None = None
    future_outlook:          str | None = None
    affected_sectors:        list[str]
    affected_companies:      list[str]
    market_indices:          list[str]
    tags:                    list[str]
    source:                  str | None = None
    primary_entity:          str | None = None
    entity_type:             str | None = None
    image_query:              str | None = None
    image_alt:                str | None = None


class ArticleListOutput(BaseModel):
    articles: list[ArticleOutput]


MAX_ARTICLES = 15


# ═══════════════════════════════════════════════════════════════════════════════
# STAGE 1 PROMPT — v3: compact, multi-persona trading-utility scoring + dedup
# ═══════════════════════════════════════════════════════════════════════════════

_STAGE1_CORE_RULES = """\
SCORING PHILOSOPHY:
Score "how much this changes trader/investor action," NOT "how prestigious the
source is." Official sources (RBI/SEBI/NSE/BSE/filings) raise CONFIDENCE, never
RELEVANCE by themselves. A verified-but-inconsequential circular stays low
relevance; an unofficial-but-material, verifiable development stays high.

AUDIENCE (item must serve >=1; serving more personas can raise the score):
intraday (same-day catalysts, liquidity triggers) | swing/days-weeks (momentum
shifts, event setups) | positional/weeks-months (trend-confirming/reversing
guidance, ratings, policy) | long-term investors (structural/valuation facts:
earnings quality, capital allocation, governance, moats) | portfolio managers
(sector/index/risk/flow implications) | research analysts (verifiable numbers
for models).

HIGH-VALUE CATEGORIES (prioritize, but always judge the actual numbers, not
just category membership): earnings & guidance | dividends/bonus/splits/
buybacks/rights | IPO/QIP/preferential allotment | M&A/demerger/stake sale/JV |
order wins/major contracts | promoter stake & insider moves | block/bulk deals
(named party + price/volume) | credit rating actions | SEBI/NSE/BSE actions,
circulars, penalties | index add/remove/reweight | FII/DII flows | RBI policy
(rates, CRR/SLR, liquidity) | macro (CPI/WPI/GDP/IIP/PMI/trade) | commodity/FX
moves hitting margins or trade economics | governance events (management
change, litigation, fraud, auditor exit).

market_relevance_score: INTEGER 0-100, weighed holistically across six
dimensions (no fixed formula) — `reason` MUST name the dimension(s) that
drove the score:
  1. ACTIONABILITY - names specific stock/sector/index with clear direction;
     has concrete parameters (price/qty/ratio/%/date/deal size); reader
     doesn't need extra research to know what to do.
  2. MATERIALITY - beats/misses consensus, reverses trend, new risk/opportunity,
     changes earnings/margin/growth trajectory. Routine/expected = low,
     regardless of company size; a real small/mid-cap surprise can outrank a
     non-event at a large-cap.
  3. SPECIFICITY & VERIFIABILITY - hard numbers/named counterparties/dates/
     filing refs = high; generic/unnamed/speculative = low regardless of
     source prestige. Verify via search grounding; unverifiable != score boost.
  4. TIMELINESS - breaking/same-day beats stale/rehashed; if grounding shows
     the market already digested it (multi-day-old, price adjusted), score down.
  5. SCOPE OF IMPACT - a MULTIPLIER, not standalone: index/sector/macro breadth
     amplifies an already-actionable+material story, doesn't substitute for it.
  6. LONG-TERM VALUATION IMPACT - structural shifts (capacity, M&A, regulatory
     regime, capital allocation, governance/fraud) that move fair value/margin/
     growth over quarters-years score high here even with muted same-day move.

Sanity check before finalizing (reason silently, don't output as fields): Would
this be in a professional morning briefing? Does it change earnings/valuation/
strategy for a covered stock/sector? If both "no" -> score well under 50
regardless of source prestige.

SCORE BANDS (guideline; content quality can shift an item across bands):
  90-100  Decisively actionable + material + specific + fresh. Morning-briefing
          lead: confirmed RBI rate move, earnings beat/miss vs. consensus with
          numbers, signed M&A with terms, index reconstitution w/ dates,
          trading halt, large confirmed order win changing revenue visibility.
  75-89   Clearly actionable/material for a specific stock/sector, any market
          cap, WITH concrete numbers: named block/bulk deal, rating change,
          surprise management exit, promoter stake change w/ figures, guidance
          revision, quantified sector circular.
  55-74   Moderately useful: relevant to >=1 persona, real data, but narrower/
          partly anticipated/moderate materiality: in-line results, routine
          non-trivial filings, FII/DII flows without a dramatic swing.
  35-54   Low-moderate: tangential, informational, thin direct trading value:
          minor procedural filings, reiterated guidance w/ no new number, soft
          commentary w/ no data, early-stage/rumored items.
  0-34    Not trading-useful: PR/marketing copy, stale/priced-in news, generic
          education, admin notices (holidays, portal maintenance), vague
          "markets may see volatility" with no actionable detail.

ANTI-PATTERNS - always score LOW, regardless of source/company size:
  promotional PR with no new fact | restated known info, no new number |
  opinion/forecast with no data | headline implies news but body has no detail
  | official-but-procedural item with no quantified market/compliance impact.

ANTI-PATTERNS - do NOT under-score for being small/unofficial:
  small-cap/SME with a material, specific, fresh block deal / order win /
  promoter pledge-unpledge / rating change / related-party txn w/ figures ->
  55+ minimum, often 75+ | narrow sector notices with compliance deadlines,
  penalties, or quantified cost impact are actionable, not "just procedural."

confidence_score: INTEGER 0-100 - confidence that (a) facts are verified via
search grounding and (b) the relevance score correctly applies the criteria
above. Official/verifiable sources raise THIS score, never relevance directly.

All scores are INTEGERS (85, never 0.85).
"""

_STAGE1_DEDUP_RULES = """\
DEDUPLICATION (mandatory - the same real-world event often arrives from
multiple RSS sources with different titles/wording):
1. Within this batch, compare items by underlying substance (entity + event
   type + key facts/numbers/dates), not by title text. Group items that
   describe the SAME real-world event, even if titles/sources/wording differ.
2. In each duplicate group, pick ONE primary item: the most complete/specific/
   verifiable version (prefer named source, exact numbers, official filing
   language). Set "is_duplicate": false and "duplicate_of": null for it.
3. For every other item in the group, set "is_duplicate": true and
   "duplicate_of": "<uid of the primary item>". Still score it normally and
   fill all fields - downstream decides whether to drop it.
4. Items with no duplicates in this batch: "is_duplicate": false,
   "duplicate_of": null.
5. ALWAYS produce "dedup_signature": a short, normalized, machine-comparable
   string so downstream code can also catch duplicates ACROSS separate
   batches/runs without another LLM call. Format:
     "<PRIMARY_ENTITY_UPPER>|<EVENT_CATEGORY>|<KEY_FACT>|<YYYY-MM-DD>"
   - PRIMARY_ENTITY_UPPER: main company/index/regulator, uppercase, no suffixes
     like "Ltd"/"Limited" (e.g. "RELIANCE INDUSTRIES", "RBI", "NIFTY 50").
   - EVENT_CATEGORY: same value as the event_category field.
   - KEY_FACT: the single most identifying number/term (deal size, rate %,
     order value, rating grade, stake %) or "NA" if none exists.
   - Date: the event date (not the article publish time) in YYYY-MM-DD; use
     the publish date if no other date is stated.
   Two items about the same event should produce an IDENTICAL signature even
   if their titles differ completely.
"""

_STAGE1_EXECUTIVE_SUMMARY_RULES = """\
executive_summary field: a SHORT GIST ONLY - max 40 words, one paragraph.
State what happened (with the key number/term) and why it's material enough
to justify the score. This is NOT the reader-facing summary - Stage 2 owns
the full, polished, publication-ready summary for whichever items survive
filtering, so do not write multi-paragraph prose or do extra research-heavy
context-building here. Do not just repeat the headline.
"""

_STAGE1_EVENT_CATEGORIES = """\
EVENT CATEGORIES (single best match): Results | Guidance Revision | Dividend |
Bonus | Stock Split | Buyback | Rights Issue | IPO | QIP | Merger |
Acquisition | Demerger | Joint Venture | Order Win | Contract Announcement |
Promoter Stake Change | Insider Trading | SEBI Circular | NSE Circular |
BSE Notice | RBI Policy | Repo Rate | Inflation | GDP | IIP | PMI | Trade
Data | Budget | Crude Oil | Commodity | Currency | FII | DII | Block Deal |
Bulk Deal | Credit Rating | Corporate Governance | Regulatory Action |
Management Change | Litigation | Bankruptcy | Legal Action | Index Change |
Other
"""

_STAGE1_TIME_HORIZON_RULES = """\
time_horizon: exactly one of "short_term_catalyst" (tradeable this week,
limited multi-quarter impact - e.g. block deal, short-term guidance beat),
"long_term_structural" (changes multi-quarter/year thesis, muted immediate
price impact - e.g. capacity expansion, governance change, regulatory regime
shift), or "both" (material now AND structurally - e.g. large confirmed M&A,
RBI policy shift with immediate + lasting effect).
"""

_STAGE1_CLASSIFICATION_RULES = """\
CLASSIFICATION (mandatory, exactly one):
- EARNINGS_RESULT: ONLY actual reported financial results - Quarterly/Annual/
  FY Results, Financial/Earnings Release, Standalone/Consolidated Results,
  Investor Presentation containing published financials.
- REGULAR_NEWS: everything else, INCLUDING items that look earnings-adjacent
  but aren't actual reported numbers - Board Meeting Notice/to Consider
  Results, Earnings Schedule, Investor/Analyst Meeting, Trading Window
  Closure, Record Date/AGM Notice, pre-results Conference Call Schedule,
  Dividend Meeting, Results Date Announcement, pre-results analyst estimates,
  "stock rises after results" market-reaction pieces, guidance revisions
  without actual reported numbers.
Regular news -> article pipeline. Earnings results -> financial analysis pipeline.
"""


def build_evaluation_prompt(raw_items_json: str, current_time: str) -> str:
    """
    Stage 1 prompt: evaluate each RSS item for trading/investment usefulness
    and flag cross-source duplicates. Gemini acts as a Senior Market
    Intelligence Analyst for a multi-persona trading audience. It must NOT
    generate articles - only evaluate, score, dedup, and summarise.

    Args:
        raw_items_json: JSON array of raw news items (title, url, summary, uid, source_name).
        current_time:   Current IST datetime string.

    Returns:
        Complete prompt string.
    """
    item_count = raw_items_json.count('"uid"')
    return f"""You are a Senior Market Intelligence Analyst for a premium Indian financial intelligence platform serving active traders and investors.
Current IST time: {current_time}

RAW NEWS ITEMS (JSON array - each item has a "uid" field you MUST preserve):
{raw_items_json}

TASK - for EACH item:
1. Use Google Search grounding to verify facts against the original source.
2. Score trading/investment usefulness per the rules below.
3. Assess probable impact on: Nifty 500, Nifty 50, Sensex, Bank Nifty, Indian
   equity market broadly, individual listed companies, sectors, Indian economy.
4. Detect event category, classification, and time horizon.
5. Detect duplicates against other items in this batch (see DEDUPLICATION).
6. Write the executive_summary.

DO NOT generate full articles - evaluation only.

{_STAGE1_CORE_RULES}

{_STAGE1_DEDUP_RULES}

{_STAGE1_EXECUTIVE_SUMMARY_RULES}

{_STAGE1_EVENT_CATEGORIES}

{_STAGE1_TIME_HORIZON_RULES}

{_STAGE1_CLASSIFICATION_RULES}

reason: one sentence, max 25 words, names the driving dimension(s)
(actionability/materiality/specificity/timeliness/scope/long-term valuation).
Do not just restate the headline.

Return exactly this JSON structure - no markdown, no code fences, no prose outside JSON:
{{
  "evaluations": [
    {{
      "uid": "exact uid from the input item",
      "market_relevance_score": 85,
      "confidence_score": 90,
      "reason": "string (one sentence, names the driving dimension(s))",
      "event_category": "string from the category list",
      "classification": "REGULAR_NEWS",
      "time_horizon": "short_term_catalyst | long_term_structural | both",
      "is_duplicate": false,
      "duplicate_of": null,
      "dedup_signature": "ENTITY|EVENT_CATEGORY|KEY_FACT|YYYY-MM-DD",
      "executive_summary": "string (short gist, max 40 words)",
      "market_indices_impact": ["Nifty 50", "Bank Nifty"],
      "affected_companies": ["string"],
      "affected_sectors": ["string"]
    }}
  ]
}}

Process ALL {item_count} items. Return ONLY valid JSON."""

# ═══════════════════════════════════════════════════════════════════════════════
# STAGE 2 PROMPT — v3
# ═══════════════════════════════════════════════════════════════════════════════

_STAGE2_LENGTH_RULES = """\
LENGTH & NO-DUPLICATION RULES (STRICT - this is what keeps the read under
30-60 seconds):
- executive_summary: the PRIMARY read. 3 short paragraphs, blank-line
  separated, <=90 words total.
- story: SUPPORTING detail only, <=150 words, exactly two sections:
    ## Overview - 2-3 sentences adding color beyond the executive_summary
      (do not restate it verbatim).
    ## Key Takeaways - 3 bullet points, each one short line.
  Do NOT add sections that duplicate a dedicated field below (no "Trading
  Implications", "Risk Factors", "Future Outlook", "Retail/Institutional
  Impact", or "Sector Analysis" headers in story - that content belongs only
  in its own field).
- market_impact, retail_investor_impact, institutional_impact,
  trading_implications, risk_factors, future_outlook: ONE sentence each,
  <=25 words. Use null if genuinely not applicable/material for this item -
  do not pad with generic filler to fill the field.
- Every sentence must earn its place: cut hedge phrases, throat-clearing, and
  restated headlines.
"""

_STAGE2_IMAGE_RULES = """\
image_query: 1-5 words, no sentences/prompts/URLs.
  Company -> official name ("Infosys"). RBI/SEBI/NSE/BSE -> full official
  name. Index -> "Nifty 50" / "Sensex". Commodity -> common name ("Gold").
  No specific entity -> short industry term ("Banking", "IPO").
image_alt: one concise sentence describing the expected image.
"""

_STAGE2_ANTI_HALLUCINATION = """\
ACCURACY & CONSISTENCY RULES (STRICT):
- Every fact/number/date/claim must come from the source item or the Stage 1
  evaluation. If a detail isn't available, write generally rather than
  inventing a figure - Stage 1 already did the verification; do not re-search
  for new facts, only use what's provided.
- sentiment, market_impact_level, and tone must stay consistent with Stage
  1's market_relevance_score, reason, and time_horizon - do not contradict it.
- time_horizon "long_term_structural" -> lean the summary's risk/outlook
  sentence toward the structural thesis; "short_term_catalyst" -> lean toward
  near-term trading; "both" -> balance in one sentence.
- Phrase uncertain implications as "could/may/signals," never as fact.
"""


def build_article_generation_prompt(
    evaluated_items_json: str,
    current_time: str,
) -> str:
    """
    Stage 2 prompt: generate short, high-signal financial articles (~30-60s
    read) from pre-evaluated items.

    Every item passed here has ALREADY been evaluated and filtered in Stage 1.
    Stage 2 assumes all inputs are market-relevant. No filtering needed here.

    Args:
        evaluated_items_json: JSON array of EvaluatedItem-like dicts containing
                              raw item data + Stage 1 evaluation results.
        current_time:         Current IST datetime string.

    Returns:
        Complete prompt string.
    """
    return f"""You are a Senior Financial Journalist for a premium Indian financial news platform serving active traders and investors.
Current IST time: {current_time}

You have been given a batch of pre-evaluated news items, each already confirmed useful for
trading/investment decisions by a Senior Market Intelligence Analyst (Stage 1). Do NOT
re-evaluate, re-score, or filter - write a short, high-signal article for EVERY item.

Readers should get everything they need in 30-60 seconds. Precision and brevity beat
length - cut anything that isn't a fact, number, or actionable point.

PRE-EVALUATED ITEMS (JSON array - each has "uid", raw news data, and Stage 1 evaluation
including market_relevance_score, reason, event_category, time_horizon, and a short gist):
{evaluated_items_json}

FOR EACH ITEM PRODUCE:

1. HEADLINE: factual, max 120 chars, leads with the concrete fact.
   Good: "HDFC Bank Q1 Results Beat Estimates - Net Profit Rises 18% YoY"
   Bad:  "HDFC Bank makes announcement" (vague) | "SHOCKING: Bank crashes" (clickbait)

2. EXECUTIVE SUMMARY (the primary, reader-facing text - see length rules):
     P1 - What happened, with the specific numbers/terms.
     P2 - Why it matters (materiality) + time horizon in one line.
     P3 - Actionable conclusion: what to watch, what it implies for positioning.

3. STORY (short supporting detail - see length rules): ## Overview, ## Key Takeaways.

4. All remaining fields per the schema below - each a single scannable sentence.

WRITING STANDARDS:
- Wire-service precision: specific numbers/%/dates from the source, zero filler sentences.
- Plain language for retail-relevant fields; institutional-grade precision in
  trading_implications/institutional_impact.
- No hedge language ("markets may react") without a specific driver, level, or scenario.

SENTIMENT: Positive | Negative | Neutral | Mixed
MARKET_IMPACT_LEVEL: Low | Medium | High | Critical

{_STAGE2_LENGTH_RULES}

{_STAGE2_IMAGE_RULES}

{_STAGE2_ANTI_HALLUCINATION}

confidence_score: INTEGER 0-100, confidence in content accuracy (grounding in
source/Stage 1 data), not the event's importance. Integers only, never decimals.

Return exactly this JSON structure - no markdown, no code fences, no prose outside JSON:
{{
  "articles": [
    {{
      "uid": "exact uid from the input item",
      "headline": "string (max 120 chars)",
      "executive_summary": "Paragraph 1\\n\\nParagraph 2\\n\\nParagraph 3",
      "story": "string (<=150 words: ## Overview + ## Key Takeaways only)",
      "sentiment": "Positive|Negative|Neutral|Mixed",
      "market_impact_level": "Low|Medium|High|Critical",
      "confidence_score": 90,
      "market_impact": "string (1 sentence, <=25 words)",
      "retail_investor_impact": "string (1 sentence, <=25 words)",
      "institutional_impact": "string (1 sentence, <=25 words)",
      "trading_implications": "string (1 sentence, <=25 words) or null",
      "risk_factors": "string (1 sentence, <=25 words) or null",
      "future_outlook": "string (1 sentence, <=25 words) or null",
      "affected_sectors": ["string"],
      "affected_companies": ["string"],
      "market_indices": ["Nifty 50", "Sensex"],
      "tags": ["string"],
      "source": "source_name from raw input",
      "primary_entity": "string or null",
      "entity_type": "string or null",
      "image_query": "string or null",
      "image_alt": "string or null"
    }}
  ]
}}

Return ONLY valid JSON. No markdown. No prose outside JSON."""


# ═══════════════════════════════════════════════════════════════════════════════
# STANDALONE FALLBACK PROMPT — v3: self-filtering, same short-read standard
# ═══════════════════════════════════════════════════════════════════════════════

_STANDALONE_RELEVANCE_BAR = """\
RELEVANCE BAR (STRICT):
Fallback mode has no pre-filtering - apply the platform's normal trading-
utility standard yourself. Only select events that are:
- ACTIONABLE: specific stock/sector/index, clear direction, concrete numbers/
  dates/terms.
- MATERIAL: genuine beat/miss, trend change, new risk/opportunity, or
  structural development - not routine or already-priced-in.
- VERIFIABLE: corroborated by >=1 credible source via web search, specific
  facts, not vague narrative.
- FRESH: breaking or same-day, not rehashed coverage of an old event.

Prioritize: earnings & guidance, dividends/bonus/buybacks/rights issues,
IPOs/QIPs, M&A, large order wins, promoter stake changes, block/bulk deals,
credit rating changes, SEBI/NSE/BSE actions, index changes, FII/DII flows,
RBI policy, key macro data (CPI/GDP/IIP/PMI), commodity/currency moves with a
clear market linkage.

Do NOT fill remaining slots with low-value filler to hit {max_articles}. If
fewer stories genuinely qualify, return fewer. Quality over quantity.
"""

_STANDALONE_ANTI_HALLUCINATION = """\
ACCURACY RULES (STRICT):
- Every number/date/name/claim must come from a source found via web search -
  never invent or estimate.
- If a detail can't be verified with reasonable confidence, omit it.
- Phrase uncertain implications as "could/may/signals," never as fact.
- If sources conflict on a material fact, use the most authoritative one
  (exchange filing, regulator site, company press release) - do not average
  or guess.
"""


def build_standalone_prompt(current_time: str, last_run_time: str | None) -> str:
    """
    Fallback prompt when the RSS fetcher yields nothing useful.

    Unlike the two-stage pipeline, this mode has no pre-filtering — Gemini must
    both discover AND evaluate relevance in a single pass via web search. It
    applies the same trading-utility bar as Stage 1, and the same short-read
    standard as Stage 2, to avoid flooding the feed with low-value or bloated
    filler content just to hit MAX_ARTICLES.

    Args:
        current_time:  Current IST datetime string.
        last_run_time: ISO datetime string of the last successful run, or None.

    Returns:
        Complete prompt string.
    """
    since_clause = ""
    if last_run_time:
        since_clause = (
            f"\nCRITICAL: Only generate articles for events that occurred "
            f"AFTER {last_run_time}. If there are no qualifying new events, "
            f'return {{"articles": []}}. Do NOT reuse or rephrase older news '
            f"to fill the quota."
        )

    return f"""You are a Senior Financial Journalist for a premium Indian financial news platform serving active traders and investors.
Current IST time: {current_time}.{since_clause}

Use web search to find the LATEST breaking Indian financial news from credible sources:
NSE (nseindia.com), BSE (bseindia.com), SEBI (sebi.gov.in), RBI (rbi.org.in),
Economic Times, Moneycontrol, Mint, Business Standard, CNBC TV18.

{_STANDALONE_RELEVANCE_BAR.format(max_articles=MAX_ARTICLES)}

Readers should get everything they need in 30-60 seconds - for each qualifying story:

1. HEADLINE: factual, max 120 chars, leads with the concrete fact, not hype.

2. EXECUTIVE SUMMARY (primary read, <=90 words total, 3 short paragraphs):
     P1 - What happened, with specific numbers/terms.
     P2 - Why it matters (materiality) + time horizon in one line.
     P3 - Actionable conclusion: what to watch, what it implies for positioning.

3. STORY (supporting detail only, <=150 words, exactly): ## Overview (2-3
   sentences, no restating the summary) + ## Key Takeaways (3 short bullets).
   No duplicate "Trading Implications"/"Risk Factors"/"Impact" headers - that
   content belongs only in its own field below.

4. market_impact, retail_investor_impact, institutional_impact,
   trading_implications, risk_factors, future_outlook: ONE sentence each,
   <=25 words, null if not materially applicable.

WRITING STANDARDS:
- Wire-service precision: specific numbers/dates, zero filler.
- Plain language for retail-relevant fields; precise for Trading Implications/
  Institutional Impact.

SENTIMENT: Positive | Negative | Neutral | Mixed
MARKET_IMPACT_LEVEL: Low | Medium | High | Critical

{_STANDALONE_ANTI_HALLUCINATION}

IMAGE QUERY RULES (image_query field):
- Company -> official name ("Infosys"). RBI/SEBI/NSE/BSE -> full official name.
- Index -> "Nifty 50"/"Sensex". Commodity -> common name ("Gold").
- No specific entity -> short industry term ("Banking", "IPO").
- 1-5 words only. No artistic prompts, URLs, or full sentences.
image_alt: one concise sentence describing the expected image.

confidence_score: INTEGER 0-100, confidence in verification/accuracy. Integers only.

Generate UP TO {MAX_ARTICLES} qualifying articles (fewer is fine - see relevance bar above).

Return exactly this JSON structure - no markdown, no code fences:
{{
  "articles": [
    {{
      "uid": "generate-standalone-N",
      "headline": "string (max 120 chars)",
      "executive_summary": "Paragraph 1\\n\\nParagraph 2\\n\\nParagraph 3",
      "story": "string (<=150 words: ## Overview + ## Key Takeaways only)",
      "sentiment": "Positive|Negative|Neutral|Mixed",
      "market_impact_level": "Low|Medium|High|Critical",
      "confidence_score": 0,
      "published_at": "string (ISO 8601 or null)",
      "market_impact": "string (1 sentence, <=25 words)",
      "retail_investor_impact": "string (1 sentence, <=25 words)",
      "institutional_impact": "string (1 sentence, <=25 words)",
      "trading_implications": "string (1 sentence, <=25 words) or null",
      "risk_factors": "string (1 sentence, <=25 words) or null",
      "future_outlook": "string (1 sentence, <=25 words) or null",
      "affected_sectors": ["string"],
      "affected_companies": ["string"],
      "market_indices": ["string"],
      "tags": ["string"],
      "source": "string",
      "primary_entity": "string or null",
      "entity_type": "string or null",
      "image_query": "string or null",
      "image_alt": "string or null"
    }}
  ]
}}

Return ONLY valid JSON, no markdown, no code fences."""


# ═══════════════════════════════════════════════════════════════════════════════
# LEGACY ENRICHMENT PROMPT (kept for backward compatibility reference)
# ═══════════════════════════════════════════════════════════════════════════════

def build_enrichment_prompt(raw_items_json: str, current_time: str) -> str:
    """
    DEPRECATED: Single-stage enrichment prompt. Do not use in new code paths.

    Preserved only for backward-compatibility reference and any external
    callers not yet migrated. The two-stage pipeline (build_evaluation_prompt()
    + build_article_generation_prompt()) supersedes this and applies the
    trading-utility scoring/filtering this function skips entirely.

    Calling this directly means NO Stage 1 relevance filtering occurs — every
    item passed in will get a full article regardless of trading usefulness.
    """
    return build_article_generation_prompt(raw_items_json, current_time)