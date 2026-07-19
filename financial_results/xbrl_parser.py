"""
financial_results/xbrl_parser.py
--------------------------------
XML/XBRL extractor for financial result filings.

Extracts BOTH metadata AND financial line items from NSE/BSE XBRL
filings. Supports two taxonomy variants:
    • IFIndAs   — standard non-banking companies
    • IFBanking — banks, NBFCs, financial institutions

Public API:
    extract_xbrl_metadata(content)    → dict of metadata fields
    extract_xbrl_financials(content)  → dict of financial line items
"""
from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

try:
    from lxml import etree
    _HAS_LXML = True
except ImportError:  # pragma: no cover
    _HAS_LXML = False
    logger.warning("[XBRL] lxml not available — XML/XBRL parsing disabled")


# ─── Quarter helpers ─────────────────────────────────────────────────────

_QUARTER_WORD_TO_NUM = {
    "first": "Q1",
    "second": "Q2",
    "third": "Q3",
    "fourth": "Q4",
}

# NSE/BSE quarters run on the Indian financial year (Apr–Mar):
#   Q1 = Apr-Jun, Q2 = Jul-Sep, Q3 = Oct-Dec, Q4 = Jan-Mar
_QUARTER_BY_MONTH = {
    4: "Q1", 5: "Q1", 6: "Q1",
    7: "Q2", 8: "Q2", 9: "Q2",
    10: "Q3", 11: "Q3", 12: "Q3",
    1: "Q4", 2: "Q4", 3: "Q4",
}

# Half-yearly periods: Sep = H1, Mar = H2
_HALF_YEAR_BY_MONTH = {
    9: "H1",
    3: "H2",
    # Some companies end on different months
    6: "H1",
    12: "H2",
}

_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")


def _quarter_from_reporting_quarter(text: str) -> str | None:
    """'First quarter' -> 'Q1', 'Second Quarter' -> 'Q2', etc.
    Returns None for non-quarterly labels like 'Yearly' (handled via period_type).
    """
    if not text:
        return None
    key = text.strip().lower().split()[0]
    return _QUARTER_WORD_TO_NUM.get(key)  # returns None for 'yearly', 'half', etc.


def _derive_period_label(
    period_type: str | None,
    period_end: str | None,
    reporting_quarter_raw: str | None,
) -> str | None:
    """Derive the canonical period label (Q1/Q2/Q3/Q4/H1/H2/FY) based on
    TypeOfReportingPeriod + end date + ReportingQuarter tag.

    Rules:
        - If period_type is 'Half Yearly': derive H1 or H2 from period_end month.
        - If period_type is 'Annual' or 'Yearly': return 'FY'.
        - If period_type is 'Quarterly': try ReportingQuarter word first,
          then fall back to period_end month.
        - If period_type is absent: same as 'Quarterly' fallback.
    """
    pt = (period_type or "").strip().lower()

    if "half" in pt:  # "Half Yearly"
        if period_end:
            m = _DATE_RE.match(period_end)
            if m:
                month = int(m.group(2))
                return _HALF_YEAR_BY_MONTH.get(month, "H1")
        return "H1"  # best guess if no date

    if "annual" in pt or pt == "yearly":  # "Annual" or "Yearly"
        return "FY"

    # Quarterly (or unknown period type)
    if reporting_quarter_raw:
        q = _quarter_from_reporting_quarter(reporting_quarter_raw)
        if q:
            return q

    # Last resort: derive from period_end month
    if period_end:
        m = _DATE_RE.match(period_end)
        if m:
            month = int(m.group(2))
            return _QUARTER_BY_MONTH.get(month)

    return None


def _quarter_from_date(date_str: str) -> str | None:
    m = _DATE_RE.match(date_str or "")
    if not m:
        return None
    month = int(m.group(2))
    return _QUARTER_BY_MONTH.get(month)


def _fiscal_year_label(fy_start: str | None, fy_end: str | None) -> str | None:
    """Build 'YYYY-YY' style Indian FY label, e.g. 2026-04-01/2027-03-31 -> '2026-27'."""
    start_year = None
    end_year = None
    if fy_start:
        m = _DATE_RE.match(fy_start)
        if m:
            start_year = int(m.group(1))
    if fy_end:
        m = _DATE_RE.match(fy_end)
        if m:
            end_year = int(m.group(1))

    if start_year and end_year:
        return f"{start_year}-{str(end_year)[-2:]}"
    if start_year:
        return f"{start_year}-{str(start_year + 1)[-2:]}"
    if end_year:
        return f"{end_year - 1}-{str(end_year)[-2:]}"
    return None


def _month_abbrev(date_str: str) -> str | None:
    m = _DATE_RE.match(date_str or "")
    if not m:
        return None
    months = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    return months[int(m.group(2))]


# ─── Core tag-name field maps ───────────────────────────────────────────
# Ordered by preference: first match wins for a given field.

_COMPANY_NAME_TAGS = [
    "NameOfTheCompany",   # in-capmkt (IFIndAs) — non-bank companies
    "NameOfCompany",
    "NameOfBank",         # in-capmkt (IFBanking) — banks
    "EntityName",
    "NameOfTheEntity",
]

_SYMBOL_TAGS = ["Symbol", "TradingSymbol"]
_SCRIP_CODE_TAGS = ["ScripCode", "SecurityCode"]
_ISIN_TAGS = ["ISIN"]
_MSEI_SYMBOL_TAGS = ["MSEISymbol"]
_CLASS_OF_SECURITY_TAGS = ["ClassOfSecurity"]

_FY_START_TAGS = ["DateOfStartOfFinancialYear"]
_FY_END_TAGS = ["DateOfEndOfFinancialYear"]

_PERIOD_START_TAGS = ["DateOfStartOfReportingPeriod"]
_PERIOD_END_TAGS = ["DateOfEndOfReportingPeriod"]

_REPORTING_PERIOD_TYPE_TAGS = ["TypeOfReportingPeriod"]  # Quarterly / Half Yearly / Annual
_REPORTING_QUARTER_TAGS = ["ReportingQuarter"]            # First quarter / Second quarter / ...

_AUDIT_STATUS_TAGS = [
    "WhetherResultsAreAuditedOrUnaudited",
    "AuditStatus",
    "TypeOfAuditReport",
]

_STANDALONE_CONSOLIDATED_TAGS = [
    "NatureOfReportStandaloneConsolidated",
    "NatureOfReport",
    "TypeOfReport",
    "NatureOfFinancialStatement",
    "ResultNature",
]

_BOARD_MEETING_DATE_TAGS = ["DateOfBoardMeetingWhenFinancialResultsWereApproved"]
_PRIOR_INTIMATION_DATE_TAGS = [
    "DateOnWhichPriorIntimationOfTheMeetingForConsideringFinancialResultsWasInformedToTheExchange"
]

_MULTISEGMENT_TAGS = ["IsCompanyReportingMultisegmentOrSingleSegment"]

# Legacy in-gaap era auditor tags — kept as a fallback since the newer
# in-capmkt taxonomy generally does not carry an explicit auditor-name
# fact in the quarterly result filing itself.
_AUDITOR_TAGS = [
    "NameOfAuditFirm",
    "AuditorName",
    "NameOfAuditor",
    "AuditFirmName",
]


def _local(el) -> str:
    return etree.QName(el).localname


def _find_all_by_localname(tree, tag: str):
    """All elements anywhere in the tree whose localname matches `tag`,
    regardless of namespace."""
    return [el for el in tree.iter() if isinstance(el.tag, str) and _local(el) == tag]


def _contexts_without_scenario(tree) -> set[str]:
    """IDs of xbrli:context elements that have NO xbrldi scenario/dimension
    child — i.e. plain entity-level contexts (not segment/expense
    breakdown rows). Matched by localname so it works regardless of the
    bound xbrli namespace URI."""
    result = set()
    for ctx in _find_all_by_localname(tree, "context"):
        ctx_id = ctx.get("id")
        if not ctx_id:
            continue
        has_scenario = any(_local(child) == "scenario" for child in ctx)
        if not has_scenario:
            result.add(ctx_id)
    return result


def _first_value(tree, tags: list[str], plain_context_ids: set[str] | None = None) -> str | None:
    """Return the text of the first non-empty element matching any tag in
    `tags` (checked in order). If plain_context_ids is given, elements
    whose contextRef is in that set are preferred over ones that aren't
    (so we don't grab a segment/expense-row duplicate)."""
    for tag in tags:
        candidates = _find_all_by_localname(tree, tag)
        if not candidates:
            continue

        if plain_context_ids:
            preferred = [
                el for el in candidates
                if el.get("contextRef") in plain_context_ids and el.text and el.text.strip()
            ]
            if preferred:
                return preferred[0].text.strip()

        for el in candidates:
            if el.text and el.text.strip():
                return el.text.strip()
    return None


def extract_xbrl_metadata(content: bytes) -> dict[str, Any]:
    """Extract metadata fields from an NSE/BSE XML/XBRL financial result
    filing (in-capmkt taxonomy — covers both non-bank "IFIndAs" and bank
    "IFBanking" variants).

    Args:
        content: Raw XML/XBRL bytes.

    Returns:
        Dict with any of: company_name, symbol, scrip_code, isin,
        msei_symbol, class_of_security, fiscal_year_start, fiscal_year_end,
        financial_year, period_start, period_end,
        quarter, period_type, period_label, filing_type,
        standalone_consolidated, board_meeting_date,
        prior_intimation_date, is_multisegment, auditor.
        Missing fields are omitted (not set to None).
    """
    if not _HAS_LXML:
        logger.warning("[XBRL] lxml unavailable — returning empty metadata")
        return {}

    meta: dict[str, Any] = {}

    try:
        tree = etree.fromstring(content)
    except Exception as exc:
        logger.warning("[XBRL] Parse failed: %s", exc)
        return meta

    plain_ctx = _contexts_without_scenario(tree)

    # ── Identifiers ──────────────────────────────────────────────────────
    company_name = _first_value(tree, _COMPANY_NAME_TAGS, plain_ctx)
    if company_name:
        meta["company_name"] = company_name

    symbol = _first_value(tree, _SYMBOL_TAGS, plain_ctx)
    if symbol:
        meta["symbol"] = symbol

    scrip_code = _first_value(tree, _SCRIP_CODE_TAGS, plain_ctx)
    if scrip_code:
        meta["scrip_code"] = scrip_code

    isin = _first_value(tree, _ISIN_TAGS, plain_ctx)
    if isin:
        meta["isin"] = isin

    msei_symbol = _first_value(tree, _MSEI_SYMBOL_TAGS, plain_ctx)
    if msei_symbol:
        meta["msei_symbol"] = msei_symbol

    class_of_security = _first_value(tree, _CLASS_OF_SECURITY_TAGS, plain_ctx)
    if class_of_security:
        meta["class_of_security"] = class_of_security

    # ── Fiscal year ───────────────────────────────────────────────────────
    fy_start = _first_value(tree, _FY_START_TAGS, plain_ctx)
    fy_end = _first_value(tree, _FY_END_TAGS, plain_ctx)
    if fy_start:
        meta["fiscal_year_start"] = fy_start
    if fy_end:
        meta["fiscal_year_end"] = fy_end
    fy_label = _fiscal_year_label(fy_start, fy_end)
    if fy_label:
        meta["financial_year"] = fy_label  # e.g. "2026-27"

    # ── Reporting period (the actual quarter's date range) ───────────────
    period_start = _first_value(tree, _PERIOD_START_TAGS, plain_ctx)
    period_end = _first_value(tree, _PERIOD_END_TAGS, plain_ctx)
    if period_start:
        meta["period_start"] = period_start
    if period_end:
        meta["period_end"] = period_end

    # Fallback: if the explicit reporting-period tags are absent, fall
    # back to scanning xbrli:context/period (older-style filings or
    # partial extracts) — only from a plain (non-dimensional) context.
    if not period_start or not period_end:
        for ctx in _find_all_by_localname(tree, "context"):
            if ctx.get("id") not in plain_ctx:
                continue
            period_el = next((c for c in ctx if _local(c) == "period"), None)
            if period_el is None:
                continue
            start_el = next((c for c in period_el if _local(c) == "startDate"), None)
            end_el = next((c for c in period_el if _local(c) == "endDate"), None)
            instant_el = next((c for c in period_el if _local(c) == "instant"), None)
            if not period_start and start_el is not None and start_el.text:
                meta.setdefault("period_start", start_el.text.strip())
            if not period_end and end_el is not None and end_el.text:
                meta.setdefault("period_end", end_el.text.strip())
            if not period_end and instant_el is not None and instant_el.text:
                meta.setdefault("period_end", instant_el.text.strip())
            if "period_start" in meta or "period_end" in meta:
                break

    # ── Period type / quarter ────────────────────────────────────────────
    period_type = _first_value(tree, _REPORTING_PERIOD_TYPE_TAGS, plain_ctx)
    if period_type:
        meta["period_type"] = period_type  # "Quarterly" / "Half Yearly" / "Annual"

    reporting_quarter_raw = _first_value(tree, _REPORTING_QUARTER_TAGS, plain_ctx)
    if reporting_quarter_raw:
        meta["reporting_quarter_label"] = reporting_quarter_raw  # e.g. "First quarter"

    # Derive the canonical period label (Q1/Q2/Q3/Q4/H1/H2/FY) using the
    # period_type-aware helper — this correctly handles Half Yearly and Annual
    # filings that would otherwise be mis-mapped to Q4 via a raw month lookup.
    quarter = _derive_period_label(
        period_type=period_type,
        period_end=meta.get("period_end"),
        reporting_quarter_raw=reporting_quarter_raw,
    )

    if quarter:
        meta["quarter"] = quarter

    # ── Friendly combined label, e.g. "Q1 FY2026-27 (Apr-Jun 2026)" ──────
    if quarter and fy_label:
        prefix = quarter  # e.g. Q1, H1, FY
        if prefix == "FY":
            label = f"FY {fy_label}"
        else:
            label = f"{prefix} FY {fy_label}"
        if period_start and period_end:
            m_start = _month_abbrev(period_start)
            m_end = _month_abbrev(period_end)
            year_end = _DATE_RE.match(period_end)
            if m_start and m_end and year_end:
                label += f" ({m_start}-{m_end} {year_end.group(1)})"
        meta["period_label"] = label

    # ── Audited / Unaudited ───────────────────────────────────────────────
    filing_type = _first_value(tree, _AUDIT_STATUS_TAGS, plain_ctx)
    if filing_type:
        meta["filing_type"] = filing_type

    # ── Standalone / Consolidated ─────────────────────────────────────────
    nature_raw = _first_value(tree, _STANDALONE_CONSOLIDATED_TAGS, plain_ctx)
    if nature_raw:
        val = nature_raw.strip().lower()
        if "consolidat" in val:
            meta["standalone_consolidated"] = "Consolidated"
        elif "standalone" in val:
            meta["standalone_consolidated"] = "Standalone"
        else:
            meta["standalone_consolidated"] = nature_raw.strip()

    # ── Board meeting / disclosure dates ─────────────────────────────────
    board_meeting_date = _first_value(tree, _BOARD_MEETING_DATE_TAGS, plain_ctx)
    if board_meeting_date:
        meta["board_meeting_date"] = board_meeting_date

    prior_intimation_date = _first_value(tree, _PRIOR_INTIMATION_DATE_TAGS, plain_ctx)
    if prior_intimation_date:
        meta["prior_intimation_date"] = prior_intimation_date

    # ── Segment reporting flag ───────────────────────────────────────────
    multisegment_raw = _first_value(tree, _MULTISEGMENT_TAGS, plain_ctx)
    if multisegment_raw:
        meta["is_multisegment"] = "multi" in multisegment_raw.strip().lower()

    # ── Auditor (legacy in-gaap taxonomy fallback; usually absent in the
    #    current in-capmkt quarterly filing itself) ──────────────────────
    auditor = _first_value(tree, _AUDITOR_TAGS, plain_ctx)
    if auditor:
        meta["auditor"] = auditor

    logger.info("[XBRL] Extracted %d metadata fields", len(meta))
    return meta


# ═══════════════════════════════════════════════════════════════════════════════
# FINANCIAL DATA EXTRACTION
# ═══════════════════════════════════════════════════════════════════════════════

# ─── Non-banking (IFIndAs) financial tags ────────────────────────────────────
# Tag name → output key.  All values are in INR (converted to Crores later).

_INDAS_FINANCIAL_TAGS: dict[str, str] = {
    # Income
    "RevenueFromOperations":       "revenue_from_operations",
    "OtherIncome":                 "other_income",
    # Expenses
    "CostOfMaterialsConsumed":     "cost_of_materials",
    "PurchasesOfStockInTrade":     "purchase_of_stock_in_trade",
    "ChangesInInventoriesOfFinishedGoodsWorkInProgressAndStockInTrade": "changes_in_inventories",
    "EmployeeBenefitExpense":      "employee_benefit_expense",
    "FinanceCosts":                "finance_costs",
    "DepreciationDepletionAndAmortisationExpense": "depreciation",
    "OtherExpenses":               "other_expenses",
    "Expenses":                    "total_expenses",
    # Profitability
    "ProfitBeforeExceptionalItemsAndTax": "profit_before_exceptional",
    "ExceptionalItemsBeforeTax":   "exceptional_items",
    "ProfitBeforeTax":             "profit_before_tax",
    "CurrentTax":                  "current_tax",
    "DeferredTax":                 "deferred_tax",
    "TaxExpense":                  "tax_expense",
    "ProfitLossForPeriodFromContinuingOperations": "profit_continuing",
    "ProfitLossForPeriod":         "profit_after_tax",
    # Comprehensive income
    "OtherComprehensiveIncomeNetOfTaxes": "other_comprehensive_income",
    "ComprehensiveIncomeForThePeriod":    "total_comprehensive_income",
    # Equity
    "PaidUpValueOfEquityShareCapital": "paid_up_equity_capital",
}

# EPS tags for IFIndAs — these use INRPerShare unit, NOT INR
_INDAS_EPS_TAGS: dict[str, str] = {
    "FaceValueOfEquityShareCapital": "face_value",
    "BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations": "basic_eps",
    "DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations": "diluted_eps",
    # Fallbacks (some filings use shorter tag names)
    "BasicEarningsLossPerShareFromContinuingOperations": "basic_eps",
    "DilutedEarningsLossPerShareFromContinuingOperations": "diluted_eps",
}

# ─── Banking (IFBanking) financial tags ──────────────────────────────────────

_BANKING_FINANCIAL_TAGS: dict[str, str] = {
    # Income
    "InterestEarned":              "interest_earned",
    "OtherIncome":                 "other_income",
    # Expenditure
    "InterestExpended":            "interest_expended",
    "EmployeesCost":               "employees_cost",
    "OtherOperatingExpenses":      "other_operating_expenses",
    "OperatingExpenses":           "operating_expenses_bank",
    "ExpenditureExcludingProvisionsAndContingencies": "total_expenditure_ex_provisions",
    # Profitability
    "OperatingProfitBeforeProvisionAndContingencies": "operating_profit",
    "ProvisionsOtherThanTaxAndContingencies": "provisions",
    "ExceptionalItems":            "exceptional_items",
    "ProfitLossFromOrdinaryActivitiesBeforeTax": "profit_before_tax",
    "TaxExpense":                  "tax_expense",
    "ProfitLossFromOrdinaryActivitiesAfterTax": "profit_after_tax_before_extraordinary",
    "ExtraordinaryItems":          "extraordinary_items",
    "ProfitLossForThePeriod":      "profit_after_tax",
    # Minority / associates (consolidated)
    "ShareOfProfitLossOfAssociates": "share_of_associates",
    "ProfitLossOfMinorityInterest": "minority_interest",
    "ProfitLossAfterTaxesMinorityInterestAndShareOfProfitLossOfAssociates": "attributable_profit",
    # Equity
    "PaidUpValueOfEquityShareCapital": "paid_up_equity_capital",
    # NPA
    "GrossNonPerformingAssets":    "gross_npa_amount",
    "NonPerformingAssets":         "net_npa_amount",
}

# Banking EPS tags — use INRPerShare unit
_BANKING_EPS_TAGS: dict[str, str] = {
    "FaceValueOfEquityShareCapital": "face_value",
    "BasicEarningsPerShareAfterExtraordinaryItems": "basic_eps",
    "DilutedEarningsPerShareAfterExtraordinaryItems": "diluted_eps",
    # Fallbacks
    "BasicEarningsPerShareBeforeExtraordinaryItems": "basic_eps",
    "DilutedEarningsPerShareBeforeExtraordinaryItems": "diluted_eps",
}

# Banking ratio tags — use 'pure' unit (ratios, not currency)
_BANKING_RATIO_TAGS: dict[str, str] = {
    "PercentageOfGrossNpa":          "gross_npa_pct",
    "PercentageOfNpa":               "net_npa_pct",
    "ReturnOnAssets":                "return_on_assets",
    "CET1Ratio":                     "cet1_ratio",
    "PercentageOfShareHeldByGovernmentOfIndia": "govt_holding_pct",
}

# ─── Banking schema detection tags ───────────────────────────────────────────
_BANKING_INDICATOR_TAGS = frozenset({
    "InterestEarned",
    "InterestExpended",
    "NameOfBank",
    "OperatingProfitBeforeProvisionAndContingencies",
    "GrossNonPerformingAssets",
})


def _numeric_value(el) -> float | None:
    """Extract the numeric value from an XBRL element, returning None on failure."""
    text = (el.text or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except (ValueError, TypeError):
        return None


def _is_banking_filing(tree) -> bool:
    """Auto-detect whether this XBRL filing uses the IFBanking taxonomy."""
    for tag in _BANKING_INDICATOR_TAGS:
        if _find_all_by_localname(tree, tag):
            return True
    return False


def extract_xbrl_financials(content: bytes) -> dict[str, Any]:
    """Extract financial line items from an NSE/BSE XBRL filing.

    Auto-detects banking vs non-banking schema and extracts the
    appropriate tags. All INR values are converted to ₹ Crores.
    EPS and ratio values are kept as-is.

    Args:
        content: Raw XML/XBRL bytes.

    Returns:
        Dict with financial fields. Includes 'is_banking' bool.
        Missing/unparseable fields are omitted.
    """
    if not _HAS_LXML:
        logger.warning("[XBRL/Fin] lxml unavailable — returning empty financials")
        return {}

    try:
        tree = etree.fromstring(content)
    except Exception as exc:
        logger.warning("[XBRL/Fin] Parse failed: %s", exc)
        return {}

    plain_ctx = _contexts_without_scenario(tree)
    is_banking = _is_banking_filing(tree)

    result: dict[str, Any] = {"is_banking": is_banking}

    # Read level of rounding (for info; conversion is always raw INR → Crores)
    rounding = _first_value(tree, ["LevelOfRounding"], plain_ctx)
    if rounding:
        result["level_of_rounding"] = rounding

    # ── Select the appropriate tag maps ──────────────────────────────────
    if is_banking:
        inr_tags = _BANKING_FINANCIAL_TAGS
        eps_tags = _BANKING_EPS_TAGS
        ratio_tags = _BANKING_RATIO_TAGS
    else:
        inr_tags = _INDAS_FINANCIAL_TAGS
        eps_tags = _INDAS_EPS_TAGS
        ratio_tags = {}

    # ── Extract INR values (convert to Crores) ───────────────────────────
    for tag_name, field_key in inr_tags.items():
        if field_key in result:
            continue  # Already set by a higher-priority tag
        candidates = _find_all_by_localname(tree, tag_name)
        for el in candidates:
            ctx = el.get("contextRef", "")
            if ctx not in plain_ctx:
                continue
            unit = el.get("unitRef", "")
            if unit != "INR":
                continue
            val = _numeric_value(el)
            if val is not None:
                result[field_key] = round(val / 1e7, 2)  # INR → Crores
                break

    # ── Extract EPS values (keep as-is, ₹ per share) ─────────────────────
    for tag_name, field_key in eps_tags.items():
        if field_key in result:
            continue
        candidates = _find_all_by_localname(tree, tag_name)
        for el in candidates:
            ctx = el.get("contextRef", "")
            if ctx not in plain_ctx:
                continue
            val = _numeric_value(el)
            if val is not None:
                result[field_key] = val
                break

    # ── Extract ratio values (keep as-is) ────────────────────────────────
    for tag_name, field_key in ratio_tags.items():
        if field_key in result:
            continue
        candidates = _find_all_by_localname(tree, tag_name)
        for el in candidates:
            ctx = el.get("contextRef", "")
            if ctx not in plain_ctx:
                continue
            val = _numeric_value(el)
            if val is not None:
                result[field_key] = val
                break

    logger.info(
        "[XBRL/Fin] Extracted %d financial fields (banking=%s)",
        len(result) - 1, is_banking,  # -1 for is_banking flag itself
    )
    return result