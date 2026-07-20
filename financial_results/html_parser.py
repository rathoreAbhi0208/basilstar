"""
financial_results/html_parser.py
--------------------------------
HTML/iXBRL extractor for financial result filings (NSE/BSE
"Integrated Filing (Finance)" Ind-AS documents and legacy HTML filings).

Extracts BOTH metadata AND financial line items from HTML documents.

Public API:
    extract_html_metadata(content)    → dict of metadata fields
    extract_html_financials(content)  → dict of financial line items
"""
from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

try:
    from bs4 import BeautifulSoup
    _HAS_BS4 = True
except ImportError:  # pragma: no cover
    _HAS_BS4 = False
    logger.warning("[HTML] beautifulsoup4 not available — HTML parsing disabled")


# ─── iXBRL tag name → metadata field map ──────────────────────────────────
#
# Keys are lowercased local names as they appear in the `name=` attribute
# of <ix:nonNumeric>/<ix:nonFraction> tags (namespace prefix stripped).
# NSE/BSE Integrated Filing (Ind-AS) documents use the "in-capmkt:" prefix;
# legacy filings sometimes use "in-bse:" or no prefix at all — all are
# normalised the same way before lookup.

_IXBRL_COMPANY_NAME_TAGS = {
    "nameofthecompany",
    "nameofcompany",
    "entityname",
}
_IXBRL_SCRIP_CODE_TAGS = {
    "scripcode",
    "securitycode",
}
_IXBRL_SYMBOL_TAGS = {
    "symbol",              # NSE Symbol
}
_IXBRL_ISIN_TAGS = {"isin"}
_IXBRL_STANDALONE_CONSOLIDATED_TAGS = {
    "natureofreportstandaloneconsolidated",
}
_IXBRL_AUDITED_UNAUDITED_TAGS = {
    "whetherresultsareauditedorunaudited",
}
_IXBRL_REPORTING_QUARTER_TAGS = {"reportingquarter"}
_IXBRL_TYPE_OF_REPORTING_PERIOD_TAGS = {"typeofreportingperiod"}
_IXBRL_PERIOD_START_TAGS = {"dateofstartofreportingperiod"}
_IXBRL_PERIOD_END_TAGS = {"dateofendofreportingperiod"}
_IXBRL_FY_START_TAGS = {"dateofstartoffinancialyear"}
_IXBRL_FY_END_TAGS = {"dateofendoffinancialyear"}
_IXBRL_AUDITOR_NAME_TAGS = {"auditorsfirmname"}

# contextRef suffixes that mark "current quarter" (as opposed to
# "year to date") facts in the SEBI in-capmkt taxonomy. Exact context IDs
# vary per filing (OneD/OneI are the conventional current-period IDs,
# FourD/FourI are year-to-date), so we match by prefix/keyword rather
# than a fixed literal set.
_CURRENT_PERIOD_CONTEXT_HINTS = ("one", "current", "qtr", "quarter")
_YTD_CONTEXT_HINTS = ("four", "ytd", "yeartodate", "annual")

# Reporting-quarter text → canonical Q1-Q4 label.
_QUARTER_TEXT_MAP = {
    "first quarter": "Q1",
    "second quarter": "Q2",
    "third quarter": "Q3",
    "fourth quarter": "Q4",
    "half yearly": "H1",
    "half year": "H1",
    "annual": "FY",
}

# Generic placeholder <title>/company-name values that must never be
# stored as the company name (kept for callers that also inspect <title>).
_TITLE_PLACEHOLDERS = frozenset({
    "company results",
    "financial results",
    "quarterly results",
    "annual results",
    "announcement",
    "integrated filing",
    "integrated filing (finance) ind as",
    "untitled",
    "result",
})


def _local_name(name_attr: str) -> str:
    """Strip an XBRL namespace prefix (e.g. 'in-capmkt:') and lowercase."""
    name_attr = (name_attr or "").strip().lower()
    if ":" in name_attr:
        name_attr = name_attr.rsplit(":", 1)[-1]
    return name_attr


def _context_is_current_period(context_ref: str) -> bool:
    ctx = (context_ref or "").lower()
    return any(h in ctx for h in _CURRENT_PERIOD_CONTEXT_HINTS) and not any(
        h in ctx for h in _YTD_CONTEXT_HINTS
    )


def _context_is_ytd(context_ref: str) -> bool:
    ctx = (context_ref or "").lower()
    return any(h in ctx for h in _YTD_CONTEXT_HINTS)


def _strip_hidden_elements(soup: "BeautifulSoup") -> None:
    """Remove elements that are not visible on the rendered page in place.

    This removes the `<ix:header>` block (XBRL context/unit definitions)
    and any element carrying `style="display:none"` / `display: none`,
    so full-page text scans only see the human-readable filing content.
    """
    # The ix:header block itself (contains all xbrli:context / xbrli:unit
    # definitions — pure metadata plumbing, not filing content).
    for tag in soup.find_all(re.compile(r"^ix:header$", re.IGNORECASE)):
        tag.decompose()

    # Any element explicitly hidden via inline style.
    hidden_pattern = re.compile(r"display\s*:\s*none", re.IGNORECASE)
    for tag in soup.find_all(style=hidden_pattern):
        tag.decompose()


def extract_html_metadata(content: bytes) -> dict[str, Any]:
    """Extract metadata fields from an HTML financial result filing.

    Searches iXBRL tags first (authoritative, unambiguous), then falls
    back to visible tables and free text for anything iXBRL doesn't
    supply (legacy non-iXBRL HTML filings).

    The <title> tag is intentionally not used for company_name because
    exchange filings frequently use generic titles like "Company Results"
    or "Integrated Filing (Finance) Ind AS" which would corrupt the
    database if stored.

    Args:
        content: Raw HTML bytes.

    Returns:
        Dict with any of: company_name, scrip_code, symbol, isin,
        period_start, period_end, financial_year, quarter,
        standalone_consolidated, filing_type, auditor.
        Missing fields are omitted.
    """
    if not _HAS_BS4:
        logger.warning("[HTML] beautifulsoup4 unavailable — returning empty metadata")
        return {}

    meta: dict[str, Any] = {}

    try:
        soup = BeautifulSoup(content, "lxml")
    except Exception:
        try:
            soup = BeautifulSoup(content, "html.parser")
        except Exception as exc:
            logger.warning("[HTML] Parse failed: %s", exc)
            return meta

    # Remove ix:header + any display:none content BEFORE anything else
    # touches this soup, so no downstream extraction — iXBRL or text —
    # can accidentally read context-definition noise.
    _strip_hidden_elements(soup)

    # ── iXBRL inline tags (authoritative source) ──────────────────────────
    # Track current-period vs YTD variants separately so we don't clobber
    # a quarter value with a year-to-date one or vice versa.
    _period_start_current: str | None = None
    _period_start_ytd: str | None = None
    _period_end_current: str | None = None
    _period_end_ytd: str | None = None
    _audited_current: str | None = None

    for tag in soup.find_all(re.compile(r"^ix:non(numeric|fraction)$", re.IGNORECASE)):
        local = _local_name(tag.get("name", ""))
        text = tag.get_text(strip=True)
        if not text:
            continue
        context_ref = tag.get("contextref") or tag.get("contextRef") or ""

        if local in _IXBRL_COMPANY_NAME_TAGS and "company_name" not in meta:
            meta["company_name"] = text
        elif local in _IXBRL_SCRIP_CODE_TAGS and "scrip_code" not in meta:
            meta["scrip_code"] = text
        elif local in _IXBRL_SYMBOL_TAGS and "symbol" not in meta:
            meta["symbol"] = text
        elif local in _IXBRL_ISIN_TAGS and "isin" not in meta:
            meta["isin"] = text
        elif local in _IXBRL_STANDALONE_CONSOLIDATED_TAGS and "standalone_consolidated" not in meta:
            low = text.lower()
            if "consolidat" in low:
                meta["standalone_consolidated"] = "Consolidated"
            elif "standalone" in low:
                meta["standalone_consolidated"] = "Standalone"
            else:
                meta["standalone_consolidated"] = text
        elif local in _IXBRL_REPORTING_QUARTER_TAGS and "quarter" not in meta:
            mapped = _QUARTER_TEXT_MAP.get(text.strip().lower())
            meta["quarter"] = mapped or text
        elif local in _IXBRL_TYPE_OF_REPORTING_PERIOD_TAGS and "reporting_period_type" not in meta:
            meta["reporting_period_type"] = text
        elif local in _IXBRL_AUDITOR_NAME_TAGS and "auditor" not in meta:
            meta["auditor"] = text
        elif local in _IXBRL_AUDITED_UNAUDITED_TAGS:
            if _context_is_current_period(context_ref) and _audited_current is None:
                _audited_current = text
            elif not _context_is_ytd(context_ref) and _audited_current is None:
                # Unknown/ambiguous context — still prefer as a fallback.
                _audited_current = text
        elif local in _IXBRL_FY_START_TAGS and "financial_year_start" not in meta:
            meta["financial_year_start"] = text
        elif local in _IXBRL_FY_END_TAGS and "financial_year_end" not in meta:
            meta["financial_year_end"] = text
        elif local in _IXBRL_PERIOD_START_TAGS:
            if _context_is_current_period(context_ref) and _period_start_current is None:
                _period_start_current = text
            elif _context_is_ytd(context_ref) and _period_start_ytd is None:
                _period_start_ytd = text
        elif local in _IXBRL_PERIOD_END_TAGS:
            if _context_is_current_period(context_ref) and _period_end_current is None:
                _period_end_current = text
            elif _context_is_ytd(context_ref) and _period_end_ytd is None:
                _period_end_ytd = text

    # Prefer the current-quarter period for period_start/period_end;
    # fall back to YTD only if the current-period tag wasn't found.
    if _period_start_current or _period_start_ytd:
        meta["period_start"] = _period_start_current or _period_start_ytd
    if _period_end_current or _period_end_ytd:
        meta["period_end"] = _period_end_current or _period_end_ytd
    if _audited_current:
        meta["filing_type"] = _audited_current

    # Derive financial_year from FY start/end iXBRL dates if we have them
    # and nothing else has set it yet (dates are typically dd-mm-yyyy).
    if "financial_year" not in meta:
        fy_end = meta.get("financial_year_end")
        if fy_end:
            y_match = re.search(r"(\d{4})\s*$", fy_end)
            if y_match:
                end_year = int(y_match.group(1))
                meta["financial_year"] = f"{end_year - 1}-{str(end_year)[-2:]}"

    # ── Meta tags (legacy fallback) ────────────────────────────────────────
    for meta_tag in soup.find_all("meta"):
        name_attr    = (meta_tag.get("name") or meta_tag.get("property") or "").lower()
        content_attr = meta_tag.get("content", "").strip()
        if not content_attr:
            continue
        if name_attr in ("company", "company-name") and "company_name" not in meta:
            if len(content_attr) > 2 and content_attr.strip().lower() not in _TITLE_PLACEHOLDERS:
                meta["company_name"] = content_attr
        elif name_attr in ("description", "og:description"):
            _extract_from_text(content_attr, meta)

    # ── Table-based extraction (fills gaps only) ─────────────────────────
    for table in soup.find_all("table"):
        for row in table.find_all("tr"):
            cells = row.find_all(["td", "th"])
            if len(cells) < 2:
                continue
            label = cells[0].get_text(strip=True).lower()
            value = cells[-1].get_text(strip=True)
            if not value:
                continue

            if any(k in label for k in ("company name", "name of company", "entity name")) and "company_name" not in meta:
                meta["company_name"] = value
            elif "scrip code" in label and "scrip_code" not in meta:
                meta["scrip_code"] = value
            elif ("nse symbol" in label or "trading symbol" in label or label.strip() == "symbol") and "symbol" not in meta:
                meta["symbol"] = value
            elif "isin" in label and "isin" not in meta:
                meta["isin"] = value
            elif any(k in label for k in ("period ended", "quarter ended", "period end")) and "period_end" not in meta:
                meta["period_end"] = value
            elif any(k in label for k in ("period from", "period start")) and "period_start" not in meta:
                meta["period_start"] = value
            elif "financial year" in label and "financial_year" not in meta:
                meta["financial_year"] = value
            elif "nature of report" in label and "standalone_consolidated" not in meta:
                low = value.lower()
                if "consolidat" in low:
                    meta["standalone_consolidated"] = "Consolidated"
                elif "standalone" in low:
                    meta["standalone_consolidated"] = "Standalone"
                else:
                    meta["standalone_consolidated"] = value
            elif ("audited" in label or "unaudited" in label) and "filing_type" not in meta:
                meta["filing_type"] = value
            elif any(k in label for k in ("auditor", "audit firm")) and "auditor" not in meta:
                meta["auditor"] = value

    # ── Full-text regex extraction (last resort — hidden nodes already
    #     stripped above, so this only ever sees visible content) ─────────
    full_text = soup.get_text(separator="\n", strip=True)
    _extract_from_text(full_text, meta)

    logger.info("[HTML] Extracted %d metadata fields", len(meta))
    return meta


# ─── Text pattern extraction (fallback only) ─────────────────────────────

def _extract_from_text(text: str, meta: dict[str, Any]) -> None:
    """Extract metadata from free-form text using regex patterns.

    Only fills fields not already populated by iXBRL tags or tables —
    this is a last-resort fallback for legacy non-iXBRL HTML filings.
    """

    if "financial_year" not in meta:
        fy_match = re.search(r"(?:FY\s*)?(\d{4})[-/](\d{2,4})", text)
        if fy_match:
            year1 = fy_match.group(1)
            year2 = fy_match.group(2)
            meta["financial_year"] = f"{year1}-{year2[-2:]}"

    if "quarter" not in meta:
        q_match = re.search(r"\b(Q[1-4])\b", text, re.IGNORECASE)
        if q_match:
            meta["quarter"] = q_match.group(1).upper()

    if "period_end" not in meta:
        period_match = re.search(
            r"(?:period|quarter)\s*(?:ended?|ending)\s*(?:on\s*)?"
            r"(\d{1,2}[-/]\w{3,9}[-/]\d{2,4}|\d{4}-\d{2}-\d{2})",
            text,
            re.IGNORECASE,
        )
        if period_match:
            meta["period_end"] = period_match.group(1)

    if "standalone_consolidated" not in meta:
        if re.search(r"\bconsolidat", text, re.IGNORECASE):
            meta["standalone_consolidated"] = "Consolidated"
        elif re.search(r"\bstandalone\b", text, re.IGNORECASE):
            meta["standalone_consolidated"] = "Standalone"

    if "filing_type" not in meta:
        if re.search(r"\bunaudited\b", text, re.IGNORECASE):
            meta["filing_type"] = "Unaudited"
        elif re.search(r"\baudited\b", text, re.IGNORECASE):
            meta["filing_type"] = "Audited"


# ═══════════════════════════════════════════════════════════════════════════════
# FINANCIAL DATA EXTRACTION FROM HTML
# ═══════════════════════════════════════════════════════════════════════════════

# ─── iXBRL financial tag maps (same tags as XBRL, found in ix:nonFraction) ──

# Non-banking (IFIndAs) — INR values
_IXBRL_INDAS_INR_TAGS: dict[str, str] = {
    "revenuefromoperations":       "revenue_from_operations",
    "otherincome":                 "other_income",
    "costofmaterialsconsumed":     "cost_of_materials",
    "purchasesofstockintrade":     "purchase_of_stock_in_trade",
    "changesinventoriesoffinishedgoodsworkinprogressandstockintrade": "changes_in_inventories",
    "employeebenefitexpense":      "employee_benefit_expense",
    "financecosts":                "finance_costs",
    "depreciationdepletionandamortisationexpense": "depreciation",
    "otherexpenses":               "other_expenses",
    "expenses":                    "total_expenses",
    "profitbeforeexceptionalitemsandtax": "profit_before_exceptional",
    "exceptionalitemsbeforetax":   "exceptional_items",
    "profitbeforetax":             "profit_before_tax",
    "taxexpense":                  "tax_expense",
    "profitlossforperiod":         "profit_after_tax",
    "profitlossforperiodfromcontinuingoperations": "profit_after_tax",
    "othercomprehensiveincomenetoftaxes": "other_comprehensive_income",
    "comprehensiveincomefortheperiod":    "total_comprehensive_income",
    "paidupvalueofequitysharecapital":    "paid_up_equity_capital",
}

# Non-banking EPS tags (INRPerShare unit)
_IXBRL_INDAS_EPS_TAGS: dict[str, str] = {
    "facevalueofequitysharecapital": "face_value",
    "basicearningslossperSharefromcontinuinganddiscontinuedoperations": "basic_eps",
    "dilutedearningslossperSharefromcontinuinganddiscontinuedoperations": "diluted_eps",
    "basicearningslosspersharefromcontinuingoperations": "basic_eps",
    "dilutedearningslosspersharefromcontinuingoperations": "diluted_eps",
    # Even more relaxed fallback tags
    "basicearningslosspershare": "basic_eps",
    "dilutedearningslosspershare": "diluted_eps",
}

# Banking (IFBanking) — INR values
_IXBRL_BANKING_INR_TAGS: dict[str, str] = {
    "interestearned":              "interest_earned",
    "otherincome":                 "other_income",
    "interestexpended":            "interest_expended",
    "employeescost":               "employees_cost",
    "operatingexpenses":           "operating_expenses_bank",
    "operatingprofitbeforeprovisionandcontingencies": "operating_profit",
    "provisionsotherthantaxandcontingencies": "provisions",
    "profitlossfromordinaryactivitiesbeforetax": "profit_before_tax",
    "taxexpense":                  "tax_expense",
    "profitlossfortheperiod":      "profit_after_tax",
    "profitlossofminorityinterest": "minority_interest",
    "profitlossaftertaxesminorityinterestandshareofprofitlossofassociates": "attributable_profit",
    "paidupvalueofequitysharecapital": "paid_up_equity_capital",
    "grossnonperformingassets":    "gross_npa_amount",
    "nonperformingassets":         "net_npa_amount",
}

# Banking EPS tags
_IXBRL_BANKING_EPS_TAGS: dict[str, str] = {
    "facevalueofequitysharecapital": "face_value",
    "basicearningspershareafterextraordinaryitems": "basic_eps",
    "dilutedearningspershareafterextraordinaryitems": "diluted_eps",
    "basicearningspersharebeforeextraordinaryitems": "basic_eps",
    "dilutedearningspersharebeforeextraordinaryitems": "diluted_eps",
}

# Banking ratio tags (pure unit)
_IXBRL_BANKING_RATIO_TAGS: dict[str, str] = {
    "percentageofgrossnpa":        "gross_npa_pct",
    "percentageofnpa":             "net_npa_pct",
    "returnonassets":              "return_on_assets",
    "cet1ratio":                   "cet1_ratio",
}

# Banking indicator tags (lowercase) — used to detect schema type
_IXBRL_BANKING_INDICATORS = frozenset({
    "interestearned",
    "interestexpended",
    "nameofbank",
    "operatingprofitbeforeprovisionandcontingencies",
    "grossnonperformingassets",
})


def extract_html_financials(content: bytes) -> dict[str, Any]:
    """Extract financial line items from an HTML/iXBRL filing.

    Searches ix:nonFraction tags first (authoritative), then falls back
    to table-based extraction for legacy non-iXBRL filings.

    Auto-detects banking vs non-banking schema.
    All INR values are converted to ₹ Crores.

    Args:
        content: Raw HTML bytes.

    Returns:
        Dict with financial fields. Includes 'is_banking' bool.
        Missing fields are omitted.
    """
    if not _HAS_BS4:
        logger.warning("[HTML/Fin] beautifulsoup4 unavailable — returning empty financials")
        return {}

    try:
        soup = BeautifulSoup(content, "lxml")
    except Exception:
        try:
            soup = BeautifulSoup(content, "html.parser")
        except Exception as exc:
            logger.warning("[HTML/Fin] Parse failed: %s", exc)
            return {}

    result: dict[str, Any] = {}

    # ── Phase 1: iXBRL ix:nonFraction tags ───────────────────────────────
    # First pass: detect banking schema + collect all numeric tags
    all_tags_found: set[str] = set()
    fraction_tags: list[tuple[str, str, str]] = []  # (local_name, text, contextRef)

    for tag in soup.find_all(re.compile(r"^ix:nonfraction$", re.IGNORECASE)):
        local = _local_name(tag.get("name", ""))
        text = tag.get_text(strip=True)
        context_ref = tag.get("contextref") or tag.get("contextRef") or ""
        if local and text:
            all_tags_found.add(local)
            fraction_tags.append((local, text, context_ref))

    # Detect banking schema
    is_banking = bool(all_tags_found & _IXBRL_BANKING_INDICATORS)
    result["is_banking"] = is_banking

    # Select tag maps
    if is_banking:
        inr_map = _IXBRL_BANKING_INR_TAGS
        eps_map = _IXBRL_BANKING_EPS_TAGS
        ratio_map = _IXBRL_BANKING_RATIO_TAGS
    else:
        inr_map = _IXBRL_INDAS_INR_TAGS
        eps_map = _IXBRL_INDAS_EPS_TAGS
        ratio_map = {}

    # Extract values from collected tags
    for local, text, context_ref in fraction_tags:
        # Only extract from current-period contexts
        if not _context_is_current_period(context_ref):
            continue

        # Try to parse the numeric value
        try:
            # iXBRL may format with commas or signs
            clean = text.replace(",", "").replace(" ", "").strip()
            # Handle negative values in parentheses: (1234) → -1234
            if clean.startswith("(") and clean.endswith(")"):
                clean = "-" + clean[1:-1]
            val = float(clean)
        except (ValueError, TypeError):
            continue

        # Check INR tags (convert to Crores)
        if local in inr_map:
            field_key = inr_map[local]
            if field_key not in result:
                result[field_key] = round(val / 1e7, 2)

        # Check EPS tags (keep as-is)
        elif local in eps_map:
            field_key = eps_map[local]
            if field_key not in result:
                result[field_key] = val

        # Check ratio tags (keep as-is)
        elif local in ratio_map:
            field_key = ratio_map[local]
            if field_key not in result:
                result[field_key] = val

    # ── Phase 2: Table fallback (legacy non-iXBRL filings) ───────────────
    # Only attempt if iXBRL extraction yielded very few results
    if len(result) <= 2:  # Only is_banking + maybe one field
        _extract_financials_from_tables(soup, result)

    logger.info(
        "[HTML/Fin] Extracted %d financial fields (banking=%s)",
        len(result) - 1, is_banking,
    )
    return result


# ─── Table-based label → field key mapping ───────────────────────────────

_TABLE_LABEL_MAP: list[tuple[list[str], str, bool]] = [
    # (label_keywords, field_key, is_inr)
    # Income
    (["revenue from operations"], "revenue_from_operations", True),
    (["other income"], "other_income", True),
    # Expenses
    (["cost of materials"], "cost_of_materials", True),
    (["employee benefit", "employee cost"], "employee_benefit_expense", True),
    (["finance cost"], "finance_costs", True),
    (["depreciation"], "depreciation", True),
    (["total expense"], "total_expenses", True),
    # Profitability
    (["profit before exceptional", "profit before extra"], "profit_before_exceptional", True),
    (["exceptional item"], "exceptional_items", True),
    (["profit before tax", "pbt"], "profit_before_tax", True),
    (["tax expense", "total tax"], "tax_expense", True),
    (["profit after tax", "pat", "net profit", "profit for the period"], "profit_after_tax", True),
    # Banking
    (["interest earned"], "interest_earned", True),
    (["interest expended"], "interest_expended", True),
    (["operating profit"], "operating_profit", True),
    (["provision"], "provisions", True),
    # EPS
    (["basic eps", "basic earning"], "basic_eps", False),
    (["diluted eps", "diluted earning"], "diluted_eps", False),
    (["face value"], "face_value", False),
]


def _extract_financials_from_tables(
    soup: "BeautifulSoup",
    result: dict[str, Any],
) -> None:
    """Fallback: extract financial data from HTML tables for legacy filings."""
    for table in soup.find_all("table"):
        for row in table.find_all("tr"):
            cells = row.find_all(["td", "th"])
            if len(cells) < 2:
                continue

            label = cells[0].get_text(strip=True).lower()
            # Take the last numeric cell (usually the current period value)
            value_text = ""
            for cell in reversed(cells[1:]):
                txt = cell.get_text(strip=True).replace(",", "").replace(" ", "")
                if txt.startswith("(") and txt.endswith(")"):
                    txt = "-" + txt[1:-1]
                try:
                    float(txt)
                    value_text = txt
                    break
                except (ValueError, TypeError):
                    continue

            if not value_text:
                continue

            try:
                val = float(value_text)
            except (ValueError, TypeError):
                continue

            for keywords, field_key, is_inr in _TABLE_LABEL_MAP:
                if field_key in result:
                    continue
                if any(kw in label for kw in keywords):
                    if is_inr:
                        # Assume table values are in the filing's rounding unit
                        # Most HTML filings display in Lakhs or Crores
                        # We'll store as-is and let the caller handle conversion
                        result[field_key] = val
                    else:
                        result[field_key] = val
                    break