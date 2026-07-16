"""
news/earnings/models.py
-----------------------
Pydantic v2 data models for the Earnings Intelligence Pipeline.

These are COMPLETELY SEPARATE from news/models.py NewsArticle.
The output is NOT a news article — it is a structured financial intelligence
report optimised for fast investor decision-making.

Pipeline
~~~~~~~~
Stage 1 (build_earnings_data_collection_prompt) → FinancialDataCollection
    Web-search-backed RAW DATA ONLY. No verdicts, no scores, no opinions.
    Every leaf value is either a real, sourced string (with unit, e.g. "₹72,275 Cr")
    or null. Nulls are expected and normal — never fabricated.

Stage 2 (build_earnings_analysis_prompt) → EarningsAnalysis
    Consumes ONLY the Stage 1 JSON. No web search. Applies fixed scoring rules
    (PASS/WARNING/FAIL thresholds, 10-point management efficiency score, audit
    red-flag detection) to produce a decision-oriented report.

Design rules
~~~~~~~~~~~~
• Models mirror the LLM JSON schemas field-for-field — no extra/renamed fields,
  so `model_validate(llm_json)` works directly without a translation layer.
• Money/ratio/percentage values from the LLM are formatted strings (e.g. "0.3",
  "+13.9%", "₹2,000 Cr"), not floats — kept as Optional[str] to avoid brittle
  parsing/validation failures on LLM output. Only true integer scores
  (0-10 scores, 0-100 confidence) are typed as int with bounds.
• Verdicts / ratings / risk levels use Literal[...] for compile-time and
  runtime safety instead of free-form strings.
• `extra="ignore"` on stage models tolerates minor LLM schema drift without
  raising, while still giving typed, validated access to known fields.
• null is used for unavailable data — models never invent defaults for
  factual fields (only structural defaults like empty lists are used).
"""
from __future__ import annotations

from typing import Annotated, List, Literal, Optional, Union
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

# ═══════════════════════════════════════════════════════════════════════════════
# COERCION HELPERS
# ═══════════════════════════════════════════════════════════════════════════════
# LLM output is JSON-typed, not schema-typed: a field we ask for as a formatted
# string (e.g. "32.99") sometimes arrives as a bare JSON number (32.99), and a
# field we ask for as an integer score sometimes arrives with a fractional part
# (e.g. overall_score derived from a management_efficiency total of 4.5).
# These coerce on the way in rather than rejecting valid data over formatting.

def _coerce_to_str(v):
    """Numeric leaf values (money, ratios, percentages) → string. Passes str/None through."""
    if v is None or isinstance(v, str):
        return v
    if isinstance(v, (int, float)):
        return str(v)
    return v


def _coerce_to_rounded_int(v):
    """Float scores (e.g. 4.5) → nearest int. Passes int/None through."""
    if v is None or isinstance(v, int):
        return v
    if isinstance(v, float):
        return round(v)
    if isinstance(v, str):
        v_clean = v.strip().replace("%", "")
        try:
            return round(float(v_clean))
        except ValueError:
            return v
    return v


# FlexStr: for any "value-ish" field (money/ratio/percentage/price) that must
# tolerate the LLM emitting a bare number instead of a quoted string.
FlexStr = Annotated[Optional[str], BeforeValidator(_coerce_to_str)]

# ScoreInt: for integer score/confidence fields that must tolerate a fractional
# input (e.g. a score built from halves, like management_efficiency.growth).
ScoreInt = Annotated[int, BeforeValidator(_coerce_to_rounded_int)]


# ═══════════════════════════════════════════════════════════════════════════════
# SHARED LITERAL TYPES
# ═══════════════════════════════════════════════════════════════════════════════

# NOTE: "N/A" is included on every enum that can legitimately be un-scoreable
# when the underlying Stage 1 data is null (e.g. no PE data → no valuation
# verdict). The Stage 2 prompt instructs the model to use exactly "N/A" here
# (never free text like "Data not available") so these stay validation-safe.
Verdict          = Literal["PASS", "WARNING", "FAIL", "N/A"]
RiskLevel        = Literal["Low", "Medium", "High"]
RiskSeverity      = Literal["Medium", "High"]          # risk_dashboard only lists flagged (non-Low) risks
Rating           = Literal["Excellent", "Good", "Average", "Weak", "Poor"]
ValuationVerdict = Literal["Undervalued", "Fairly Valued", "Expensive", "Very Expensive", "N/A"]
Recommendation   = Literal[
    "Strong Buy", "Buy", "Accumulate", "Hold", "Reduce", "Sell", "Avoid"
]
BeatInlineMiss   = Literal["Beat", "Inline", "Miss", "N/A"]
YesNo            = Literal["Yes", "No", "N/A"]
PeerPosition     = Literal["Best", "Average", "Worst", "N/A"]
TrendDirection   = Literal["Consistent Growth", "Volatile", "Declining", "N/A"]
MarginTrend      = Literal["Expansion", "Compression", "Stable", "N/A"]


class _StrictModel(BaseModel):
    """Base for LLM-populated models: tolerate minor field drift, keep types strict."""
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


# ═══════════════════════════════════════════════════════════════════════════════
# STAGE 1 — FINANCIAL DATA COLLECTION (raw facts only, no analysis)
# ═══════════════════════════════════════════════════════════════════════════════

class RevenueBlock(_StrictModel):
    current:    FlexStr = None
    yoy_prior:  FlexStr = None
    qoq_prior:  FlexStr = None
    yoy_pct:    FlexStr = None
    qoq_pct:    FlexStr = None


class OperatingProfitBlock(_StrictModel):
    current:    FlexStr = None
    margin_pct: FlexStr = None


class NetProfitBlock(_StrictModel):
    current:        FlexStr = None
    yoy_prior:      FlexStr = None
    yoy_pct:        FlexStr = None
    net_margin_pct: FlexStr = None


class EPSBlock(_StrictModel):
    current:   FlexStr = None
    yoy_prior: FlexStr = None
    yoy_pct:   FlexStr = None


class IncomeStatementData(_StrictModel):
    revenue:                 RevenueBlock         = Field(default_factory=RevenueBlock)
    operating_profit_ebitda: OperatingProfitBlock  = Field(default_factory=OperatingProfitBlock)
    net_profit_pat:           NetProfitBlock       = Field(default_factory=NetProfitBlock)
    eps:                      EPSBlock             = Field(default_factory=EPSBlock)


class BalanceSheetData(_StrictModel):
    total_debt:            FlexStr = None
    total_equity:          FlexStr = None
    debt_to_equity:        FlexStr = None
    current_assets:        FlexStr = None
    current_liabilities:   FlexStr = None
    current_ratio:         FlexStr = None
    promoter_pledge_pct:   FlexStr = None


class CashFlowData(_StrictModel):
    ocf:        FlexStr = None
    ocf_vs_pat: FlexStr = None


class ValuationData(_StrictModel):
    current_pe:     FlexStr = None
    avg_5y_pe:      FlexStr = None
    sector_pe:      FlexStr = None
    peer_pe:        FlexStr = None
    peg_ratio:      FlexStr = None
    ev_ebitda:      FlexStr = None
    market_cap:     FlexStr = None
    dividend_yield: FlexStr = None


class AnalystEstimatesData(_StrictModel):
    revenue_estimate: FlexStr = None
    revenue_actual:   FlexStr = None
    eps_estimate:     FlexStr = None
    eps_actual:       FlexStr = None
    ebitda_estimate:  FlexStr = None
    ebitda_actual:    FlexStr = None


class PeerRawData(_StrictModel):
    company_name:        Optional[str] = None
    revenue_growth_pct:  FlexStr = None
    operating_margin_pct: FlexStr = None
    pe:                  FlexStr = None


class ManagementCommentaryFacts(_StrictModel):
    guidance_stated: Optional[str] = None
    capex_plans:     Optional[str] = None
    risks_mentioned: List[str] = Field(default_factory=list)


class AuditGovernanceFacts(_StrictModel):
    auditor_opinion:            Optional[str] = None
    auditor_changed:            Optional[str] = None
    contingent_liabilities:     Optional[str] = None
    related_party_transactions: Optional[str] = None
    going_concern_note:         Optional[str] = None


class MarketData(_StrictModel):
    current_price:            FlexStr = None
    pre_results_change_pct:   FlexStr = None
    post_results_change_pct:  FlexStr = None
    volume_vs_avg:             FlexStr = None
    high_52w:                 FlexStr = Field(default=None, alias="52w_high")
    low_52w:                  FlexStr = Field(default=None, alias="52w_low")

    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class HistoricalTrendLast4Quarters(_StrictModel):
    quarters:            List[Optional[str]] = Field(default_factory=list)
    revenue:              List[FlexStr] = Field(default_factory=list)
    operating_margin_pct: List[FlexStr] = Field(default_factory=list)


class FinancialDataCollection(_StrictModel):
    """
    Complete output of Earnings Stage 1 — raw structured financial data.
    NO analysis, NO scores, NO verdicts, NO recommendations. Facts and nulls only.
    """
    company:       Optional[str] = None
    quarter:       Optional[str] = None
    fiscal_year:   Optional[str] = None
    report_date:   Optional[str] = None

    income_statement:  IncomeStatementData         = Field(default_factory=IncomeStatementData)
    balance_sheet:     BalanceSheetData             = Field(default_factory=BalanceSheetData)
    cash_flow:         CashFlowData                 = Field(default_factory=CashFlowData)
    valuation:         ValuationData                = Field(default_factory=ValuationData)
    analyst_estimates: AnalystEstimatesData         = Field(default_factory=AnalystEstimatesData)
    peer_comparison:   List[PeerRawData]            = Field(default_factory=list)

    management_commentary_facts: ManagementCommentaryFacts = Field(default_factory=ManagementCommentaryFacts)
    audit_governance:            AuditGovernanceFacts       = Field(default_factory=AuditGovernanceFacts)
    market_data:                 MarketData                 = Field(default_factory=MarketData)
    historical_trend_last_4_quarters: HistoricalTrendLast4Quarters = Field(
        default_factory=HistoricalTrendLast4Quarters
    )

    data_completeness_pct: Optional[ScoreInt] = Field(default=None, ge=0, le=100)
    missing_fields:        List[str] = Field(default_factory=list)


class FinancialDataResponse(BaseModel):
    """Top-level wrapper matching the Stage 1 prompt's `{"financial_data": {...}}` root key."""
    financial_data: FinancialDataCollection


# ═══════════════════════════════════════════════════════════════════════════════
# STAGE 2 — DECISION-ORIENTED ANALYSIS (verdicts, scores, no raw sourcing)
# ═══════════════════════════════════════════════════════════════════════════════

class QuickScorecardItem(_StrictModel):
    category: Literal["Revenue", "Profit", "Margins", "Debt", "Valuation", "Audit"]
    verdict:  Verdict


class QuickDecode(_StrictModel):
    growth:               YesNo
    debt_control:         YesNo
    valuation_reasonable: YesNo
    strong_candidate:     bool


class MetricVerdictItem(_StrictModel):
    """Single scored metric line, used for both income_statement and balance_sheet sections."""
    metric:  str
    value:   FlexStr = None
    verdict: Verdict
    note:    Optional[str] = None


class ManagementEfficiencyScore(_StrictModel):
    """
    10-point scoring breakdown:
      growth (0-3) + profitability (0-2) + cash_quality (0-2)
      + debt_safety (-1 to 2) + valuation (0-1) = total (max 10)
    """
    growth:         float = Field(ge=0, le=3)
    profitability:  ScoreInt = Field(ge=0, le=2)
    cash_quality:   ScoreInt = Field(ge=0, le=2)
    debt_safety:    ScoreInt = Field(ge=-1, le=2)
    valuation:      ScoreInt = Field(ge=0, le=1)
    total:          float
    max:            int = 10
    rating:         Rating
    deductions:     Optional[str] = None


class ValuationAnalysisResult(_StrictModel):
    current_pe:    FlexStr = None
    historical_pe: FlexStr = None
    peg:           FlexStr = None
    verdict:       ValuationVerdict
    note:          Optional[str] = None


class ManagementCommentaryAnalysis(_StrictModel):
    guidance:        Optional[str] = None
    capex:           Optional[str] = None
    growth_drivers:  List[str] = Field(default_factory=list)
    risks:           List[str] = Field(default_factory=list)


class HistoricalTrendAnalysis(_StrictModel):
    revenue_trend: TrendDirection
    margin_trend:  MarginTrend
    note:          Optional[str] = None


class EstimateVsActual(_StrictModel):
    estimate: FlexStr = None
    actual:   FlexStr = None


class ExpectationVsActual(_StrictModel):
    revenue:         EstimateVsActual = Field(default_factory=EstimateVsActual)
    eps:              EstimateVsActual = Field(default_factory=EstimateVsActual)
    overall_verdict:  BeatInlineMiss


class PeerComparisonResult(_StrictModel):
    company_name: str
    position:     PeerPosition
    note:         Optional[str] = None


class AuditFlagItem(_StrictModel):
    category: str
    detail:   str


class AuditFlags(_StrictModel):
    """Only actually-flagged (non-clean) audit items are listed — clean items are omitted."""
    audit_risk_score: ScoreInt = Field(ge=0, le=10)
    risk_level:       RiskLevel
    flags:            List[AuditFlagItem] = Field(default_factory=list)


class RiskDashboardItem(_StrictModel):
    """Only Medium/High severity risks are reported; Low-severity risks are omitted."""
    risk:     str
    severity: RiskSeverity


class InvestmentVerdict(_StrictModel):
    suitable_for:     List[str] = Field(default_factory=list)
    not_suitable_for: List[str] = Field(default_factory=list)
    top_reasons:      List[str] = Field(default_factory=list)
    top_risks:        List[str] = Field(default_factory=list)


class AIConfidence(_StrictModel):
    data_completeness:            ScoreInt = Field(ge=0, le=100)
    missing_critical_information: List[str] = Field(default_factory=list)


class EarningsAnalysis(_StrictModel):
    """
    Complete output of Earnings Stage 2 — the `earnings_analysis` object.
    Pure verdicts/scores derived from Stage 1 data. No web search, no new facts.
    """
    headline:            str
    executive_summary:   str = Field(max_length=1500)  # ~150 words soft budget, hard char ceiling
    overall_score:        ScoreInt = Field(ge=0, le=10)
    overall_rating:       Rating
    recommendation:       Recommendation
    confidence:           ScoreInt = Field(ge=0, le=100)

    quick_scorecard: List[QuickScorecardItem] = Field(default_factory=list)
    quick_decode:    QuickDecode

    income_statement: List[MetricVerdictItem] = Field(default_factory=list)
    balance_sheet:    List[MetricVerdictItem] = Field(default_factory=list)

    management_efficiency: ManagementEfficiencyScore
    valuation_analysis:    ValuationAnalysisResult
    management_commentary: ManagementCommentaryAnalysis = Field(default_factory=ManagementCommentaryAnalysis)
    historical_trend:      HistoricalTrendAnalysis
    expectation_vs_actual: ExpectationVsActual
    peer_comparison:       List[PeerComparisonResult] = Field(default_factory=list)

    audit_flags:     AuditFlags
    risk_dashboard:  List[RiskDashboardItem] = Field(default_factory=list)
    investment_verdict: InvestmentVerdict
    ai_confidence:      AIConfidence


class EarningsAnalysisResponse(BaseModel):
    """Top-level wrapper matching the Stage 2 prompt's `{"earnings_analysis": {...}}` root key."""
    earnings_analysis: EarningsAnalysis


# ═══════════════════════════════════════════════════════════════════════════════
# COMBINED REPORT — Stage 1 + Stage 2 persisted together
# ═══════════════════════════════════════════════════════════════════════════════

class EarningsReport(BaseModel):
    """
    The full persisted Earnings Intelligence Report: raw sourced data (Stage 1)
    plus derived analysis (Stage 2), stored together for audit trail and re-analysis.

    company/quarter/fiscal_year/report_date are intentionally NOT duplicated at
    this level — they already live in `financial_data` (Stage 1's own reported
    values). Read via report.financial_data.company, etc. The pipeline's
    externally-resolved company name (from the news evaluation step, which may
    differ from the LLM's self-reported company string) is tracked separately
    by the caller — see generator.py — rather than stored twice here.
    """
    financial_data:  FinancialDataCollection
    analysis:        EarningsAnalysis

    generated_at:    Optional[str] = None  # ISO8601 timestamp, set by the pipeline runner


# ═══════════════════════════════════════════════════════════════════════════════
# API RESPONSE WRAPPERS
# ═══════════════════════════════════════════════════════════════════════════════

class EarningsReportResponse(BaseModel):
    success: bool = True
    report:  EarningsReport


class EarningsReportListResponse(BaseModel):
    success:   bool
    page:      int
    page_size: int
    total:     int
    reports:   List[EarningsReport]