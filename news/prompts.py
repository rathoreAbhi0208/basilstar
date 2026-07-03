"""
news/prompts.py
---------------
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
# STAGE 1 PROMPT — v3: multi-persona, multi-dimensional trading-utility scoring
# ═══════════════════════════════════════════════════════════════════════════════

_STAGE1_PERSONAS = """\
TARGET AUDIENCE — score for ALL of these personas, not just one:
- Intraday traders: need same-day price-moving catalysts, liquidity/volume triggers.
- Swing traders (days-to-weeks): need momentum shifts, technical/fundamental
  inflection points, event-driven setups (results, order wins, rating actions).
- Positional traders (weeks-to-months): need trend-confirming or trend-reversing
  developments — guidance changes, sector re-ratings, policy shifts.
- Long-term investors: need structural, fundamental, valuation-altering information —
  earnings quality, capital allocation (buybacks/dividends/bonus), governance, moats.
- Portfolio managers: need portfolio-level and sector/index-level implications,
  correlation/risk factors, allocation-relevant macro and flow data.
- Research analysts: need verifiable facts and numbers suitable for building or
  revising models and estimates.

An item does not need to matter to ALL personas to score high — it needs to be
GENUINELY decision-relevant to AT LEAST ONE, and the more personas it serves,
the higher it can justifiably score.
"""

_STAGE1_HIGH_VALUE_EVENTS = """\
HIGH-VALUE EVENT TYPES — actively prioritize these categories, but do NOT
auto-inflate the score just because an item falls into one of them. Always
evaluate the actual numbers/terms present:
Earnings results & guidance changes | Dividends, bonus issues, splits, buybacks,
rights issues | IPOs, QIPs, preferential allotments | Mergers, acquisitions,
demergers, stake sales, JVs | Large order wins/losses, major contracts |
Promoter stake changes (pledge/unpledge/buy/sell), insider disclosures |
Block deals and bulk deals with named parties and price/volume | Credit rating
changes | SEBI/NSE/BSE regulatory actions, circulars, penalties | Index
additions/removals/weight changes | FII/DII flow data | RBI policy decisions
(rates, CRR/SLR, liquidity) | Macro data (CPI/WPI, GDP, IIP, PMI, trade data) |
Commodity/currency moves affecting margins or export/import economics |
Governance events: management changes, litigation, fraud allegations, auditor
resignations.
"""

_STAGE1_SCORING_RULES = f"""\
SCORING PHILOSOPHY:
This platform exists ONLY for active traders and investors in Indian equity
markets. Do not score "how prestigious is this entity/source" — score "how
much does this change what a trader or investor should do." Official-source
status (RBI, SEBI, NSE, BSE, exchange filings) should raise your CONFIDENCE in
the facts, but must NOT automatically inflate the RELEVANCE score. A verified
but inconsequential circular is still low relevance; an unofficial but highly
material and verifiable development is still high relevance.

{_STAGE1_PERSONAS}

{_STAGE1_HIGH_VALUE_EVENTS}

market_relevance_score: INTEGER 0-100, assessed across six dimensions. Weigh
them together holistically (no fixed formula) — but your `reason` must name
the dimension(s) that drove the score.

1. ACTIONABILITY — Can a trader or investor act on this directly? Names
   specific stocks/sectors/indices with a clear directional implication.
   Contains concrete parameters (price, quantity, ratio, %, date, deal size) —
   not just narrative. Does not require reader expertise to know what to do next.

2. MATERIALITY — How much does this change the investment case? Beats/misses
   consensus, reverses a prior trend, introduces new risk/opportunity, changes
   earnings/margin/growth trajectory. Routine/expected events are LOW
   materiality regardless of company size. A genuine surprise at a small/mid-cap
   can outrank a non-event at a large-cap.

3. SPECIFICITY & VERIFIABILITY — Hard numbers, named counterparties, dates,
   filing references = high. Generic statements, unnamed sources, speculation
   with no hard data = low, regardless of source prestige. Use Google Search
   grounding to verify; unverifiable claims must not inflate the score.

4. TIMELINESS — Same-day/breaking news outranks rehashed or stale news. If
   grounding shows the market has already digested and moved past this
   (multi-day-old, price already adjusted), reduce the score even if the
   original event was significant.

5. SCOPE OF IMPACT — Breadth of affected participants/instruments. Acts as a
   MULTIPLIER on the above, not a standalone score. Index-level, sector-wide,
   or macro scope amplifies an already-actionable, material story — it does
   not substitute for actionability/materiality on its own.

6. LONG-TERM VALUATION IMPACT — Does this alter the fundamental thesis?
   Structural developments (capacity expansion, M&A, regulatory regime change,
   capital allocation policy, governance/fraud issues) that shift fair value,
   margins, or growth trajectory over multiple quarters/years score high here
   even if same-day price reaction is muted.

BEFORE ASSIGNING A SCORE, INTERNALLY CHECK (reason through these, do not
output them as separate fields):
   - Would this merit inclusion in a professional trader's morning briefing?
   - Does this change earnings expectations, valuation, or trading strategy
     for any covered stock/sector?
   If the honest answer to both is "no," the score should be well under 50
   regardless of how official or well-sourced the item is.

SCORE BANDS (guideline, not rigid — content quality can move an item across bands):
    90-100 = Decisively actionable AND highly material AND specific AND fresh.
             Would lead a morning briefing. E.g. confirmed RBI rate decision,
             major earnings beat/miss vs consensus with numbers, signed M&A
             with deal terms, index reconstitution with effective dates,
             trading halt/regulatory restriction, large confirmed order win
             that materially changes revenue visibility.
    75-89  = Clearly actionable and material for a specific stock/sector/theme,
             regardless of market cap, PROVIDED concrete numbers/terms are
             present. E.g. mid/small-cap block or bulk deal with named buyer
             and price, credit rating change, unexpected management exit,
             promoter stake change with figures, sector circular with
             quantified compliance cost, guidance revision.
    55-74  = Moderately useful: relevant to at least one persona, real data
             behind it, but narrower in scope, partially anticipated, or
             moderate materiality. In-line quarterly results, routine but
             non-trivial filings, FII/DII flow data without a dramatic swing,
             sector commentary backed by real figures.
    35-54  = Low-moderate: tangentially relevant, mostly informational, thin
             direct trading value. Minor procedural filings, reiterated
             guidance with no new number, soft official commentary without
             new data, early-stage/rumored developments.
    0-34   = Not useful for trading or investment decisions. Pure PR/marketing
             copy, stale or fully-priced-in news, generic educational content,
             administrative notices (holiday calendars, portal maintenance),
             vague "markets may see volatility" pieces with no actionable detail.

ANTI-PATTERNS — score LOW regardless of source prestige or company size:
    - Purely promotional press releases with no new financial/operational fact.
    - Restatement of previously known information with no new number or development.
    - Opinion/forecast pieces with no data backing ("analysts believe markets could rise").
    - Headlines implying news where the verified body contains no concrete detail.
    - Official-source items that are procedural/administrative with no
      quantified market or compliance impact — official does not mean relevant.

ANTI-PATTERNS — do NOT under-score just because the entity is small or unofficial:
    - A small-cap or SME stock with a genuinely material, specific, fresh
      development (block deal, order win, promoter pledge/unpledge, rating
      change, related-party transaction with figures) deserves 55+ minimum,
      often 75+, on the strength of the criteria above.
    - Narrow sector- or theme-specific regulatory notices carrying compliance
      deadlines, penalties, or quantified cost implications are actionable
      and should not be scored low merely for being "procedural."

confidence_score: INTEGER 0-100. Your confidence that (a) the facts are
correctly verified via Google Search grounding, and (b) the assigned
market_relevance_score correctly reflects trading/investment usefulness per
the criteria above. Official, verifiable sources should raise this confidence
score — but must NOT be used to raise the relevance score itself.

- All scores MUST be integers. Never return decimals (e.g. 0.85 is WRONG; use 85).
"""

_STAGE1_EXECUTIVE_SUMMARY_RULES = """\
EXECUTIVE SUMMARY RULES:
Write EXACTLY 4 short paragraphs (separated by a blank line). This must go
beyond a recap — it is the primary decision-support text traders will read.

Paragraph 1 — WHAT HAPPENED: The current event, stated clearly with the
specific numbers/terms involved. No vague language.

Paragraph 2 — HISTORICAL CONTEXT & WHY IT MATTERS: Use Google Search to find:
    * Has this company/entity had similar events before, and how did the
      market react then?
    * Relevant prior quarters' trends, prior policy actions, or comparable
      precedents (e.g. how SEBI/RBI actions of this type have played out).
    * Explain WHY this event matters for the investment case, not just what
      happened.

Paragraph 3 — RISKS, OPPORTUNITIES & TIME HORIZON: Explicitly state:
    * Key risks and key opportunities this creates.
    * Whether this is primarily a SHORT-TERM CATALYST (tradeable this week),
      a LONG-TERM STRUCTURAL development (changes the multi-quarter thesis),
      or both — be explicit about which.

Paragraph 4 — ACTIONABLE CONCLUSION: A clear, concrete takeaway for
traders/investors — what to watch next, what this implies for positioning,
or what would confirm/invalidate the thesis. Avoid hedging filler like
"investors should stay cautious" without specifying what to watch for.

RULES:
- DO NOT simply repeat the headline.
- The summary must stand alone — users should understand the full picture
  and have a clear next step without reading the full article.
- Maximum 160 words total across all 4 paragraphs.
"""

_STAGE1_EVENT_CATEGORIES = """\
EVENT CATEGORIES (pick the single best match):
Results | Guidance Revision | Dividend | Bonus | Stock Split | Buyback |
Rights Issue | IPO | QIP | Merger | Acquisition | Demerger | Joint Venture |
Order Win | Contract Announcement | Promoter Stake Change | Insider Trading |
SEBI Circular | NSE Circular | BSE Notice | RBI Policy | Repo Rate |
Inflation | GDP | IIP | PMI | Trade Data | Budget | Crude Oil | Commodity |
Currency | FII | DII | Block Deal | Bulk Deal | Credit Rating |
Corporate Governance | Regulatory Action | Management Change | Litigation |
Bankruptcy | Legal Action | Index Change | Other
"""

_STAGE1_TIME_HORIZON_RULES = """\
time_horizon field: EXACTLY one of "short_term_catalyst", "long_term_structural",
or "both".
- "short_term_catalyst": primarily tradeable this week; limited multi-quarter
  thesis impact (e.g. a block deal, a short-term guidance beat).
- "long_term_structural": primarily changes the multi-quarter/year investment
  thesis with muted immediate price impact (e.g. capacity expansion, governance
  change, regulatory regime shift).
- "both": material in the near term AND changes the structural thesis (e.g. a
  large confirmed M&A, an RBI policy shift with immediate and lasting effects).
"""


def build_evaluation_prompt(raw_items_json: str, current_time: str) -> str:
    """
    Stage 1 prompt: evaluate each RSS item for trading/investment usefulness.

    Gemini acts as a Senior Market Intelligence Analyst evaluating for a
    multi-persona trading audience (intraday/swing/positional traders,
    long-term investors, portfolio managers, research analysts).
    It must NOT generate articles — only evaluate, score, and summarise.

    Args:
        raw_items_json: JSON array of raw news items (title, url, summary, uid, source_name).
        current_time:   Current IST datetime string.

    Returns:
        Complete prompt string.
    """
    return f"""You are a Senior Market Intelligence Analyst for a premium Indian financial intelligence platform serving active traders and investors.
Current IST time: {current_time}

You have been given a batch of raw news items freshly fetched from RSS feeds covering Indian financial markets.

RAW NEWS ITEMS (JSON array — each item has a "uid" field you MUST preserve):
{raw_items_json}

YOUR TASK:
For EACH item in the array, you must:
1. Use Google Search grounding to verify the information and read the original source.
2. Determine whether the event is genuinely useful for trading/investment decisions
   (see scoring philosophy, personas, and dimensions below).
3. Assess probable impact on:
   - Nifty 500, Nifty 50, Sensex, Bank Nifty
   - Indian Equity Market broadly
   - Individual listed companies
   - Market sectors
   - Indian Economy
4. Detect the event category.
5. Classify the time horizon (short-term catalyst vs. long-term structural vs. both).
6. Write an executive_summary (see rules below).
7. Assign scores.

DO NOT generate full articles. This is an evaluation stage only.

{_STAGE1_SCORING_RULES}

{_STAGE1_EXECUTIVE_SUMMARY_RULES}

{_STAGE1_EVENT_CATEGORIES}

{_STAGE1_TIME_HORIZON_RULES}

reason: One concise sentence (max 25 words) that explicitly names which
scoring dimension(s) — actionability / materiality / specificity / timeliness /
scope of impact / long-term valuation impact — drove the score. Avoid
restating the headline as the reason.

JSON SCHEMA (return exactly this structure — no markdown, no code fences):
{{
  "evaluations": [
    {{
      "uid": "exact uid from the input item",
      "market_relevance_score": 85,
      "confidence_score": 90,
      "reason": "string (one sentence, names the driving dimension(s))",
      "event_category": "string from the category list",
      "time_horizon": "short_term_catalyst | long_term_structural | both",
      "executive_summary": "Paragraph 1\\n\\nParagraph 2\\n\\nParagraph 3\\n\\nParagraph 4",
      "market_indices_impact": ["Nifty 50", "Bank Nifty"],
      "affected_companies": ["string"],
      "affected_sectors": ["string"]
    }}
  ]
}}

Return ONLY valid JSON. No markdown. No prose outside JSON.
Process ALL {len(raw_items_json.split('"uid"')) - 1} items."""


# ═══════════════════════════════════════════════════════════════════════════════
# STAGE 2 PROMPT — v2: production-grade article generation
# ═══════════════════════════════════════════════════════════════════════════════

_STAGE2_IMAGE_RULES = """\
IMAGE QUERY RULES (image_query field):
- If about a company: return only the official company name (e.g. "Infosys", "Tata Motors").
- If about RBI: "Reserve Bank of India"
- If about SEBI: "Securities and Exchange Board of India"
- If about NSE: "National Stock Exchange of India"
- If about BSE: "Bombay Stock Exchange"
- If about an index: "Nifty 50" or "Sensex"
- If about a commodity: return its common name (e.g. "Gold", "Crude Oil")
- If no specific entity: a short industry term (e.g. "Banking", "IPO", "Logistics")
- Return ONLY 1-5 words. No artistic prompts, no URLs, no full sentences.
image_alt: One concise sentence describing the expected representative image.
"""

_STAGE2_SCORING_RULES = """\
SCORING RULES (STRICT):
- confidence_score: INTEGER 0-100. Your confidence in the content accuracy —
  i.e. how well the article's claims are grounded in the source data and
  Stage 1 evaluation, not a measure of the underlying event's importance.
- All scores must be integers. Never return decimals.
"""

_STAGE2_ANTI_HALLUCINATION = """\
ACCURACY & CONSISTENCY RULES (STRICT):
- Do NOT introduce any fact, number, date, or claim that is not present in the
  source item or the Stage 1 evaluation. If a section-relevant detail (e.g.
  historical comparison, analyst estimate) is not available, write in general
  but honest terms rather than inventing a specific figure.
- The article's sentiment, market_impact_level, and narrative tone MUST be
  consistent with the Stage 1 market_relevance_score, reason, and
  time_horizon fields for that item — do not contradict the prior evaluation.
- If the Stage 1 evaluation marked time_horizon as "long_term_structural",
  the STORY's ## Future Outlook and ## Risk Factors sections should carry
  proportionally more weight than ## Trading Implications, and vice versa
  for "short_term_catalyst". For "both", balance the two.
- Never present speculation as fact. Phrase uncertain implications as
  "could," "may," or "signals" rather than stating them definitively.
"""


def build_article_generation_prompt(
    evaluated_items_json: str,
    current_time: str,
) -> str:
    """
    Stage 2 prompt: generate premium financial articles from pre-evaluated items.

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

You have been given a batch of pre-evaluated news items. Each item has ALREADY been confirmed as
useful for trading/investment decisions by a Senior Market Intelligence Analyst (Stage 1). Your job
is ONLY to write premium quality financial articles from this pre-evaluated data. Do NOT re-evaluate,
re-score, or filter. Write an article for EVERY item in the input.

PRE-EVALUATED ITEMS (JSON array — each has "uid", raw news data, and Stage 1 evaluation
including market_relevance_score, reason, event_category, time_horizon, and executive_summary):
{evaluated_items_json}

YOUR TASK — for each item produce:

1. HEADLINE: Compelling, factual, max 120 chars. Lead with the concrete fact, not hype.
   Good: "HDFC Bank Q1 Results Beat Estimates — Net Profit Rises 18% YoY"
   Bad:  "HDFC Bank makes announcement" (vague) | "SHOCKING: Bank crashes" (clickbait)

2. EXECUTIVE SUMMARY: Refine the Stage 1 executive_summary into polished, publication-ready
   prose. Preserve the 4-paragraph structure:
     Paragraph 1 — What happened, with specific numbers/terms.
     Paragraph 2 — Historical context and why it matters.
     Paragraph 3 — Key risks, opportunities, and time horizon (short-term catalyst vs.
                   long-term structural vs. both).
     Paragraph 4 — Clear, actionable conclusion for traders/investors.
   This is the PRIMARY text displayed on the UI — make every sentence earn its place.
   Maximum 160 words total.

3. STORY: Full article, minimum 450 words, with these sections in order:
   ## Overview
   ## Why It Matters
   ## Trading Implications
   ## Impact on Retail Investors
   ## Institutional Impact
   ## Sector Analysis
   ## Risk Factors
   ## Future Outlook
   ## Key Takeaways (3-5 bullet points)

4. All other fields per the schema below.

WRITING STANDARDS:
- Bloomberg / Financial Times quality: precise, data-driven, no filler sentences.
- Include specific numbers, percentages, and dates from the source wherever available.
- Explain the "so what" for retail investors in plain, jargon-free language, while
  keeping institutional-grade precision in the Trading Implications and Institutional
  Impact sections.
- Every claim must be traceable to the source item or Stage 1 evaluation — see
  accuracy rules below.
- Avoid generic hedge language ("markets may react") without pairing it to a specific
  driver, level, or scenario.

SENTIMENT: Positive | Negative | Neutral | Mixed
MARKET_IMPACT_LEVEL: Low | Medium | High | Critical

{_STAGE2_IMAGE_RULES}

{_STAGE2_SCORING_RULES}

{_STAGE2_ANTI_HALLUCINATION}

JSON SCHEMA (return exactly this structure — no markdown, no code fences):
{{
  "articles": [
    {{
      "uid": "exact uid from the input item",
      "headline": "string (max 120 chars)",
      "executive_summary": "Paragraph 1\\n\\nParagraph 2\\n\\nParagraph 3\\n\\nParagraph 4",
      "story": "string (min 450 words with section headers)",
      "sentiment": "Positive|Negative|Neutral|Mixed",
      "market_impact_level": "Low|Medium|High|Critical",
      "confidence_score": 90,
      "market_impact": "string (1-2 sentences)",
      "retail_investor_impact": "string (1-2 sentences)",
      "institutional_impact": "string (1-2 sentences)",
      "trading_implications": "string or null",
      "risk_factors": "string or null",
      "future_outlook": "string or null",
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
# STANDALONE FALLBACK PROMPT — v2: self-filtering
# ═══════════════════════════════════════════════════════════════════════════════

_STANDALONE_RELEVANCE_BAR = """\
RELEVANCE BAR (STRICT):
Since this is a fallback mode with no pre-filtering, YOU must independently apply
the same trading-utility standard the platform normally uses. Only select events
that are:
- ACTIONABLE: name specific stocks/sectors/indices with a clear directional
  implication, backed by concrete numbers, dates, or terms.
- MATERIAL: represent a genuine beat/miss, trend change, new risk/opportunity,
  or a structural development — not routine or already-priced-in news.
- VERIFIABLE: corroborated by at least one credible source via web search, with
  specific facts, not vague narrative.
- FRESH: breaking or same-day news, not rehashed coverage of an old event.

Prioritize high-value categories: earnings & guidance, dividends/bonus/buybacks/
rights issues, IPOs/QIPs, M&A, large order wins, promoter stake changes, block/
bulk deals, credit rating changes, SEBI/NSE/BSE regulatory actions, index
changes, FII/DII flows, RBI policy, key macro data (CPI/GDP/IIP/PMI), and
commodity/currency moves with a clear market linkage.

Do NOT fill remaining article slots with low-value filler just to reach the
target count. If fewer than {max_articles} genuinely qualifying stories exist
right now, return fewer articles. Quality and trading usefulness take priority
over quantity.
"""

_STANDALONE_ANTI_HALLUCINATION = """\
ACCURACY RULES (STRICT):
- Every number, date, name, and claim in the article MUST come from a source
  found via web search. Do NOT invent or estimate figures.
- If you cannot verify a detail with reasonable confidence, omit it rather
  than guessing.
- Do not present speculation as fact — use "could," "may," or "signals" for
  uncertain implications.
- If search results conflict on a material fact (e.g. differing figures across
  sources), use the most authoritative/official source (exchange filing,
  regulator website, or company press release) and do not average or guess.
"""


def build_standalone_prompt(current_time: str, last_run_time: str | None) -> str:
    """
    Fallback prompt when the RSS fetcher yields nothing useful.

    Unlike the two-stage pipeline, this mode has no pre-filtering — Gemini must
    both discover AND evaluate relevance in a single pass via web search. It
    applies the same trading-utility bar as Stage 1 to avoid flooding the feed
    with low-value filler content just to hit MAX_ARTICLES.

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

For each qualifying story, write a premium article following these standards:

1. HEADLINE: Compelling, factual, max 120 chars. Lead with the concrete fact, not hype.

2. EXECUTIVE SUMMARY: Exactly 4 short paragraphs (blank line separated), max 160 words total:
     Paragraph 1 — What happened, with specific numbers/terms.
     Paragraph 2 — Historical context and why it matters.
     Paragraph 3 — Key risks, opportunities, and time horizon (short-term catalyst
                   vs. long-term structural vs. both).
     Paragraph 4 — Clear, actionable conclusion for traders/investors.

3. STORY: Full article, minimum 450 words, with these sections in order:
   ## Overview
   ## Why It Matters
   ## Trading Implications
   ## Impact on Retail Investors
   ## Institutional Impact
   ## Sector Analysis
   ## Risk Factors
   ## Future Outlook
   ## Key Takeaways (3-5 bullet points)

WRITING STANDARDS:
- Bloomberg / Financial Times quality: precise, data-driven, no filler sentences.
- Explain the "so what" for retail investors in plain language while keeping
  institutional-grade precision in Trading Implications and Institutional Impact.

SENTIMENT: Positive | Negative | Neutral | Mixed
MARKET_IMPACT_LEVEL: Low | Medium | High | Critical

{_STANDALONE_ANTI_HALLUCINATION}

IMAGE QUERY RULES (image_query field):
- If about a company: official company name only (e.g. "Infosys", "Tata Motors").
- If about RBI/SEBI/NSE/BSE: full official name.
- If about an index: "Nifty 50" or "Sensex".
- If about a commodity: common name (e.g. "Gold", "Crude Oil").
- If no specific entity: a short industry term (e.g. "Banking", "IPO").
- Return ONLY 1-5 words. No artistic prompts, no URLs, no full sentences.
image_alt: One concise sentence describing the expected representative image.

confidence_score: INTEGER 0-100. Your confidence in the verification and
accuracy of this article's claims. All scores MUST be integers, never decimals.

Generate UP TO {MAX_ARTICLES} qualifying articles (fewer is acceptable — see relevance bar above).

JSON SCHEMA (return exactly this structure — no markdown, no code fences):
{{
  "articles": [
    {{
      "uid": "generate-standalone-N",
      "headline": "string (max 120 chars)",
      "executive_summary": "Paragraph 1\\n\\nParagraph 2\\n\\nParagraph 3\\n\\nParagraph 4",
      "story": "string (min 450 words with section headers)",
      "sentiment": "Positive|Negative|Neutral|Mixed",
      "market_impact_level": "Low|Medium|High|Critical",
      "confidence_score": 0,
      "published_at": "string (ISO 8601 or null)",
      "market_impact": "string",
      "retail_investor_impact": "string",
      "institutional_impact": "string",
      "trading_implications": "string or null",
      "risk_factors": "string or null",
      "future_outlook": "string or null",
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

Return ONLY valid JSON. No markdown, no code fences."""


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