"""
news/db.py
----------
Async SQLite persistence layer for the News package.

Features:
  • WAL mode + NORMAL sync for optimal read/write concurrency.
  • 24-hour TTL with automatic pruning.
  • INSERT OR IGNORE for idempotent bulk inserts (deduplication by article id).
  • Columns for image_url / image_alt (the new fields).
  • In-memory cache timestamp for the API "cache_updated_at" field.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional

import aiosqlite

from .models import NewsArticle

logger = logging.getLogger(__name__)

_TTL_HOURS = 24
_TTL_SECS  = _TTL_HOURS * 3600

# ─── DDL ────────────────────────────────────────────────────────────────────

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS news_articles (
    id                      TEXT PRIMARY KEY,
    headline                TEXT    NOT NULL,
    short_summary           TEXT    NOT NULL,
    story                   TEXT    NOT NULL,
    category                TEXT    NOT NULL,
    subcategory             TEXT    NOT NULL,
    sentiment               TEXT    NOT NULL,
    impact                  TEXT    NOT NULL,
    importance_score        INTEGER NOT NULL,
    confidence_score        INTEGER NOT NULL,
    market_impact           TEXT    NOT NULL,
    retail_investor_impact  TEXT    NOT NULL,
    institutional_impact    TEXT    NOT NULL,
    affected_sectors        TEXT    NOT NULL,   -- JSON array
    affected_companies      TEXT    NOT NULL,   -- JSON array
    tags                    TEXT    NOT NULL,   -- JSON array
    expires_at_unix         REAL    NOT NULL,
    image_url               TEXT,
    image_alt               TEXT
);
"""

_INDEXES: list[str] = [
    "CREATE INDEX IF NOT EXISTS idx_expires    ON news_articles (expires_at_unix);",
    "CREATE INDEX IF NOT EXISTS idx_category   ON news_articles (category);",
    "CREATE INDEX IF NOT EXISTS idx_sentiment  ON news_articles (sentiment);",
    "CREATE INDEX IF NOT EXISTS idx_impact     ON news_articles (impact);",
    "CREATE INDEX IF NOT EXISTS idx_importance ON news_articles (importance_score DESC);",
]

# ─── Insert SQL ─────────────────────────────────────────────────────────────
_INSERT_SQL = """
INSERT OR IGNORE INTO news_articles (
    id, headline, short_summary, story, category, subcategory,
    sentiment, impact, importance_score, confidence_score,
    market_impact, retail_investor_impact, institutional_impact,
    affected_sectors, affected_companies, tags, expires_at_unix,
    image_url, image_alt
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
            for idx in _INDEXES:
                await db.execute(idx)
            await db.commit()
        logger.info("[Database] Connection opened at %s", self._db_path)

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
                a.short_summary,
                a.story,
                a.category,
                a.subcategory,
                a.sentiment,
                a.impact,
                a.importance_score,
                a.confidence_score,
                a.market_impact,
                a.retail_investor_impact,
                a.institutional_impact,
                json.dumps(a.affected_sectors),
                json.dumps(a.affected_companies),
                json.dumps(a.tags),
                now_unix + _TTL_SECS,
                a.image_url,
                a.image_alt,
            ))

        async with aiosqlite.connect(self._db_path) as db:
            cursor = await db.executemany(_INSERT_SQL, tuples)
            await db.commit()
            inserted = cursor.rowcount

        if inserted > 0:
            logger.info("[Database] Cache refreshed")
            from datetime import datetime, timezone
            self._cache_updated_at = datetime.now(timezone.utc).isoformat()

        skipped = len(articles) - max(inserted, 0)
        logger.info("[Database] Inserted %d new, skipped %d duplicates", inserted, skipped)
        logger.info("[Database] Save completed")
        return max(inserted, 0)

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
        category:  Optional[str] = None,
        sentiment: Optional[str] = None,
        impact:    Optional[str] = None,
        company:   Optional[str] = None,
        sector:    Optional[str] = None,
        tag:       Optional[str] = None,
        search:    Optional[str] = None,
        sort:      Optional[str] = None,    # "importance" | "oldest" | "newest" (default)
        limit:     int = 20,
        offset:    int = 0,
    ) -> tuple[list[NewsArticle], int]:

        now = time.time()
        where:  list[str] = ["expires_at_unix > ?"]
        params: list[Any] = [now]

        if category:
            where.append("category = ?");          params.append(category)
        if sentiment:
            where.append("sentiment = ?");         params.append(sentiment)
        if impact:
            where.append("impact = ?");            params.append(impact)
        if company:
            where.append("affected_companies LIKE ?"); params.append(f"%{company}%")
        if sector:
            where.append("affected_sectors LIKE ?");   params.append(f"%{sector}%")
        if tag:
            where.append("tags LIKE ?");           params.append(f"%{tag}%")
        if search:
            where.append("(headline LIKE ? OR short_summary LIKE ? OR story LIKE ?)")
            params += [f"%{search}%", f"%{search}%", f"%{search}%"]

        where_sql = " AND ".join(where)
        order_sql = {
            "importance": "importance_score DESC, expires_at_unix DESC",
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
        """Return all non-expired article IDs (for dedup in fetcher)."""
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "SELECT id FROM news_articles WHERE expires_at_unix > ?",
                (time.time(),),
            )
            rows = await cur.fetchall()
        return {r[0] for r in rows}


# ─── Row → Model ────────────────────────────────────────────────────────────

def _row_to_article(row: aiosqlite.Row) -> NewsArticle:
    d = dict(row)
    for col in ("affected_sectors", "affected_companies", "tags"):
        if col in d:
            d[col] = json.loads(d.get(col) or "[]")
    
    # Strip out any dropped columns in case the db file still has them
    for drop_col in ("keywords", "estimated_read_time", "published_at", "generated_at", 
                     "expires_at", "source_url", "source_name", "source_tier", 
                     "official_sources", "disclaimer", "expires_at_unix", "image_prompt"):
        d.pop(drop_col, None)
        
    return NewsArticle(**d)
