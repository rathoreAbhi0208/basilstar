"""
news/db.py
----------
Async SQLite persistence layer for the News package.

Features:
  • WAL mode + NORMAL sync for optimal read/write concurrency.
  • 24-hour TTL with automatic pruning for news_articles.
  • INSERT OR IGNORE for idempotent bulk inserts (deduplication by article id).
  • Separate ``official_information`` table for Official Intelligence records.
  • In-memory cache timestamp for the API "cache_updated_at" field.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional

import aiosqlite

from .models import NewsArticle, RawNewsRecord

logger = logging.getLogger(__name__)

_TTL_HOURS = 24
_TTL_SECS  = _TTL_HOURS * 3600

# ─── DDL ────────────────────────────────────────────────────────────────────

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS news_articles (
    id                      TEXT PRIMARY KEY,
    headline                TEXT    NOT NULL,
    executive_summary       TEXT,               -- Stage 1 + Stage 2 combined summary (primary UI text)
    story                   TEXT    NOT NULL,
    sentiment               TEXT    NOT NULL,
    market_impact_level     TEXT    NOT NULL,
    market_relevance_score  INTEGER NOT NULL DEFAULT 0,  -- Stage 1 score
    confidence_score        INTEGER NOT NULL,
    market_impact           TEXT    NOT NULL,
    retail_investor_impact  TEXT    NOT NULL,
    institutional_impact    TEXT    NOT NULL,
    trading_implications    TEXT,               -- Stage 2 analysis
    risk_factors            TEXT,               -- Stage 2 analysis
    future_outlook          TEXT,               -- Stage 2 analysis
    affected_sectors        TEXT    NOT NULL,   -- JSON array
    affected_companies      TEXT    NOT NULL,   -- JSON array
    market_indices          TEXT,               -- JSON array (e.g. ["Nifty 50", "Sensex"])
    tags                    TEXT    NOT NULL,   -- JSON array
    event_category          TEXT,               -- Stage 1 detected event category
    time_horizon            TEXT,               -- short_term_catalyst | long_term_structural | both
    expires_at_unix         REAL    NOT NULL,
    primary_entity          TEXT,
    entity_type             TEXT,
    source                  TEXT,               -- RSS feed / platform name
    published_at            TEXT,               -- ISO date or null
    image_query             TEXT,
    image_url               TEXT,
    image_alt               TEXT
);
"""

_CREATE_RAW_NEWS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS raw_news_items (
    uid                     TEXT PRIMARY KEY,
    title                   TEXT NOT NULL,
    url                     TEXT NOT NULL,
    summary                 TEXT,
    source_name             TEXT NOT NULL,
    source_tier             INTEGER NOT NULL,
    published_at            TEXT,
    category                TEXT,
    market_relevance_score  INTEGER,
    confidence_score        INTEGER,
    decision                TEXT,
    reason                  TEXT,
    event_category          TEXT,
    time_horizon            TEXT,
    executive_summary       TEXT,
    market_indices_impact   TEXT,
    affected_companies      TEXT,
    affected_sectors        TEXT,
    created_at_unix         REAL NOT NULL
);
"""

_CREATE_IMAGE_CACHE_SQL = """
CREATE TABLE IF NOT EXISTS image_cache (
    query       TEXT PRIMARY KEY,
    image_url   TEXT NOT NULL,
    provider    TEXT NOT NULL,
    timestamp   REAL NOT NULL
);
"""

_INDEXES: list[str] = [
    "CREATE INDEX IF NOT EXISTS idx_expires    ON news_articles (expires_at_unix);",
    "CREATE INDEX IF NOT EXISTS idx_sentiment  ON news_articles (sentiment);",
    "CREATE INDEX IF NOT EXISTS idx_impact     ON news_articles (market_impact_level);",
]



# ─── Insert SQL ─────────────────────────────────────────────────────────────
_INSERT_SQL = """
INSERT OR IGNORE INTO news_articles (
    id, headline, executive_summary, story,
    sentiment, market_impact_level,
    market_relevance_score, confidence_score,
    market_impact, retail_investor_impact, institutional_impact,
    trading_implications, risk_factors, future_outlook,
    affected_sectors, affected_companies, market_indices, tags,
    event_category, time_horizon, expires_at_unix,
    primary_entity, entity_type, source, published_at, image_query, image_url, image_alt
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""

_INSERT_RAW_NEWS_SQL = """
INSERT OR REPLACE INTO raw_news_items (
    uid, title, url, summary, source_name, source_tier, published_at, category,
    market_relevance_score, confidence_score, decision, reason, event_category, time_horizon,
    executive_summary, market_indices_impact, affected_companies, affected_sectors,
    created_at_unix
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""


class NewsDB:
    def __init__(self, db_path: str = "news.db") -> None:
        self._db_path         = db_path
        self._cache_updated_at: str = ""

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def init(self) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("PRAGMA journal_mode=WAL;")
            await db.execute("PRAGMA synchronous=NORMAL;")
            await db.execute("PRAGMA cache_size=-32000;")   # 32 MB page cache
            await db.execute(_CREATE_TABLE_SQL)
            await db.execute(_CREATE_RAW_NEWS_TABLE_SQL)
            await db.execute(_CREATE_IMAGE_CACHE_SQL)
            for idx in _INDEXES:
                await db.execute(idx)
            # Live migrations: add new columns to existing DBs (idempotent)
            _migrations = [
                "ALTER TABLE news_articles ADD COLUMN source TEXT;",
                "ALTER TABLE news_articles ADD COLUMN executive_summary TEXT;",
                "ALTER TABLE news_articles ADD COLUMN market_relevance_score INTEGER NOT NULL DEFAULT 0;",
                "ALTER TABLE news_articles ADD COLUMN trading_implications TEXT;",
                "ALTER TABLE news_articles ADD COLUMN risk_factors TEXT;",
                "ALTER TABLE news_articles ADD COLUMN future_outlook TEXT;",
                "ALTER TABLE news_articles ADD COLUMN market_indices TEXT;",
                "ALTER TABLE news_articles ADD COLUMN event_category TEXT;",
                "ALTER TABLE news_articles ADD COLUMN time_horizon TEXT;",
                "ALTER TABLE news_articles ADD COLUMN market_impact_level TEXT;",
                "ALTER TABLE news_articles ADD COLUMN published_at TEXT;",
                "ALTER TABLE raw_news_items ADD COLUMN time_horizon TEXT;",
            ]
            for migration in _migrations:
                try:
                    await db.execute(migration)
                except Exception:
                    pass   # column already exists
            await db.commit()
        logger.info("[Database] Initialised at %s", self._db_path)

    # ── Properties ───────────────────────────────────────────────────────

    @property
    def cache_updated_at(self) -> str:
        return self._cache_updated_at

    # ── Writes ───────────────────────────────────────────────────────────

    async def bulk_insert(self, articles: list[NewsArticle]) -> int:
        """Insert articles; duplicates (by id) are silently ignored."""
        if not articles:
            return 0

        logger.info("[Database] Saving %d articles", len(articles))

        now_unix = time.time()
        tuples = []
        for a in articles:
            tuples.append((
                a.id,
                a.headline,
                a.executive_summary,
                a.story,
                a.sentiment,
                a.market_impact_level,
                a.market_relevance_score,
                a.confidence_score,
                a.market_impact,
                a.retail_investor_impact,
                a.institutional_impact,
                a.trading_implications,
                a.risk_factors,
                a.future_outlook,
                json.dumps(a.affected_sectors),
                json.dumps(a.affected_companies),
                json.dumps(a.market_indices),
                json.dumps(a.tags),
                a.event_category,
                a.time_horizon,
                now_unix + _TTL_SECS,
                a.primary_entity,
                a.entity_type,
                a.source,
                a.published_at,
                a.image_query,
                a.image_url,
                a.image_alt,
            ))

        async with aiosqlite.connect(self._db_path) as db:
            cursor = await db.executemany(_INSERT_SQL, tuples)
            await db.commit()
            inserted = cursor.rowcount

        if inserted > 0:
            from datetime import datetime, timezone
            self._cache_updated_at = datetime.now(timezone.utc).isoformat()
            logger.info("[Database] Cache refreshed")

        skipped = len(articles) - max(inserted, 0)
        logger.info("[Database] Inserted %d new, skipped %d duplicates", inserted, skipped)
        return max(inserted, 0)

    async def bulk_insert_raw_items(self, items: list[Any]) -> int:
        """Insert evaluated raw items into raw_news_items."""
        if not items:
            return 0
            
        logger.info("[Database] Saving %d raw items", len(items))

        now_unix = time.time()
        tuples = []
        for i in items:
            tuples.append((
                i.raw.uid,
                i.raw.title,
                i.raw.url,
                i.raw.summary,
                i.raw.source_name,
                i.raw.source_tier,
                i.raw.published_at,
                i.raw.category,
                i.evaluation.market_relevance_score,
                i.evaluation.confidence_score,
                i.decision,
                i.evaluation.reason,
                i.evaluation.event_category,
                i.evaluation.time_horizon,
                i.evaluation.executive_summary,
                json.dumps(i.evaluation.market_indices_impact),
                json.dumps(i.evaluation.affected_companies),
                json.dumps(i.evaluation.affected_sectors),
                now_unix
            ))

        async with aiosqlite.connect(self._db_path) as db:
            cursor = await db.executemany(_INSERT_RAW_NEWS_SQL, tuples)
            await db.commit()
            return cursor.rowcount

    async def prune_expired(self) -> int:
        """Delete articles older than 24 h."""
        logger.info("[Database] Pruning old records...")
        async with aiosqlite.connect(self._db_path) as db:
            cursor = await db.execute(
                "DELETE FROM news_articles WHERE expires_at_unix <= ?",
                (time.time(),),
            )
            await db.commit()
        logger.info("[Database] Pruned %d expired articles", cursor.rowcount)
        return cursor.rowcount

    # ── Image Cache ──────────────────────────────────────────────────────

    async def get_cached_image(self, query: str, ttl_hours: int = 24 * 7) -> Optional[dict]:
        """Fetch cached image result if valid within TTL."""
        if not query:
            return None
            
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM image_cache WHERE query = ?",
                (query.lower(),),
            )
            row = await cur.fetchone()
            
        if row:
            if (time.time() - row["timestamp"]) < (ttl_hours * 3600):
                return dict(row)
        return None

    async def set_cached_image(self, query: str, image_url: str, provider: str) -> None:
        """Store image result in cache."""
        if not query or not image_url:
            return
            
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO image_cache (query, image_url, provider, timestamp) VALUES (?, ?, ?, ?)",
                (query.lower(), image_url, provider, time.time())
            )
            await db.commit()

    # ── Reads ────────────────────────────────────────────────────────────

    async def get_by_id(self, article_id: str) -> Optional[NewsArticle]:
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM news_articles WHERE id = ? AND expires_at_unix > ?",
                (article_id, time.time()),
            )
            row = await cur.fetchone()
        return _row_to_article(row) if row else None

    async def list_articles(
        self,
        *,
        sentiment:           Optional[str] = None,
        market_impact_level: Optional[str] = None,
        time_horizon:        Optional[str] = None,
        company:             Optional[str] = None,
        sector:              Optional[str] = None,
        tag:                 Optional[str] = None,
        search:              Optional[str] = None,
        source:              Optional[str] = None,    # filter by source name (e.g. NSE, BSE, SEBI)
        sort:                Optional[str] = None,    # "importance" | "oldest" | "newest" (default)
        limit:               int = 20,
        offset:              int = 0,
    ) -> tuple[list[NewsArticle], int]:

        now = time.time()
        where:  list[str] = ["expires_at_unix > ?"]
        params: list[Any] = [now]

        if sentiment:
            where.append("sentiment = ?");         params.append(sentiment)
        if market_impact_level:
            where.append("market_impact_level = ?"); params.append(market_impact_level)
        if time_horizon:
            where.append("time_horizon = ?");      params.append(time_horizon)
        if company:
            where.append("affected_companies LIKE ?"); params.append(f"%{company}%")
        if sector:
            where.append("affected_sectors LIKE ?");   params.append(f"%{sector}%")
        if tag:
            where.append("tags LIKE ?");           params.append(f"%{tag}%")
        if source:
            where.append("source LIKE ?");         params.append(f"%{source}%")
        if search:
            where.append("(headline LIKE ? OR short_summary LIKE ? OR story LIKE ?)")
            params += [f"%{search}%", f"%{search}%", f"%{search}%"]

        where_sql = " AND ".join(where)
        order_sql = {
            "importance": "market_relevance_score DESC, expires_at_unix DESC",
            "oldest":     "expires_at_unix ASC",
        }.get(sort or "newest", "expires_at_unix DESC")

        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row

            cnt = await db.execute(
                f"SELECT COUNT(*) FROM news_articles WHERE {where_sql}", params
            )
            total = (await cnt.fetchone())[0]

            rows_cur = await db.execute(
                f"""
                SELECT * FROM news_articles
                WHERE {where_sql}
                ORDER BY {order_sql}
                LIMIT ? OFFSET ?
                """,
                [*params, limit, offset],
            )
            rows = await rows_cur.fetchall()

        articles = [_row_to_article(r) for r in rows if r]
        return articles, total

    async def count_live(self) -> int:
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "SELECT COUNT(*) FROM news_articles WHERE expires_at_unix > ?",
                (time.time(),),
            )
            return (await cur.fetchone())[0]

    async def get_existing_uids(self) -> set[str]:
        """Return article IDs from news_articles (non-expired) for dedup."""
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "SELECT id FROM news_articles WHERE expires_at_unix > ?",
                (time.time(),),
            )
            rows = await cur.fetchall()
        return {r[0] for r in rows}

    async def get_raw_existing_uids(self) -> set[str]:
        """Return all UIDs from raw_news_items fetched within the last 48 h.

        Used by the fetcher as the authoritative dedup set — prevents re-processing
        items that were already evaluated (and possibly discarded) by Stage 1.
        The 48-hour window avoids unbounded growth while providing enough overlap
        for all feed refresh frequencies.
        """
        cutoff = time.time() - (48 * 3600)
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "SELECT uid FROM raw_news_items WHERE created_at_unix > ?",
                (cutoff,),
            )
            rows = await cur.fetchall()
        return {r[0] for r in rows}



    async def list_raw_items(
        self,
        *,
        source_name:  Optional[str] = None,
        decision:     Optional[str] = None,
        time_horizon: Optional[str] = None,
        limit:        int = 50,
        offset:       int = 0,
    ) -> tuple[list[RawNewsRecord], int]:
        where:  list[str] = ["1=1"]
        params: list[Any] = []

        if source_name:
            where.append("source_name LIKE ?")
            params.append(f"%{source_name}%")
        if decision:
            where.append("decision = ?")
            params.append(decision)
        if time_horizon:
            where.append("time_horizon = ?")
            params.append(time_horizon)

        where_sql = " AND ".join(where)

        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row

            cnt = await db.execute(
                f"SELECT COUNT(*) FROM raw_news_items WHERE {where_sql}", params
            )
            total = (await cnt.fetchone())[0]

            rows_cur = await db.execute(
                f"""
                SELECT * FROM raw_news_items
                WHERE {where_sql}
                ORDER BY created_at_unix DESC
                LIMIT ? OFFSET ?
                """,
                [*params, limit, offset],
            )
            rows = await rows_cur.fetchall()

        return [_row_to_raw_record(r) for r in rows], total



# ─── Row → NewsArticle ───────────────────────────────────────────────────────

def _row_to_article(row: aiosqlite.Row) -> NewsArticle:
    d = dict(row)
    # Deserialise JSON array columns
    for col in ("affected_sectors", "affected_companies", "market_indices", "tags"):
        if col in d:
            d[col] = json.loads(d.get(col) or "[]")

    # Strip legacy / dropped columns
    for drop_col in (
        "keywords", "estimated_read_time", "generated_at",
        "expires_at", "source_url", "source_name", "source_tier",
        "official_sources", "disclaimer", "expires_at_unix", "image_prompt",
        "category", "subcategory", "importance_score", "impact", "short_summary",
    ):
        d.pop(drop_col, None)

    # Default new fields for rows written by old schema
    d.setdefault("executive_summary", d.get("short_summary", ""))
    d.setdefault("market_relevance_score", 0)
    d.setdefault("market_indices", [])
    d.setdefault("trading_implications", None)
    d.setdefault("risk_factors", None)
    d.setdefault("future_outlook", None)
    d.setdefault("event_category", None)
    d.setdefault("time_horizon", "both")

    return NewsArticle(**d)




# ─── Row → RawNewsRecord ─────────────────────────────────────────────────────

def _row_to_raw_record(r: aiosqlite.Row) -> RawNewsRecord:
    def _parse_json(val: str | None) -> list[str]:
        if not val:
            return []
        try:
            parsed = json.loads(val)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []

    return RawNewsRecord(
        uid=r["uid"],
        title=r["title"],
        url=r["url"],
        summary=r["summary"],
        source_name=r["source_name"],
        source_tier=r["source_tier"],
        published_at=r["published_at"],
        category=r["category"],
        market_relevance_score=r["market_relevance_score"],
        confidence_score=r["confidence_score"],
        decision=r["decision"],
        reason=r["reason"],
        event_category=r["event_category"],
        time_horizon=r["time_horizon"],
        executive_summary=r["executive_summary"],
        market_indices_impact=_parse_json(r["market_indices_impact"]),
        affected_companies=_parse_json(r["affected_companies"]),
        affected_sectors=_parse_json(r["affected_sectors"]),
        created_at_unix=r["created_at_unix"],
    )
