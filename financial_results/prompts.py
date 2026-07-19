"""
financial_results/prompts.py
----------------------------
Gemini prompt construction for financial result analysis.

Contains:
    • build_financial_analysis_prompt()  — constructs the full prompt
    • FinancialAnalysisOutput            — Pydantic validation schema

Design principle:  The prompt explicitly tells Gemini which metadata
fields are ALREADY extracted from the XBRL/HTML filing document so
Gemini can focus entirely on financial analysis, sentiment, and
crafting a professional headline + summary.

Fields we already have from XBRL/HTML/RSS parsing:
    company_name, symbol, exchange, quarter, financial_year,
    period_start, period_end, announcement_date,
    standalone_consolidated, filing_type, document_type, source_url

Fields Gemini must provide:
    headline, result_date, revenue, revenue_change_yoy, profit_net,
    profit_change_yoy, eps, eps_change_yoy, executive_summary,
    guidance, sentiment, impact, forecast_short_term,
    forecast_medium_term, source
"""
from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field

from .schemas import FinancialResultMetadata


# ─── Gemini output validation schema ────────────────────────────────────────

class ForecastOutput(BaseModel):
    direction:  str = Field(description="UP | DOWN | MIXED")
    confidence: str = Field(description="HIGH | MEDIUM | LOW")
    reason:     str = ""


class FinancialAnalysisOutput(BaseModel):
    """Pydantic model matching the expected Gemini JSON response.

    Only contains fields that require AI analysis — identity/period
    metadata and core financial numbers are already extracted from the
    filing document itself and are NOT requested from Gemini.
    """
    headline:               str            = Field(default="", description="WSJ-style headline")

    result_date:            str            = ""

    revenue_change_yoy:     Optional[float] = None
    profit_change_yoy:      Optional[float] = None
    eps_change_yoy:         Optional[float] = None

    executive_summary:      str            = ""

    guidance:               str            = ""

    sentiment:              str            = Field(default="NEUTRAL", description="BULLISH | BEARISH | NEUTRAL")
    impact:                 str            = Field(default="MEDIUM",  description="HIGH | MEDIUM | LOW")

    forecast_short_term:    Optional[ForecastOutput] = None
    forecast_medium_term:   Optional[ForecastOutput] = None

    source:                 List[str]      = Field(default_factory=list)


# ─── Source priority list ────────────────────────────────────────────────────

_SOURCE_PRIORITY = """\
PREFERRED SOURCES (in order of priority):
1. Official Company Investor Relations page
2. NSE (nseindia.com) corporate filings
3. BSE (bseindia.com) corporate filings
4. Official Earnings Press Release
5. Official Investor Presentation
6. Official Earnings Call Transcript
7. Moneycontrol (only if official source unavailable)

Do NOT use:
- Blogs
- Forums
- Social media
- Unofficial aggregators
"""

# ─── Anti-hallucination rules ────────────────────────────────────────────────

_RULES = """\
CRITICAL RULES:
- Never guess, estimate, or fabricate financial numbers.
- Return ONLY information supported by official disclosures.
- Do NOT analyze previous quarters — analyze ONLY the latest filing
  corresponding to the supplied metadata.
- Do NOT hallucinate numbers.
- Do NOT use blogs or forums.
- Do NOT provide BUY, SELL, or HOLD recommendations.
  Return only sentiment and impact — the recommendation is computed
  server-side.

COMPLETENESS MANDATE:
You MUST make every reasonable effort to fill ALL fields. A null value
is acceptable ONLY when the data genuinely does not exist in any
official source. Do NOT default to null out of convenience.

- eps_change_yoy: If EPS for both current and year-ago quarter are
                  available, you MUST compute the YoY percentage change.
                  Do NOT leave this null if both figures are findable.
- revenue_change_yoy / profit_change_yoy: Same rule — compute from
  current and year-ago figures if both are officially available.
- guidance:   See GUIDANCE RULES below.
- executive_summary: MUST always be filled. See EXECUTIVE SUMMARY RULES.
- headline:   MUST always be filled. See HEADLINE RULES.
- sentiment / impact: MUST always be filled based on your analysis.
- forecast_short_term / forecast_medium_term: MUST always be filled.
"""

# ─── Guidance rules ──────────────────────────────────────────────────────────

_GUIDANCE_RULES = """\
GUIDANCE RULES:
Extract management's forward-looking guidance from the earnings call,
press release, or investor presentation.

Include ANY of the following if disclosed:
- Revenue growth guidance (e.g. "15-17% revenue growth for FY27")
- Margin guidance (e.g. "Targeting 25%+ EBITDA margin")
- Order book / deal pipeline outlook
- Capex plans (e.g. "₹5,000 Cr capex planned for FY27")
- Hiring / headcount guidance
- Segment-level outlook (e.g. "Expect retail loan growth of 20%")
- Any revised or reiterated targets

Format as a concise 2-4 sentence paragraph. Example:
"Management reiterated FY27 revenue growth guidance of 13-15% in
constant currency. The company expects EBITDA margins to improve by
50-100 bps in H2 on the back of operational efficiencies. Capex for
the year is guided at ₹4,200 Cr, focused on data center expansion."

If management provided NO forward guidance whatsoever, return:
"Management did not provide forward guidance for upcoming quarters."

Do NOT return an empty string — always provide either extracted
guidance or the explicit no-guidance statement above.
"""

# ─── Headline rules ─────────────────────────────────────────────────────────

_HEADLINE_RULES = """\
HEADLINE RULES:
Write a single headline in the style of the Wall Street Journal or Bloomberg.

Requirements:
- Maximum 120 characters.
- Lead with the most market-moving number (profit, revenue, or EPS).
- Include YoY direction (Surges, Slips, Climbs, Jumps, Plunges, Edges Up, etc.).
- Mention the company name (short form preferred, e.g. "ICICI Bank" not "ICICI Bank Limited").
- Include the quarter context (e.g. "in Q1", "for June Quarter").
- Be factual, precise, and punchy — no editorializing.
- Use active voice, present tense.

Examples of excellent headlines:
- "Reliance Q1 Profit Surges 12% to ₹19,299 Cr on Retail, Jio Strength"
- "Infosys Lifts FY27 Guidance After June-Quarter Revenue Beats Street"
- "TCS Q1 Earnings Miss Estimates as Deal Pipeline Weakens"
- "HDFC Bank Net Jumps 35% to ₹16,474 Cr; NIM Steady at 3.6%"
- "Tata Motors Posts Surprise Loss on JLR Restructuring Charge"
"""

# ─── Executive summary rules ────────────────────────────────────────────────

_EXECUTIVE_SUMMARY_RULES = """\
EXECUTIVE SUMMARY RULES:
Write a 3-5 paragraph analysis in the style of a Wall Street Journal
earnings dispatch — precise, information-dense, no filler.

Structure:
  Paragraph 1 — THE HEADLINE NUMBER: Open with the single most important
  figure (net profit or revenue), its YoY change, and whether it beat
  or missed consensus estimates. One sentence, punchy. Use the VERIFIED
  FINANCIAL DATA provided to you as the source of truth for the absolute numbers.

  Paragraph 2 — REVENUE & MARGINS: Revenue trajectory, segment-level
  colour where available (e.g. retail vs wholesale for banks; IT services
  vs consulting). Operating margins, NIM for banks, EBITDA margin for
  industrials. Keep numbers grounded and sourced.

  Paragraph 3 — BUSINESS PERFORMANCE: Key operational highlights — deal
  wins, subscriber additions, market-share shifts, capacity utilisation,
  asset quality (GNPA/NNPA for banks), order book for infra/defence.

  Paragraph 4 — MANAGEMENT OUTLOOK: Forward guidance, capex plans,
  revised estimates, strategic pivots. If management did not provide
  guidance, say so explicitly ("Management did not update guidance").

  Paragraph 5 (optional) — RISKS & WATCH ITEMS: Regulatory headwinds,
  sector-specific risks, one-off items that inflated/deflated the
  quarter. Only include if material.

Rules:
- Use ₹ for Indian currency, Cr for crores, Lk Cr for lakh crores.
- All YoY comparisons must specify direction and percentage.
- Attribute every number to its source ("per BSE filing", "per the
  company's investor presentation").
- Do NOT speculate. Do NOT editorialize. Do NOT use superlatives
  unless quoting management.
- Keep it factual. If a number cannot be verified, omit it.
"""


# ─── Prompt builder ─────────────────────────────────────────────────────────

def build_financial_analysis_prompt(metadata: FinancialResultMetadata) -> str:
    """Construct the complete Gemini prompt for financial result analysis.

    The prompt explicitly lists what metadata we already have (parsed
    from the XBRL/HTML filing) so Gemini can focus on financial
    analysis rather than re-extracting identity or period information.

    Args:
        metadata: Parsed filing metadata.

    Returns:
        Complete prompt string.
    """
    # ── Build "already known" context block ────────────────────────────────
    known_parts = [f"Company Name: {metadata.company_name}"]

    if metadata.symbol:
        known_parts.append(f"Symbol: {metadata.symbol}")

    known_parts.append(f"Exchange: {metadata.exchange}")

    if metadata.quarter:
        known_parts.append(f"Quarter: {metadata.quarter}")

    if metadata.financial_year:
        known_parts.append(f"Financial Year: {metadata.financial_year}")

    if metadata.period_start:
        known_parts.append(f"Period Start: {metadata.period_start}")

    if metadata.period_end:
        known_parts.append(f"Period End: {metadata.period_end}")

    if metadata.announcement_date:
        known_parts.append(f"Announcement Date: {metadata.announcement_date}")

    if metadata.standalone_consolidated:
        known_parts.append(f"Report Type: {metadata.standalone_consolidated}")

    if metadata.filing_type:
        known_parts.append(f"Filing Type: {metadata.filing_type}")

    if metadata.document_type:
        known_parts.append(f"Document Type: {metadata.document_type}")

    known_parts.append(f"Source URL: {metadata.source_url}")
    
    if metadata.financials:
        known_parts.append("\nVERIFIED FINANCIAL DATA (Extracted from Filing):")
        f = metadata.financials
        if f.revenue is not None: known_parts.append(f"Revenue: ₹{f.revenue:,.2f} Cr")
        if f.profit_net is not None: known_parts.append(f"Net Profit: ₹{f.profit_net:,.2f} Cr")
        if f.basic_eps is not None: known_parts.append(f"Basic EPS: ₹{f.basic_eps:,.2f}")
        if f.ebitda is not None: known_parts.append(f"EBITDA: ₹{f.ebitda:,.2f} Cr")
        if f.ebitda_margin is not None: known_parts.append(f"EBITDA Margin: {f.ebitda_margin:,.2f}%")
        if f.pat_margin is not None: known_parts.append(f"PAT Margin: {f.pat_margin:,.2f}%")
        if f.net_interest_income is not None: known_parts.append(f"Net Interest Income: ₹{f.net_interest_income:,.2f} Cr")
        if f.provisions is not None: known_parts.append(f"Provisions: ₹{f.provisions:,.2f} Cr")
        if f.gross_npa_pct is not None: known_parts.append(f"Gross NPA: {f.gross_npa_pct * 100:,.2f}%")

    known_block = "\n".join(known_parts)

    # ── Build standalone/consolidated differentiation block ────────────
    report_type = (metadata.standalone_consolidated or "").strip()
    report_type_upper = report_type.upper()

    if report_type_upper == "CONSOLIDATED":
        report_type_block = """
REPORT TYPE INSTRUCTIONS — CONSOLIDATED:
This filing is a CONSOLIDATED result. You MUST report CONSOLIDATED
figures that INCLUDE subsidiaries, associates, and joint ventures.

For example, for ICICI Bank consolidated results include income and
profit from ICICI Prudential Life, ICICI Lombard, ICICI Securities,
and all other group entities. Consolidated revenue and profit are
ALWAYS HIGHER than standalone for companies with subsidiaries.

- Use CONSOLIDATED revenue (total income including subsidiaries).
- Use CONSOLIDATED net profit / PAT.
- Use CONSOLIDATED EPS (which factors in minority interest).
- The headline MUST mention "consolidated" or "group".
- The executive summary MUST reference consolidated figures and
  mention key subsidiary contributions where available.

DO NOT use standalone figures. If you cannot find consolidated-specific
numbers, search harder — they are always filed separately."""
    elif report_type_upper == "STANDALONE":
        report_type_block = """
REPORT TYPE INSTRUCTIONS — STANDALONE:
This filing is a STANDALONE result. You MUST report STANDALONE figures
that cover ONLY the parent entity, EXCLUDING subsidiaries.

For example, for ICICI Bank standalone results cover only the bank's
own operations — NOT ICICI Prudential, ICICI Lombard, ICICI Securities,
or other group companies. Standalone figures are typically LOWER than
consolidated for companies with subsidiaries.

- Use STANDALONE revenue (parent entity only).
- Use STANDALONE net profit / PAT.
- Use STANDALONE EPS.
- The headline MUST mention "standalone" if the company also files
  consolidated results (e.g. banks, conglomerates).
- The executive summary MUST reference standalone figures only.

DO NOT use consolidated figures. If you find only consolidated data,
search harder — standalone results are always filed separately."""
    else:
        report_type_block = ""

    return f"""You are a Senior Financial Analyst at a Wall Street Journal-calibre
publication, specializing in Indian equity markets.

You have been given verified metadata about a newly announced financial
result filing. This metadata has already been extracted from the official
XBRL/HTML filing document — do NOT re-extract or override these fields.

VERIFIED FILING METADATA & FINANCIALS (already extracted — do NOT override):

{known_block}
{report_type_block}

YOUR TASK:
Using Google Search, locate the official {report_type.lower() or 'financial'} result
corresponding to the above filing and produce the FINANCIAL ANALYSIS
fields listed below. Do NOT return company name, symbol, exchange,
quarter, period, or any field already listed in the verified metadata.
We ALREADY have the absolute financial numbers (Revenue, PAT, EPS) from the document.
Your job is to find the YoY changes, management guidance, and write the qualitative analysis.

Steps:
1. Use Google Search to find the official {report_type.lower() or 'financial'} result
   for this specific company, quarter, and financial year.
2. Cross-check financial numbers against official sources to compute YoY changes.
3. Search for management guidance from earnings calls, press releases,
   or investor presentations.
4. Extract ALL available values. Use null ONLY if data genuinely does
   not exist in any official source.
5. Write a WSJ-style headline, detailed executive summary, and guidance.
   {"The headline and summary MUST reference '" + report_type.lower() + "' figures." if report_type else ""}
6. Fill in sentiment, impact, and both forecasts based on your analysis.

{_SOURCE_PRIORITY}

{_RULES}

{_HEADLINE_RULES}

{_EXECUTIVE_SUMMARY_RULES}

{_GUIDANCE_RULES}

OUTPUT FORMAT:
Return ONLY valid JSON. No Markdown. No explanations. No code fences.

JSON SCHEMA (return exactly this structure):
{{
    "headline": "",
    "result_date": "",
    "revenue_change_yoy": null,
    "profit_change_yoy": null,
    "eps_change_yoy": null,
    "executive_summary": "",
    "guidance": "",
    "sentiment": "BULLISH|BEARISH|NEUTRAL",
    "impact": "HIGH|MEDIUM|LOW",
    "forecast_short_term": {{
        "direction": "UP|DOWN|MIXED",
        "confidence": "HIGH|MEDIUM|LOW",
        "reason": ""
    }},
    "forecast_medium_term": {{
        "direction": "UP|DOWN|MIXED",
        "confidence": "HIGH|MEDIUM|LOW",
        "reason": ""
    }},
    "source": [
        ""
    ]
}}

Field definitions:
- headline:            REQUIRED. WSJ-style headline, max 120 chars.
- result_date:         REQUIRED. Date the result was announced (YYYY-MM-DD).
- revenue_change_yoy:  REQUIRED if both quarters available. YoY % change.
- profit_change_yoy:   REQUIRED if both quarters available. YoY % change.
- eps_change_yoy:      REQUIRED if both quarters available. YoY % change.
- executive_summary:   REQUIRED. 3-5 paragraph WSJ-style analysis.
- guidance:            REQUIRED. Management's forward guidance, OR explicit
                       statement that no guidance was provided. NEVER empty.
- sentiment:           REQUIRED. BULLISH | BEARISH | NEUTRAL.
- impact:              REQUIRED. HIGH | MEDIUM | LOW.
- forecast_short_term: REQUIRED. Near-term directional outlook.
- forecast_medium_term: REQUIRED. Medium-term directional outlook.
- source:              REQUIRED. Array of URLs used for verification.

Use null for YoY change numbers ONLY when the data genuinely does not
exist in any official source. Never leave a field null out of laziness.

Return ONLY the JSON object. No surrounding text."""
