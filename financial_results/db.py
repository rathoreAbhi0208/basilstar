"""
financial_results/db.py
-----------------------
Async SQLite persistence layer for the Financial Results package.

Features:
    • WAL mode + NORMAL sync for optimal read/write concurrency.
    • 7-day TTL with automatic pruning.
    • INSERT OR IGNORE for idempotent inserts (dedup by filing UID).
    • Full-text search across company_name, symbol, executive_summary.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional

import aiosqlite

from .models import FinancialResultRecord, Forecast

logger = logging.getLogger(__name__)

_TTL_HOURS = 168     # 7 days
_TTL_SECS  = _TTL_HOURS * 3600


# ─── DDL ────────────────────────────────────────────────────────────────────

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS financial_results (
    id                      TEXT PRIMARY KEY,

    -- Filing metadata
    company_name            TEXT    NOT NULL,
    symbol                  TEXT    NOT NULL DEFAULT '',
    exchange                TEXT    NOT NULL DEFAULT '',
    quarter                 TEXT    NOT NULL DEFAULT '',
    result_date             TEXT    NOT NULL DEFAULT '',
    announcement_date       TEXT,
    period_start            TEXT,
    period_end              TEXT,
    financial_year          TEXT,
    standalone_consolidated TEXT,
    filing_type             TEXT,
    document_type           TEXT,
    source_url              TEXT    NOT NULL DEFAULT '',

    -- Extracted Financials (Core — revenue = ops + other income)
    revenue                 REAL,
    profit_before_tax       REAL,
    profit_net              REAL,
    basic_eps               REAL,

    -- Extracted Financials (Non-banking)
    depreciation            REAL,

    -- Extracted Financials (Banking)
    operating_profit        REAL,

    -- Derived metrics
    ebitda                  REAL,
    ebitda_margin           REAL,
    pat_margin              REAL,
    operating_profit_margin REAL,

    -- AI analysis
    headline                TEXT    NOT NULL DEFAULT '',
    executive_summary       TEXT    NOT NULL DEFAULT '',
    guidance                TEXT    NOT NULL DEFAULT '',
    sentiment               TEXT    NOT NULL DEFAULT 'NEUTRAL',
    impact                  TEXT    NOT NULL DEFAULT 'MEDIUM',
    revenue_change_yoy      REAL,
    profit_change_yoy       REAL,
    eps_change_yoy          REAL,

    forecast_short_term     TEXT,    -- JSON
    forecast_medium_term    TEXT,    -- JSON

    source_urls             TEXT    NOT NULL DEFAULT '[]',   -- JSON array

    -- Backend-derived
    recommendation          TEXT    NOT NULL DEFAULT 'HOLD',

    -- Operational
    gemini_model            TEXT    NOT NULL DEFAULT '',
    created_at_unix         REAL    NOT NULL,
    updated_at_unix         REAL    NOT NULL,
    expires_at_unix         REAL    NOT NULL
);
"""

_INDEXES: list[str] = [
    "CREATE INDEX IF NOT EXISTS idx_fr_symbol     ON financial_results (symbol);",
    "CREATE INDEX IF NOT EXISTS idx_fr_exchange    ON financial_results (exchange);",
    "CREATE INDEX IF NOT EXISTS idx_fr_quarter     ON financial_results (quarter);",
    "CREATE INDEX IF NOT EXISTS idx_fr_sentiment   ON financial_results (sentiment);",
    "CREATE INDEX IF NOT EXISTS idx_fr_impact      ON financial_results (impact);",
    "CREATE INDEX IF NOT EXISTS idx_fr_rec         ON financial_results (recommendation);",
    "CREATE INDEX IF NOT EXISTS idx_fr_expires     ON financial_results (expires_at_unix);",
    "CREATE INDEX IF NOT EXISTS idx_fr_created     ON financial_results (created_at_unix);",
    "CREATE INDEX IF NOT EXISTS idx_fr_company     ON financial_results (company_name);",
    # Business-logic dedup: one result per company per period per filing type.
    # company_name used because symbol can be empty for BSE-only filings.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_fr_biz_dedup "
    "ON financial_results (company_name, period_end, standalone_consolidated) "
    "WHERE period_end IS NOT NULL;",
]


# ─── Insert SQL ─────────────────────────────────────────────────────────────

_INSERT_SQL = """
INSERT OR IGNORE INTO financial_results (
    id,
    company_name, symbol, exchange, quarter, result_date,
    announcement_date, period_start, period_end, financial_year,
    standalone_consolidated, filing_type, document_type, source_url,

    revenue, profit_before_tax, profit_net, basic_eps,
    depreciation, operating_profit,
    ebitda, ebitda_margin, pat_margin, operating_profit_margin,

    headline, executive_summary, guidance, sentiment, impact,
    revenue_change_yoy, profit_change_yoy, eps_change_yoy,

    forecast_short_term, forecast_medium_term, source_urls, recommendation, gemini_model,
    created_at_unix, updated_at_unix, expires_at_unix
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?, ?,?,?,?,?,?,?,?,?,?, ?,?,?,?,?,?,?,?, ?,?,?,?,?, ?,?,?)
"""


class ResultsDB:
    """Async SQLite database for financial results."""

    def __init__(self, db_path: str = "results.db") -> None:
        self._db_path = db_path

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def init(self) -> None:
        """Create tables, indexes, and run schema migrations."""
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("PRAGMA journal_mode=WAL;")
            await db.execute("PRAGMA synchronous=NORMAL;")
            await db.execute("PRAGMA cache_size=-32000;")
            await db.execute(_CREATE_TABLE_SQL)
            for idx in _INDEXES:
                await db.execute(idx)
            # ── Schema migrations — add new columns (idempotent) ─────────────
            await self._migrate_add_column(db, "headline", "TEXT NOT NULL DEFAULT ''")
            await self._migrate_add_column(db, "profit_before_tax", "REAL")
            await self._migrate_add_column(db, "basic_eps", "REAL")
            await self._migrate_add_column(db, "depreciation", "REAL")
            await self._migrate_add_column(db, "operating_profit", "REAL")
            await self._migrate_add_column(db, "ebitda", "REAL")
            await self._migrate_add_column(db, "ebitda_margin", "REAL")
            await self._migrate_add_column(db, "pat_margin", "REAL")
            await self._migrate_add_column(db, "operating_profit_margin", "REAL")
            # ── Schema migrations — drop deprecated columns (idempotent) ──────
            for _col in (
                "other_income", "tax_expense", "diluted_eps",
                "total_expenses", "finance_costs",
                "interest_earned", "interest_expended", "net_interest_income",
                "provisions", "gross_npa_pct", "net_npa_pct", "effective_tax_rate",
            ):
                await self._migrate_drop_column(db, _col)
            await db.commit()
        logger.info("[Results/DB] Initialised at %s", self._db_path)

    @staticmethod
    async def _migrate_add_column(db, column: str, col_type: str) -> None:
        """Add a column if it doesn't already exist (idempotent)."""
        try:
            await db.execute(
                f"ALTER TABLE financial_results ADD COLUMN {column} {col_type}"
            )
            logger.info("[Results/DB] Migration: added column '%s'", column)
        except Exception:
            # Column already exists — nothing to do.
            pass

    @staticmethod
    async def _migrate_drop_column(db, column: str) -> None:
        """Drop a column if it exists (idempotent, requires SQLite ≥ 3.35)."""
        try:
            # Check if the column exists first
            cur = await db.execute(
                "SELECT COUNT(*) FROM pragma_table_info('financial_results') WHERE name = ?",
                (column,),
            )
            count = (await cur.fetchone())[0]
            if count:
                await db.execute(
                    f"ALTER TABLE financial_results DROP COLUMN {column}"
                )
                logger.info("[Results/DB] Migration: dropped column '%s'", column)
        except Exception as exc:
            logger.debug("[Results/DB] Drop column '%s' skipped: %s", column, exc)

    # ── Writes ───────────────────────────────────────────────────────────

    async def insert_result(self, record: FinancialResultRecord) -> bool:
        """Insert a single financial result; duplicates are silently ignored.

        Returns True if inserted, False if duplicate.
        """
        now_unix = time.time()
        expires  = now_unix + _TTL_SECS

        row = (
            record.id,
            record.company_name,
            record.symbol,
            record.exchange,
            record.quarter,
            record.result_date,
            record.announcement_date,
            record.period_start,
            record.period_end,
            record.financial_year,
            record.standalone_consolidated,
            record.filing_type,
            record.document_type,
            record.source_url,

            # Financials
            record.revenue,
            record.profit_before_tax,
            record.profit_net,
            record.basic_eps,
            record.depreciation,
            record.operating_profit,
            record.ebitda,
            record.ebitda_margin,
            record.pat_margin,
            record.operating_profit_margin,

            # AI Analysis
            record.headline,
            record.executive_summary,
            record.guidance,
            record.sentiment,
            record.impact,
            record.revenue_change_yoy,
            record.profit_change_yoy,
            record.eps_change_yoy,

            json.dumps(record.forecast_short_term.model_dump() if record.forecast_short_term else None),
            json.dumps(record.forecast_medium_term.model_dump() if record.forecast_medium_term else None),
            json.dumps(record.source_urls),
            record.recommendation,
            record.gemini_model,
            record.created_at_unix or now_unix,
            record.updated_at_unix or now_unix,
            expires,
        )

        async with aiosqlite.connect(self._db_path) as db:
            cursor = await db.execute(_INSERT_SQL, row)
            await db.commit()
            inserted = cursor.rowcount > 0

        if inserted:
            logger.info("[Results/DB] Inserted: %s (%s)", record.company_name, record.id[:12])
        else:
            logger.debug("[Results/DB] Duplicate skipped: %s", record.company_name)

        return inserted

    async def prune_expired(self) -> int:
        """Delete results older than 7 days."""
        async with aiosqlite.connect(self._db_path) as db:
            cursor = await db.execute(
                "DELETE FROM financial_results WHERE expires_at_unix <= ?",
                (time.time(),),
            )
            await db.commit()
        pruned = cursor.rowcount
        if pruned > 0:
            logger.info("[Results/DB] Pruned %d expired results", pruned)
        return pruned

    # ── Reads ────────────────────────────────────────────────────────────

    async def get_existing_uids(self) -> set[str]:
        """Return all UIDs from financial_results for URL-level dedup.

        Returns UIDs created within the last 14 days to avoid unbounded growth.
        """
        cutoff = time.time() - (14 * 24 * 3600)
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "SELECT id FROM financial_results WHERE created_at_unix > ?",
                (cutoff,),
            )
            rows = await cur.fetchall()
        return {r[0] for r in rows}

    async def get_existing_business_keys(self) -> set[tuple[str, str, str]]:
        """Return (company_name, period_end, standalone_consolidated) tuples
        already stored in the DB (created within 14 days).

        Used by the scheduler to skip items whose business identity matches
        an existing record — even if the filing URL (and thus UID) differs.
        This prevents duplicate analysis when NSE/BSE re-publish the same
        result announcement with a slightly different URL.
        """
        cutoff = time.time() - (14 * 24 * 3600)
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                """
                SELECT company_name, period_end, standalone_consolidated
                FROM   financial_results
                WHERE  created_at_unix > ?
                  AND  period_end IS NOT NULL
                """,
                (cutoff,),
            )
            rows = await cur.fetchall()
        return {
            (r[0] or "", r[1] or "", r[2] or "")
            for r in rows
        }

    async def count_live(self) -> int:
        """Count non-expired results."""
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "SELECT COUNT(*) FROM financial_results WHERE expires_at_unix > ?",
                (time.time(),),
            )
            return (await cur.fetchone())[0]

    async def get_by_symbol(self, symbol: str) -> list[FinancialResultRecord]:
        """Get all results for a given trading symbol."""
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """SELECT * FROM financial_results
                   WHERE (UPPER(symbol) = UPPER(?) OR UPPER(company_name) LIKE UPPER(?))
                   AND expires_at_unix > ?
                   ORDER BY created_at_unix DESC""",
                (symbol, f"%{symbol}%", time.time()),
            )
            rows = await cur.fetchall()
        return [_row_to_record(r) for r in rows]

    async def list_results(
        self,
        *,
        exchange:   Optional[str] = None,
        quarter:    Optional[str] = None,
        sentiment:  Optional[str] = None,
        impact:     Optional[str] = None,
        recommendation: Optional[str] = None,
        limit:      int = 20,
        offset:     int = 0,
    ) -> tuple[list[FinancialResultRecord], int]:
        """Paginated listing with filters."""
        now = time.time()
        where:  list[str] = ["expires_at_unix > ?"]
        params: list[Any] = [now]

        if exchange:
            where.append("UPPER(exchange) = UPPER(?)")
            params.append(exchange)
        if quarter:
            where.append("UPPER(quarter) = UPPER(?)")
            params.append(quarter)
        if sentiment:
            where.append("UPPER(sentiment) = UPPER(?)")
            params.append(sentiment)
        if impact:
            where.append("UPPER(impact) = UPPER(?)")
            params.append(impact)
        if recommendation:
            where.append("UPPER(recommendation) = UPPER(?)")
            params.append(recommendation)

        where_sql = " AND ".join(where)

        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row

            cnt = await db.execute(
                f"SELECT COUNT(*) FROM financial_results WHERE {where_sql}", params
            )
            total = (await cnt.fetchone())[0]

            rows_cur = await db.execute(
                f"""
                SELECT * FROM financial_results
                WHERE {where_sql}
                ORDER BY created_at_unix DESC
                LIMIT ? OFFSET ?
                """,
                [*params, limit, offset],
            )
            rows = await rows_cur.fetchall()

        return [_row_to_record(r) for r in rows], total

    async def search_results(
        self,
        query: str,
        *,
        limit:  int = 20,
        offset: int = 0,
    ) -> tuple[list[FinancialResultRecord], int]:
        """Full-text search across company_name, symbol, executive_summary."""
        now = time.time()
        like = f"%{query}%"

        where_sql = """
            expires_at_unix > ?
            AND (
                company_name LIKE ?
                OR symbol LIKE ?
                OR executive_summary LIKE ?
            )
        """
        params: list[Any] = [now, like, like, like]

        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row

            cnt = await db.execute(
                f"SELECT COUNT(*) FROM financial_results WHERE {where_sql}", params
            )
            total = (await cnt.fetchone())[0]

            rows_cur = await db.execute(
                f"""
                SELECT * FROM financial_results
                WHERE {where_sql}
                ORDER BY created_at_unix DESC
                LIMIT ? OFFSET ?
                """,
                [*params, limit, offset],
            )
            rows = await rows_cur.fetchall()

        return [_row_to_record(r) for r in rows], total


# ─── Row → FinancialResultRecord ────────────────────────────────────────────

def _parse_forecast(val: str | None) -> Forecast | None:
    """Parse a JSON forecast string into a Forecast model."""
    if not val:
        return None
    try:
        data = json.loads(val)
        if data is None:
            return None
        return Forecast(**data)
    except Exception:
        return None


def _row_to_record(row: aiosqlite.Row) -> FinancialResultRecord:
    """Convert a database row to a FinancialResultRecord."""
    d = dict(row)

    # Parse JSON columns
    d["forecast_short_term"]  = _parse_forecast(d.get("forecast_short_term"))
    d["forecast_medium_term"] = _parse_forecast(d.get("forecast_medium_term"))

    source_urls_raw = d.get("source_urls", "[]")
    try:
        d["source_urls"] = json.loads(source_urls_raw) if source_urls_raw else []
    except Exception:
        d["source_urls"] = []

    # Drop DB-only columns not in the Pydantic model
    d.pop("expires_at_unix", None)

    return FinancialResultRecord(**d)
