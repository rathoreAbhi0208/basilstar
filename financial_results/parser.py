"""
financial_results/parser.py
---------------------------
Filing document parser — routes to the appropriate extractor based on
document type, reconciles document metadata with RSS-level data, and
returns a fully-merged FinancialResultMetadata.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from .schemas      import FinancialResultMetadata
from .xbrl_parser  import extract_xbrl_metadata, extract_xbrl_financials
from .html_parser  import extract_html_metadata, extract_html_financials
from .financial_data import FinancialData
from .utils        import make_uid

logger = logging.getLogger(__name__)


# ─── Generic placeholders that must never become company names ───────────────

# These strings appear as HTML <title> values in exchange filings but carry
# no actual company identity.  Normalise before comparing.
_PLACEHOLDERS: frozenset[str] = frozenset({
    "company results",
    "financial results",
    "quarterly results",
    "annual results",
    "announcement",
    "integrated filing",
    "untitled",
    "result",
    "annual report",
    "bse filing",
    "nse filing",
})


# ─── Name normalisation ──────────────────────────────────────────────────────

def _normalise_name(name: str) -> str:
    """Normalise a company name for fuzzy comparison.

    Steps:
        1. Lowercase.
        2. Expand common abbreviations (ltd → limited, pvt → private, & → and).
        3. Remove punctuation (commas, dots, hyphens when surrounded by space).
        4. Collapse whitespace.

    This is intentionally lightweight — used only for comparison, not storage.
    The canonical stored name always comes from the higher-quality source.
    """
    name = name.lower().strip()
    # Abbreviation normalisation
    name = re.sub(r"\bltd\b",  "limited",  name)
    name = re.sub(r"\bpvt\b",  "private",  name)
    name = re.sub(r"\bcorp\b", "corporation", name)
    name = re.sub(r"&",        "and",      name)
    # Strip remaining punctuation
    name = re.sub(r"[.,\-']",  " ",        name)
    # Collapse whitespace
    name = re.sub(r"\s+",      " ",        name).strip()
    return name


def _is_placeholder(name: str) -> bool:
    """Return True if the name is a known generic placeholder."""
    return _normalise_name(name) in _PLACEHOLDERS


# ─── Company name reconciliation ─────────────────────────────────────────────

def _reconcile_company_name(
    rss_name: str,
    doc_name: str | None,
    doc_source: str,
) -> tuple[str, str]:
    """Choose the best company name between RSS and document sources.

    Rules (in order):
        1. If doc_name is absent or a placeholder → keep RSS name.
        2. If normalised names match → use doc_name (more formal).
        3. If they differ → keep RSS name and log a warning.

    Returns:
        (chosen_name, source_label)
    """
    if not doc_name or _is_placeholder(doc_name):
        return rss_name, "rss"

    if _normalise_name(rss_name) == _normalise_name(doc_name):
        # Names match after normalisation — use the document's formal variant.
        return doc_name, doc_source

    # Genuine mismatch — keep the RSS name (higher trust) and warn.
    logger.warning(
        "[Parser] Company name mismatch — keeping RSS value. "
        "rss=%r  doc=%r  doc_source=%s",
        rss_name, doc_name, doc_source,
    )
    return rss_name, "rss"


# ─── Scrip code reconciliation ───────────────────────────────────────────────

def _reconcile_scrip_code(
    rss_scrip: str | None,
    doc_scrip: str | None,
    doc_source: str,
) -> tuple[str | None, str | None]:
    """Reconcile scrip codes from RSS and document.

    Rules:
        1. If only one source has a code → use it.
        2. If both match → use doc value (same content, doc is fine).
        3. If both present but differ → keep RSS value, log warning.

    Returns:
        (chosen_scrip, source_label)  — both None when no code is available.
    """
    if not rss_scrip and not doc_scrip:
        return None, None
    if not doc_scrip:
        return rss_scrip, "rss"
    if not rss_scrip:
        return doc_scrip, doc_source

    if rss_scrip == doc_scrip:
        return doc_scrip, doc_source

    logger.warning(
        "[Parser] Scrip code mismatch — keeping RSS value. "
        "rss=%r  doc=%r  doc_source=%s",
        rss_scrip, doc_scrip, doc_source,
    )
    return rss_scrip, "rss"


# ─── Public parse function ───────────────────────────────────────────────────

def parse_filing(
    content:  bytes | None,
    doc_type: str | None,
    rss_meta: dict[str, Any],
) -> FinancialResultMetadata:
    """Parse a filing document and merge with RSS-level metadata.

    Args:
        content:  Raw bytes of the filing document. None if download failed.
        doc_type: 'xml' or 'html'. None if download failed.
        rss_meta: Dict with keys from the RSS feed:
                  company_name, scrip_code, filing_url, published_at,
                  exchange, uid.

    Returns:
        FinancialResultMetadata with all available fields populated.
        Quarter is auto-derived from period_end if missing.
    """
    # ── 1. Extract document-level metadata ───────────────────────────────
    doc_meta: dict[str, Any] = {}
    doc_financials: dict[str, Any] = {}
    doc_source = doc_type or "unknown"   # "xml" | "html"

    if content and doc_type:
        try:
            if doc_type == "xml":
                doc_meta = extract_xbrl_metadata(content)
                doc_financials = extract_xbrl_financials(content)
            elif doc_type == "html":
                doc_meta = extract_html_metadata(content)
                doc_financials = extract_html_financials(content)
            else:
                logger.warning("[Parser] Unknown doc_type %r — skipping document parse", doc_type)
        except Exception as exc:
            logger.exception("[Parser] Document parse failed: %s", exc)

    # ── 2. Reconcile identity fields (RSS primary) ────────────────────────

    rss_company   = rss_meta.get("company_name", "Unknown")
    doc_company   = doc_meta.get("company_name")          # may be a placeholder
    rss_scrip     = rss_meta.get("scrip_code")            # parsed from BSE title
    doc_scrip     = doc_meta.get("scrip_code") or doc_meta.get("symbol")

    company_name, company_source = _reconcile_company_name(
        rss_company, doc_company, doc_source,
    )
    scrip_code, scrip_source = _reconcile_scrip_code(
        rss_scrip, doc_scrip, doc_source,
    )

    # ── 3. Build merged metadata ──────────────────────────────────────────
    # Period / filing-type fields come exclusively from the document — the RSS
    # feed does not carry them.  symbol is set to scrip_code when available
    # (consistent with existing behaviour).

    financials_model = None
    if doc_financials:
        # Create FinancialData and compute derived metrics (EBITDA, margins)
        financials_model = FinancialData(**doc_financials).compute_derived()

    metadata = FinancialResultMetadata(
        # Identity (reconciled above)
        company_name         = company_name,
        company_name_source  = company_source,
        scrip_code           = scrip_code,
        scrip_code_source    = scrip_source,
        # Symbol: prefer explicit doc symbol, fall back to scrip_code
        symbol               = doc_meta.get("symbol") or scrip_code,
        # Always from RSS
        source_url           = rss_meta.get("filing_url", ""),
        exchange             = rss_meta.get("exchange", "NSE"),
        uid                  = rss_meta.get("uid") or make_uid(rss_meta.get("filing_url", "")),
        announcement_date    = rss_meta.get("published_at"),
        document_type        = doc_type,
        # From document only
        period_start             = doc_meta.get("period_start"),
        period_end               = doc_meta.get("period_end"),
        financial_year           = doc_meta.get("financial_year"),
        quarter                  = doc_meta.get("quarter"),
        standalone_consolidated  = doc_meta.get("standalone_consolidated"),
        filing_type              = doc_meta.get("filing_type"),
        auditor                  = doc_meta.get("auditor"),
        financials               = financials_model,
    )

    metadata = metadata.with_derived_quarter()

    logger.info(
        "[Parser] Parsed: %s (source=%s) | exchange=%s | quarter=%s | period_end=%s",
        metadata.company_name,
        metadata.company_name_source,
        metadata.exchange,
        metadata.quarter or "?",
        metadata.period_end or "?",
    )
    return metadata
