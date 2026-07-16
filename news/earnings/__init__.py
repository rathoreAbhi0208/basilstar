"""
news/earnings — Earnings Intelligence Pipeline sub-package.

Dedicated pipeline for financial results / earnings-related events.
Activated automatically when Stage 1 classifies a news item as EARNINGS_RESULT.

Exports
~~~~~~~
    generate_earnings_report Async two-stage earnings analysis engine
"""
from .generator import generate_earnings_report

__all__ = [
    "generate_earnings_report",
]
