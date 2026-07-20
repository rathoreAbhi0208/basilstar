"""
financial_results/financial_data.py
------------------------------------
Pydantic model and calculation logic for financial data extracted
directly from XBRL/XML and HTML filings.

Contains:
    • FinancialData        — all raw + calculated financial line items
    • compute_derived()    — calculates EBITDA, margins, NIM, etc.
    • to_crores()          — converts raw INR values to ₹ Crores
    • format_financials_for_prompt() — human-readable block for Gemini

Supports two XBRL taxonomy variants:
    • IFIndAs   — standard non-banking companies
    • IFBanking — banks, NBFCs, financial institutions
"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


# ─── Unit conversion ────────────────────────────────────────────────────────

def to_crores(value_inr: float | int | str | None) -> float | None:
    """Convert a raw INR value (as reported in XBRL) to ₹ Crores.

    XBRL always stores values in the base currency unit (INR).
    1 Crore = 10,000,000 INR = 10^7.
    """
    if value_inr is None:
        return None
    try:
        v = float(value_inr)
    except (ValueError, TypeError):
        return None
    return round(v / 1e7, 2)


# ─── Financial Data Model ───────────────────────────────────────────────────

class FinancialData(BaseModel):
    """Financial line items extracted from XBRL/HTML filings.

    All monetary values are in ₹ Crores.
    Ratios and percentages are stored as-is (e.g. 0.12 for 12%).
    EPS is in ₹ per share.

    The `is_banking` flag determines which schema was used for
    extraction (IFBanking vs IFIndAs) — this affects which fields
    are populated and which calculated metrics are relevant.
    """

    # ── Filing schema type ───────────────────────────────────────────────
    is_banking:                 bool           = False
    level_of_rounding:          str            = ""

    # ── Common P&L fields (both banking and non-banking) ─────────────────
    other_income:               Optional[float] = Field(None, description="₹ Cr")
    profit_before_tax:          Optional[float] = Field(None, description="₹ Cr")
    tax_expense:                Optional[float] = Field(None, description="₹ Cr")
    profit_after_tax:           Optional[float] = Field(None, description="₹ Cr")
    exceptional_items:          Optional[float] = Field(None, description="₹ Cr")

    # ── Equity / EPS ─────────────────────────────────────────────────────
    paid_up_equity_capital:     Optional[float] = Field(None, description="₹ Cr")
    face_value:                 Optional[float] = Field(None, description="₹ per share")
    basic_eps:                  Optional[float] = Field(None, description="₹ per share")
    diluted_eps:                Optional[float] = Field(None, description="₹ per share")

    # ── Non-banking (IFIndAs) line items ─────────────────────────────────
    revenue_from_operations:    Optional[float] = Field(None, description="₹ Cr")
    cost_of_materials:          Optional[float] = Field(None, description="₹ Cr")
    purchase_of_stock_in_trade: Optional[float] = Field(None, description="₹ Cr")
    changes_in_inventories:     Optional[float] = Field(None, description="₹ Cr")
    employee_benefit_expense:   Optional[float] = Field(None, description="₹ Cr")
    finance_costs:              Optional[float] = Field(None, description="₹ Cr")
    depreciation:               Optional[float] = Field(None, description="₹ Cr")
    other_expenses:             Optional[float] = Field(None, description="₹ Cr")
    total_expenses:             Optional[float] = Field(None, description="₹ Cr")
    profit_before_exceptional:  Optional[float] = Field(None, description="₹ Cr")
    other_comprehensive_income: Optional[float] = Field(None, description="₹ Cr")
    total_comprehensive_income: Optional[float] = Field(None, description="₹ Cr")

    # ── Banking (IFBanking) line items ───────────────────────────────────
    interest_earned:            Optional[float] = Field(None, description="₹ Cr")
    interest_expended:          Optional[float] = Field(None, description="₹ Cr")
    net_interest_income:        Optional[float] = Field(None, description="₹ Cr — calculated")
    operating_expenses_bank:    Optional[float] = Field(None, description="₹ Cr")
    employees_cost:             Optional[float] = Field(None, description="₹ Cr")
    operating_profit:           Optional[float] = Field(None, description="₹ Cr")
    provisions:                 Optional[float] = Field(None, description="₹ Cr")
    gross_npa_amount:           Optional[float] = Field(None, description="₹ Cr")
    net_npa_amount:             Optional[float] = Field(None, description="₹ Cr")
    gross_npa_pct:              Optional[float] = Field(None, description="Ratio (e.g. 0.023 = 2.3%)")
    net_npa_pct:                Optional[float] = Field(None, description="Ratio (e.g. 0.0016 = 0.16%)")
    return_on_assets:           Optional[float] = Field(None, description="Ratio (e.g. 0.0189)")
    cet1_ratio:                 Optional[float] = Field(None, description="Ratio (e.g. 0.265)")
    minority_interest:          Optional[float] = Field(None, description="₹ Cr")
    attributable_profit:        Optional[float] = Field(None, description="₹ Cr — after minority")

    # ── Calculated metrics ───────────────────────────────────────────────
    # Non-banking
    ebitda:                     Optional[float] = Field(None, description="₹ Cr — profit_before_exceptional + finance_costs + depreciation")
    ebitda_margin:              Optional[float] = Field(None, description="% — EBITDA / revenue_from_operations × 100 (non-banking)")
    # Shared
    pat_margin:                 Optional[float] = Field(None, description="% — PAT / total_revenue × 100")
    operating_profit_margin:    Optional[float] = Field(None, description="% — non-banking: EBIT/revenue_from_ops; banking: operating_profit/total_income")
    effective_tax_rate:         Optional[float] = Field(None, description="% — tax / PBT × 100")

    @property
    def revenue(self) -> float | None:
        """Total revenue = Revenue from Operations + Other Income.

        For non-banking companies:
            revenue_from_operations + other_income  (= Total Income per P&L)
        For banking companies:
            interest_earned + other_income           (= Total Income per P&L)

        Falls back to None if both components are unavailable.
        """
        if self.is_banking:
            base = self.interest_earned
        else:
            base = self.revenue_from_operations

        if base is not None:
            return round(base + (self.other_income or 0), 2)

        return None  # no data available

    @property
    def profit_net(self) -> float | None:
        return self.profit_after_tax

    def compute_derived(self) -> "FinancialData":
        """Compute derived/calculated metrics from raw line items.

        Returns a new FinancialData instance with calculated fields filled in.

        EBITDA (non-banking):
            = profit_before_exceptional + finance_costs + depreciation
            Using profit_before_exceptional (not PBT) ensures one-off exceptional
            write-offs / gains do not distort the operating profitability metric.

        Operating Profit Margin:
            Non-banking : (profit_before_exceptional + finance_costs + depreciation)
                          / revenue_from_operations × 100  (= EBITDA margin)
            Banking     : operating_profit / (interest_earned + other_income) × 100
        """
        updates: dict[str, Any] = {}

        if self.is_banking:
            # ── Banking: NII ─────────────────────────────────────────
            if self.interest_earned is not None and self.interest_expended is not None:
                updates["net_interest_income"] = round(
                    self.interest_earned - self.interest_expended, 2
                )

            # ── Banking: Operating profit margin ─────────────────────
            # Denominator = total income (interest earned + other income)
            op = self.operating_profit
            total_income = self.revenue  # property: interest_earned + other_income
            if op is not None and total_income and total_income > 0:
                updates["operating_profit_margin"] = round(
                    (op / total_income) * 100, 2
                )

        else:
            # ── Non-banking: EBITDA ──────────────────────────────────
            # Base: profit_before_exceptional (excludes one-off items)
            # Falls back to profit_before_tax if pre-exceptional figure absent.
            pbe = self.profit_before_exceptional
            pbt = self.profit_before_tax
            base = pbe if pbe is not None else pbt  # prefer pre-exceptional
            fc  = self.finance_costs or 0
            dep = self.depreciation or 0

            if base is not None:
                updates["ebitda"] = round(base + fc + dep, 2)

            # ── EBITDA margin ────────────────────────────────────────
            # Denominator: revenue_from_operations only (standard Indian P&L)
            rev_ops = self.revenue_from_operations
            if updates.get("ebitda") is not None and rev_ops and rev_ops > 0:
                updates["ebitda_margin"] = round(
                    (updates["ebitda"] / rev_ops) * 100, 2
                )
                # For non-banking, operating_profit_margin == ebitda_margin
                updates["operating_profit_margin"] = updates["ebitda_margin"]

        # ── PAT margin (both banking and non-banking) ────────────────
        # Denominator: total revenue (ops/interest + other income)
        pat = self.profit_after_tax
        combined_rev = self.revenue  # uses the property above
        if pat is not None and combined_rev and combined_rev > 0:
            updates["pat_margin"] = round((pat / combined_rev) * 100, 2)

        # ── Effective tax rate ───────────────────────────────────────
        pbt = self.profit_before_tax
        tax = self.tax_expense
        if pbt and pbt > 0 and tax is not None:
            updates["effective_tax_rate"] = round((tax / pbt) * 100, 2)

        if updates:
            return self.model_copy(update=updates)
        return self


# ─── Prompt formatter ────────────────────────────────────────────────────────

def format_financials_for_prompt(data: FinancialData) -> str:
    """Format extracted financial data as a human-readable text block
    for inclusion in the Gemini prompt.

    Only includes fields that have actual values (not None).
    """
    lines: list[str] = []

    def _add(label: str, value: float | None, unit: str = "₹ Cr") -> None:
        if value is not None:
            lines.append(f"  {label}: {value:,.2f} {unit}")

    def _add_pct(label: str, value: float | None) -> None:
        if value is not None:
            # Check if it's a ratio (< 1) that needs conversion to %
            if abs(value) < 1:
                lines.append(f"  {label}: {value * 100:.2f}%")
            else:
                lines.append(f"  {label}: {value:.2f}%")

    if data.is_banking:
        lines.append("INCOME:")
        _add("Interest Earned", data.interest_earned)
        _add("Other Income", data.other_income)

        lines.append("\nEXPENDITURE:")
        _add("Interest Expended", data.interest_expended)
        _add("Employees Cost", data.employees_cost)
        _add("Operating Expenses", data.operating_expenses_bank)

        lines.append("\nPROFITABILITY:")
        _add("Net Interest Income (NII)", data.net_interest_income)
        _add("Operating Profit (Pre-Provision)", data.operating_profit)
        _add_pct("Operating Profit Margin", data.operating_profit_margin)
        _add("Provisions & Contingencies", data.provisions)
        _add("Exceptional Items", data.exceptional_items)
        _add("Profit Before Tax", data.profit_before_tax)
        _add("Tax Expense", data.tax_expense)
        _add("Profit After Tax (PAT)", data.profit_after_tax)
        _add("Minority Interest", data.minority_interest)
        _add("Attributable Profit", data.attributable_profit)

        lines.append("\nASSET QUALITY & RATIOS:")
        _add("Gross NPA", data.gross_npa_amount)
        _add("Net NPA", data.net_npa_amount)
        _add_pct("Gross NPA Ratio", data.gross_npa_pct)
        _add_pct("Net NPA Ratio", data.net_npa_pct)
        _add_pct("Return on Assets", data.return_on_assets)
        _add_pct("CET1 Ratio", data.cet1_ratio)
    else:
        lines.append("INCOME:")
        _add("Revenue from Operations", data.revenue_from_operations)
        _add("Other Income", data.other_income)

        lines.append("\nEXPENDITURE:")
        _add("Cost of Materials Consumed", data.cost_of_materials)
        _add("Purchase of Stock-in-Trade", data.purchase_of_stock_in_trade)
        _add("Changes in Inventories", data.changes_in_inventories)
        _add("Employee Benefit Expense", data.employee_benefit_expense)
        _add("Finance Costs", data.finance_costs)
        _add("Depreciation & Amortisation", data.depreciation)
        _add("Other Expenses", data.other_expenses)
        _add("Total Expenses", data.total_expenses)

        lines.append("\nPROFITABILITY:")
        _add("Profit Before Exceptional Items & Tax", data.profit_before_exceptional)
        _add("Exceptional Items", data.exceptional_items)
        _add("Profit Before Tax", data.profit_before_tax)
        _add("Tax Expense", data.tax_expense)
        _add("Profit After Tax (PAT)", data.profit_after_tax)
        _add("Other Comprehensive Income", data.other_comprehensive_income)
        _add("Total Comprehensive Income", data.total_comprehensive_income)

        lines.append("\nCALCULATED METRICS:")
        _add("EBITDA", data.ebitda)
        _add_pct("EBITDA Margin (on Revenue from Ops)", data.ebitda_margin)
        _add_pct("Operating Profit Margin", data.operating_profit_margin)

    # ── Common bottom section ────────────────────────────────────────────
    lines.append("\nPER SHARE DATA:")
    _add("Basic EPS", data.basic_eps, "₹")
    _add("Diluted EPS", data.diluted_eps, "₹")
    _add("Face Value", data.face_value, "₹")
    _add("Paid-Up Equity Capital", data.paid_up_equity_capital)

    lines.append("\nMARGINS:")
    _add_pct("PAT Margin", data.pat_margin)
    _add_pct("Effective Tax Rate", data.effective_tax_rate)

    # Filter out empty section headers
    result_lines: list[str] = []
    for i, line in enumerate(lines):
        if line.endswith(":") and not line.startswith("  "):
            # Section header — only include if next line has data
            if i + 1 < len(lines) and lines[i + 1].startswith("  "):
                result_lines.append(line)
        else:
            result_lines.append(line)

    return "\n".join(result_lines)
