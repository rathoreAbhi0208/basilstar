"""
news/earnings/prompts.py
------------------------
Two-stage Gemini prompt system for the Earnings Intelligence Pipeline.

Earnings Stage 1 — Financial Data Collection
    build_earnings_data_collection_prompt()
    → Structured JSON with financial data, NO analysis, NO scores

Earnings Stage 2 — Professional Financial Analysis
    build_earnings_analysis_prompt()
    → Decision-oriented analysis from Stage 1 JSON only (no web search)

Design rules
~~~~~~~~~~~~
• Stage 1 MUST use Google Search grounding to gather financial data.
• Stage 1 MUST NOT perform any analysis — only collect facts and nulls.
• Stage 2 receives ONLY the Stage 1 JSON. No web search. No external lookup.
• Stage 2 acts as an experienced equity research analyst.
• Output must be readable in 30-60 seconds. No storytelling.
"""
from __future__ import annotations


# ═══════════════════════════════════════════════════════════════════════════════
# EARNINGS STAGE 1 — Financial Data Collection
# ═══════════════════════════════════════════════════════════════════════════════

def build_earnings_data_collection_prompt(
    company_name: str,
    headline: str,
    summary: str,
    source_url: str,
    current_time: str,
) -> str:
    return f"""You are a financial data collector. Current IST time: {current_time}

EVENT:
Company: {company_name}
Headline: {headline}
Summary: {summary}
Source: {source_url}

TASK: gather REAL, sourced data for the most recently reported quarter/FY. Do not hallucinate — if a value cannot be found, use null. Compute only the derived metrics listed (formulas given), using found figures. Do not rate, score, interpret, or recommend anything.

RESEARCH STRATEGY (follow in order — this is what actually determines your fill rate):
1. FIRST, open and read {source_url} directly (you have URL Context access) — it is
   the primary source for this event and often already contains the headline
   income-statement numbers (revenue, PAT, EPS, margins) and management quotes.
2. THEN, run TARGETED searches per category below rather than one generic query.
   Named sources consistently carry this data for Indian-listed companies —
   search them by name:
   - Income statement, balance sheet, cash flow, 4-quarter trend, valuation
     multiples (P/E, PEG, EV/EBITDA), promoter pledge: "{company_name} quarterly
     results Screener.in", "{company_name} balance sheet Screener.in"
   - Peer/sector comparison, sector P/E: "{company_name} peer comparison
     Screener.in" or "{company_name} vs peers Trendlyne"
   - Analyst estimates vs actuals, consensus: "{company_name} results vs
     estimates Moneycontrol" or "{company_name} brokerage report {current_time[:4]}"
   - Market data (price reaction, volume, 52w range): "{company_name} share
     price target results reaction" or NSE/BSE quote pages
   - Management commentary, guidance, capex: "{company_name} concall
     transcript" or "{company_name} investor presentation"
   - Audit/governance facts: "{company_name} annual report auditor" or
     "{company_name} related party transactions"
   Run a separate search per category you can't fill from the source page —
   do not rely on a single broad query to surface all of these at once.
3. If a specific figure still can't be found after a targeted search, leave
   it null. Do not estimate, average, or infer it from partial data.

FORMULAS TO APPLY (compute only, no commentary):
- Operating Margin = Operating Profit / Revenue
- Net Margin = Net Profit / Revenue
- Revenue YoY = (Current Qtr Revenue - Same Qtr Last Year) / Same Qtr Last Year
- Revenue QoQ = (Current Qtr Revenue - Previous Qtr Revenue) / Previous Qtr Revenue
- PAT YoY / QoQ = same method on Net Profit
- Debt-to-Equity = Total Debt / Total Equity
- Current Ratio = Current Assets / Current Liabilities
- PEG Ratio = P/E Ratio / Earnings Growth Rate (%)
- EV/EBITDA = Enterprise Value / EBITDA

DATA TO COLLECT (search for each; leave null if unavailable):
1. Income statement: Revenue, Operating Profit/EBITDA, Net Profit (PAT), EPS — current period, YoY comparable, QoQ comparable
2. Balance sheet: Total Debt, Total Equity, Current Assets, Current Liabilities, Promoter Pledge %
3. Cash flow: Operating Cash Flow (OCF)
4. Valuation: Current P/E, 5Y Average P/E, Sector P/E, Peer P/E, Market Cap, Dividend Yield, Enterprise Value
5. Analyst estimates (if published): Estimated Revenue, EPS, EBITDA vs Actuals
6. Peer data: 2-3 direct competitors — Revenue Growth %, Operating Margin %, P/E
7. Management commentary (from concall/press release, factual only): stated guidance figures, capex plans announced, risks explicitly mentioned by management
8. Audit/governance facts: auditor opinion type, any auditor change, any disclosed contingent liabilities/related-party transactions, any going-concern note
9. Market data: stock price reaction (pre/post results %), trading volume vs average, 52-week high/low, current price
10. Historical trend: Revenue and Operating Margin for last 4 reported quarters

data_completeness_pct: your honest estimate of what fraction of ALL fields
above you actually found real values for (not a guess — count filled vs
total leaf fields). missing_fields: list the specific field paths (e.g.
"valuation.peer_pe", "audit_governance.auditor_opinion") you could not find
after following the research strategy above — this drives a follow-up
targeted search pass, so be precise and complete, not just a couple examples.

OUTPUT — return ONLY this JSON structure, no markdown, no prose, no fields beyond this schema. Every value must be either real (with unit, e.g. "₹72,275 Cr") or null. Every computed field must show the formula result, not an opinion:

{{
  "financial_data": {{
    "company": "{company_name}",
    "quarter": "Q1/Q2/Q3/Q4/FY",
    "fiscal_year": "FY2026",
    "report_date": "YYYY-MM-DD",

    "income_statement": {{
      "revenue": {{"current": null, "yoy_prior": null, "qoq_prior": null, "yoy_pct": null, "qoq_pct": null}},
      "operating_profit_ebitda": {{"current": null, "margin_pct": null}},
      "net_profit_pat": {{"current": null, "yoy_prior": null, "yoy_pct": null, "net_margin_pct": null}},
      "eps": {{"current": null, "yoy_prior": null, "yoy_pct": null}}
    }},

    "balance_sheet": {{
      "total_debt": null,
      "total_equity": null,
      "debt_to_equity": null,
      "current_assets": null,
      "current_liabilities": null,
      "current_ratio": null,
      "promoter_pledge_pct": null
    }},

    "cash_flow": {{
      "ocf": null,
      "ocf_vs_pat": null
    }},

    "valuation": {{
      "current_pe": null,
      "avg_5y_pe": null,
      "sector_pe": null,
      "peer_pe": null,
      "peg_ratio": null,
      "ev_ebitda": null,
      "market_cap": null,
      "dividend_yield": null
    }},

    "analyst_estimates": {{
      "revenue_estimate": null,
      "revenue_actual": null,
      "eps_estimate": null,
      "eps_actual": null,
      "ebitda_estimate": null,
      "ebitda_actual": null
    }},

    "peer_comparison": [
      {{"company_name": null, "revenue_growth_pct": null, "operating_margin_pct": null, "pe": null}}
    ],

    "management_commentary_facts": {{
      "guidance_stated": null,
      "capex_plans": null,
      "risks_mentioned": []
    }},

    "audit_governance": {{
      "auditor_opinion": null,
      "auditor_changed": null,
      "contingent_liabilities": null,
      "related_party_transactions": null,
      "going_concern_note": null
    }},

    "market_data": {{
      "current_price": null,
      "pre_results_change_pct": null,
      "post_results_change_pct": null,
      "volume_vs_avg": null,
      "52w_high": null,
      "52w_low": null
    }},

    "historical_trend_last_4_quarters": {{
      "quarters": [null, null, null, null],
      "revenue": [null, null, null, null],
      "operating_margin_pct": [null, null, null, null]
    }},

    "data_completeness_pct": null,
    "missing_fields": ["string"]
  }}
}}

Return ONLY valid JSON. No markdown. No prose outside JSON."""


# ═══════════════════════════════════════════════════════════════════════════════
# EARNINGS STAGE 1b — Targeted Gap-Fill (only runs when completeness is low)
# ═══════════════════════════════════════════════════════════════════════════════

def build_earnings_gap_fill_prompt(
    company_name: str,
    source_url: str,
    known_data_json: str,
    missing_fields: list[str],
    current_time: str,
) -> str:
    """
    Build a focused follow-up prompt that searches ONLY for the specific
    fields the first collection pass could not find. Keeps the retry cheap
    (small, targeted ask) instead of re-running the full 40-field request.
    """
    missing_list = "\n".join(f"- {f}" for f in missing_fields)
    return f"""You are a financial data collector doing a targeted follow-up search. Current IST time: {current_time}

Company: {company_name}
Source article: {source_url}

DATA ALREADY FOUND (do not re-search these, do not overwrite them):
{known_data_json}

STILL MISSING — search specifically for each of these, one query per item,
using named Indian financial data sources (Screener.in, Trendlyne,
Moneycontrol, Tickertape, NSE/BSE filings, brokerage reports) as appropriate
to the field:
{missing_list}

RULES:
- Do not hallucinate. If a value genuinely cannot be found after a real
  targeted search, leave it null — do not estimate or infer.
- Return ONLY the fields you were asked to fill, nested under the same
  path structure as the schema below (omit sections with nothing new).
- Do not repeat or restate values already found above.

Return ONLY this JSON structure, no markdown, no prose:

{{
  "financial_data": {{
    "income_statement": {{ "...(only if you found something new)": null }},
    "balance_sheet": {{}},
    "cash_flow": {{}},
    "valuation": {{}},
    "analyst_estimates": {{}},
    "peer_comparison": [],
    "management_commentary_facts": {{}},
    "audit_governance": {{}},
    "market_data": {{}},
    "historical_trend_last_4_quarters": {{}}
  }}
}}

Return ONLY valid JSON matching the relevant nested paths. No markdown. No prose outside JSON."""

# ═══════════════════════════════════════════════════════════════════════════════
# EARNINGS STAGE 2 — Professional Financial Analysis
# ═══════════════════════════════════════════════════════════════════════════════

def build_earnings_analysis_prompt(
    company_name: str,
    financial_data_json: str,
    current_time: str,
) -> str:
    """
    Build the Stage 2 prompt for professional financial analysis.

    Receives ONLY the structured JSON from Stage 1.
    NO web search. NO external lookup. NO hallucination.
    """
    return f"""You are a 20-year Equity Research Analyst (Indian markets). Current IST time: {current_time}
Company: {company_name}

FINANCIAL DATA (from Stage 1, already verified — use ONLY this, do not invent numbers):
{financial_data_json}

RULES:
- If the underlying data is null, do not guess or score it.
- For any field with a fixed set of allowed values (every "verdict", "position", "*_trend", Yes/No field, and "valuation verdict") — if it cannot be scored due to missing data, output exactly the literal "N/A". Never write "Data not available" or any other free text inside these constrained fields.
- Put the human-readable explanation ("data not available for X") only in free-text fields: "value", "note", "explanation", "deductions".
- Every explanation ≤1 short sentence. No storytelling, no repeated points across sections.
- Only report checklist/audit items that are flagged (WARNING/FAIL) — do not list clean items.
- Output must be scannable in under a minute: scores, verdicts, short bullets only.

SCORING RULES TO APPLY:

Income Statement:
- Revenue YoY: PASS if double-digit growth, WARNING if 0-10%, FAIL if declining.
- Operating Margin: PASS if stable/expanding, WARNING if compressing <3pts, FAIL if compressing >3pts.
- Net Profit Quality: FAIL/WARNING if profit growth is materially driven by "Other Income" vs core operations.

Balance Sheet:
- Debt-to-Equity: PASS <0.5, WARNING 0.5-1.0, FAIL >1.0.
- Current Ratio: PASS >1.5, WARNING 1.0-1.5, FAIL <1.0.
- Promoter Pledge: PASS 0%, WARNING 1-20%, FAIL >20%.

Management Efficiency Score (10 pts total):
- Growth (3 pts): both Revenue & PAT YoY >15% = 3; only one = 1.5; neither = 0.
- Profitability (2 pts): Operating Margin stable/expanding = 2; compressing = 0.
- Cash Quality (2 pts): OCF ≥ PAT = 2; OCF < PAT = 0.
- Debt Safety (2 pts): D/E <0.5 = 2; D/E >1.5 = -1; else 0.
- Valuation (1 pt): PEG <1.0 = 1; PEG >2.0 = 0.
Rating: Excellent 9-10, Good 7-8, Average 5-6, Weak 3-4, Poor 0-2.

Valuation Verdict: compare current P/E to 5Y avg P/E and PEG → Undervalued / Fairly Valued / Expensive / Very Expensive.

Quick Decode (3 yes/no): Sales & profit growing? Debt under control (D/E<1)? Price not near valuation extremes? All 3 Yes = strong fundamental candidate.

Audit Red Flags — flag ONLY if present in data: qualified/adverse/disclaimer opinion, going-concern doubt, high/non-arm's-length related-party transactions, aggressive revenue recognition or frequent policy changes, internal control weaknesses, large contingent liabilities/tax disputes, unexplained prior-period adjustments, frequent/unexplained auditor change, OCF vs PAT mismatch not explained by business rationale, filing delays, management override of controls, unexplained inter-company loans.
Assign audit_risk_score 0-10 (0=clean) based on severity/count of flags found.

Return ONLY this JSON (no markdown, no code fences, no extra fields):

{{
  "earnings_analysis": {{
    "headline": "Company Qx FYxx Results: verdict in one line",
    "executive_summary": "string, max 150 words — what happened, strong/weak, top positive, top concern, final view",
    "overall_score": 7,
    "overall_rating": "Excellent|Good|Average|Weak|Poor",
    "recommendation": "Strong Buy|Buy|Accumulate|Hold|Reduce|Sell|Avoid",
    "confidence": 75,

    "quick_scorecard": [
      {{"category": "Revenue", "verdict": "PASS|WARNING|FAIL|N/A"}},
      {{"category": "Profit", "verdict": "PASS|WARNING|FAIL|N/A"}},
      {{"category": "Margins", "verdict": "PASS|WARNING|FAIL|N/A"}},
      {{"category": "Debt", "verdict": "PASS|WARNING|FAIL|N/A"}},
      {{"category": "Valuation", "verdict": "PASS|WARNING|FAIL|N/A"}},
      {{"category": "Audit", "verdict": "PASS|WARNING|FAIL|N/A"}}
    ],

    "quick_decode": {{"growth": "Yes|No|N/A", "debt_control": "Yes|No|N/A", "valuation_reasonable": "Yes|No|N/A", "strong_candidate": true}},

    "income_statement": [
      {{"metric": "Revenue Growth YoY", "value": "string", "verdict": "PASS|WARNING|FAIL|N/A", "note": "string"}},
      {{"metric": "Revenue Growth QoQ", "value": "string", "verdict": "PASS|WARNING|FAIL|N/A", "note": "string"}},
      {{"metric": "Operating Margin", "value": "string", "verdict": "PASS|WARNING|FAIL|N/A", "note": "string"}},
      {{"metric": "Net Profit Quality", "value": "string", "verdict": "PASS|WARNING|FAIL|N/A", "note": "string"}}
    ],

    "balance_sheet": [
      {{"metric": "Debt-to-Equity", "value": "string", "verdict": "PASS|WARNING|FAIL|N/A", "note": "string"}},
      {{"metric": "Current Ratio", "value": "string", "verdict": "PASS|WARNING|FAIL|N/A", "note": "string"}},
      {{"metric": "Promoter Pledge", "value": "string", "verdict": "PASS|WARNING|FAIL|N/A", "note": "string"}}
    ],

    "management_efficiency": {{
      "growth": 3, "profitability": 2, "cash_quality": 2, "debt_safety": 2, "valuation": 1,
      "total": 8, "max": 10, "rating": "Good",
      "deductions": "string, 1 sentence on where points were lost"
    }},

    "valuation_analysis": {{
      "current_pe": "string", "historical_pe": "string", "peg": "string",
      "verdict": "Undervalued|Fairly Valued|Expensive|Very Expensive|N/A",
      "note": "string"
    }},

    "management_commentary": {{
      "guidance": "string", "capex": "string",
      "growth_drivers": ["string"], "risks": ["string"]
    }},

    "historical_trend": {{
      "revenue_trend": "Consistent Growth|Volatile|Declining|N/A",
      "margin_trend": "Expansion|Compression|Stable|N/A",
      "note": "string, is current quarter an anomaly or continuation"
    }},

    "expectation_vs_actual": {{
      "revenue": {{"estimate": "string", "actual": "string"}},
      "eps": {{"estimate": "string", "actual": "string"}},
      "overall_verdict": "Beat|Inline|Miss|N/A"
    }},

    "peer_comparison": [
      {{"company_name": "string", "position": "Best|Average|Worst|N/A", "note": "string"}}
    ],

    "audit_flags": {{
      "audit_risk_score": 2,
      "risk_level": "Low|Medium|High",
      "flags": [
        {{"category": "string (e.g. Contingent Liability)", "detail": "string"}}
      ]
    }},

    "risk_dashboard": [
      {{"risk": "string", "severity": "Medium|High"}}
    ],

    "investment_verdict": {{
      "suitable_for": ["string"],
      "not_suitable_for": ["string"],
      "top_reasons": ["string"],
      "top_risks": ["string"]
    }},

    "ai_confidence": {{
      "data_completeness": 85,
      "missing_critical_information": ["string"]
    }}
  }}
}}

Return ONLY valid JSON. No markdown. No prose outside JSON."""