"""
financial_results/schemas.py
----------------------------
Pydantic v2 schemas for parsed filing metadata.

Contains:
    • FinancialResultMetadata — normalised metadata extracted from XML/XBRL/HTML filings
"""
from __future__ import annotations

from typing import Optional
from pydantic import BaseModel, Field

from .utils import derive_quarter
from .financial_data import FinancialData


class FinancialResultMetadata(BaseModel):
    """Normalised metadata extracted from a financial result filing.

    Fields are populated from a combination of RSS feed data and document
    parsing (XML/XBRL or HTML).  All fields are optional except company_name
    and source_url — the pipeline gracefully handles partial metadata.

    Provenance fields (*_source) record where each value came from
    ("rss", "xbrl", "html_table", "html_title") to aid debugging.
    """
    company_name:            str
    company_name_source:     str            = Field(default="rss",  description="Source of company_name")
    scrip_code:              Optional[str]  = Field(None, description="BSE scrip code")
    scrip_code_source:       Optional[str]  = Field(None, description="Source of scrip_code")
    symbol:                  Optional[str]  = Field(None, description="NSE/BSE trading symbol")
    announcement_date:       Optional[str]  = Field(None, description="ISO date of the announcement")
    period_start:            Optional[str]  = Field(None, description="Reporting period start (ISO date)")
    period_end:              Optional[str]  = Field(None, description="Reporting period end (ISO date)")
    financial_year:          Optional[str]  = Field(None, description="e.g. 2024-25")
    quarter:                 Optional[str]  = Field(None, description="Q1 | Q2 | Q3 | Q4")
    standalone_consolidated: Optional[str]  = Field(None, description="Standalone | Consolidated")
    filing_type:             Optional[str]  = Field(None, description="e.g. Audited, Unaudited, Provisional")
    auditor:                 Optional[str]  = Field(None, description="Name of the auditor firm")
    exchange:                str            = Field(default="NSE", description="NSE | BSE")
    source_url:              str            = Field(description="URL of the original filing document")
    document_type:           Optional[str]  = Field(None, description="xml | xbrl | html")
    uid:                     str            = Field(description="SHA-256 of the source URL")
    financials:              Optional[FinancialData] = Field(None, description="Raw financial line items + computed metrics")

    def with_derived_quarter(self) -> "FinancialResultMetadata":
        """Return a copy with quarter derived from period_end if missing."""
        q = self.quarter or derive_quarter(self.period_end)
        if not q:
            return self

        import re
        q_match = re.search(r"\b(Q[1-4]|H[1-2]|FY)\b", q, re.IGNORECASE)
        if q_match:
            base_q = q_match.group(1).upper()
            if self.financial_year:
                if base_q == "FY":
                    q = f"FY {self.financial_year}"
                else:
                    q = f"{base_q} FY {self.financial_year}"
            else:
                q = base_q

        if q != self.quarter:
            return self.model_copy(update={"quarter": q})
        return self
