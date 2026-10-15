"""SQLite persistence for news items.

Replaces the generate-and-discard JSON storage with a proper SQLite database.
Enables: querying history, weekly rollups, and feedback tracking.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tracker_core.schema import NewsItem, SourceType, Category


logger = logging.getLogger(__name__)


class SQLiteStore:
    """SQLite-backed storage for news items."""

    SCHEMA_VERSION = 1

    def __init__(self, db_path: Path | str = "./data/news.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        """Get or create database connection."""
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.db_path))
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
        return self._conn

    def _init_db(self) -> None:
        """Initialize database schema."""
        conn = self._get_conn()

        conn.executescript("""
            CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER PRIMARY KEY
            );

            CREATE TABLE IF NOT EXISTS news_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                title TEXT NOT NULL,
                url TEXT NOT NULL,
                published_date TEXT,
                fetched_date TEXT NOT NULL DEFAULT (datetime('now')),
                category TEXT,
                summary TEXT,
                relevance_score REAL DEFAULT 0.0,
                is_relevant INTEGER DEFAULT 0,
                source_count INTEGER DEFAULT 1,
                raw_data TEXT,
                UNIQUE(url, title)
            );

            CREATE INDEX IF NOT EXISTS idx_news_source ON news_items(source);
            CREATE INDEX IF NOT EXISTS idx_news_date ON news_items(fetched_date);
            CREATE INDEX IF NOT EXISTS idx_news_category ON news_items(category);
            CREATE INDEX IF NOT EXISTS idx_news_relevance ON news_items(relevance_score);

            CREATE TABLE IF NOT EXISTS feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                timestamp TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (item_id) REFERENCES news_items(id)
            );

            CREATE INDEX IF NOT EXISTS idx_feedback_item ON feedback(item_id);

            CREATE TABLE IF NOT EXISTS source_health (
                name TEXT PRIMARY KEY,
                consecutive_failures INTEGER DEFAULT 0,
                last_success TEXT,
                last_failure TEXT,
                is_disabled INTEGER DEFAULT 0,
                total_items INTEGER DEFAULT 0
            );
        """)

        # Check schema version
        row = conn.execute("SELECT version FROM schema_version").fetchone()
        if row is None:
            conn.execute("INSERT INTO schema_version (version) VALUES (?)", (self.SCHEMA_VERSION,))
            conn.commit()

    def save_items(self, items: list[NewsItem]) -> int:
        """Save news items to database. Returns count of new items inserted."""
        conn = self._get_conn()
        inserted = 0

        for item in items:
            try:
                # NewsItem's legacy default is utcnow() (naive UTC); aware
                # observations are normalized without replacing their age. A
                # non-null tzinfo can still supply no offset, and astimezone()
                # would then read the wall clock as machine-local time.
                fetched = item.fetched_date
                if fetched.tzinfo is None or fetched.utcoffset() is None:
                    fetched = fetched.replace(tzinfo=timezone.utc)
                cursor = conn.execute("""
                    INSERT OR IGNORE INTO news_items
                    (source, title, url, published_date, fetched_date, category, summary,
                     relevance_score, is_relevant, source_count, raw_data)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    item.source.value if hasattr(item.source, "value") else str(item.source),
                    item.title,
                    str(item.url) if item.url else "",
                    item.published_date.isoformat() if item.published_date else None,
                    fetched.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f"),
                    item.category.value if item.category else None,
                    item.summary,
                    item.relevance_score,
                    1 if item.is_relevant else 0,
                    item.source_count,
                    json.dumps(item.raw_data) if item.raw_data else None,
                ))
                inserted += cursor.rowcount
            except sqlite3.Error:
                # Storage failures are not malformed individual observations.
                # Never publish a partial batch as a successful completed run.
                conn.rollback()
                logger.warning("News storage failed; batch rolled back")
                raise
            except Exception as e:
                logger.warning("Failed to save news item (%s); valid siblings retained", type(e).__name__)

        conn.commit()
        # `len(items)` is the size of the batch handed to this call, not the
        # number of rows the database retains. Labelling it "total" made a
        # restored-cache replay read as a running total.
        logger.info(f"Saved {inserted} new items to SQLite "
                    f"({self.count_items(days=0)} retained)")
        return inserted

    def existing_urls(self, items: list[NewsItem]) -> set[str]:
        """URLs already stored, so a run can tell new observations from replays.

        Restored CI cache replays yesterday's news; without this, the same
        breaking item is re-sent on every run as if it were fresh.
        """
        urls = [str(item.url) for item in items if getattr(item, "url", None)]
        if not urls:
            return set()
        conn = self._get_conn()
        known: set[str] = set()
        for start in range(0, len(urls), 400):  # stay under SQLite's variable limit
            chunk = urls[start:start + 400]
            placeholders = ",".join("?" * len(chunk))
            rows = conn.execute(
                f"SELECT url FROM news_items WHERE url IN ({placeholders})", chunk).fetchall()
            known.update(row[0] for row in rows)
        return known

    def get_items(
        self,
        days: int = 7,
        category: str | None = None,
        source: str | None = None,
        min_score: float = 0.0,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Query news items with filters."""
        conn = self._get_conn()

        query = "SELECT * FROM news_items WHERE 1=1"
        params: list[Any] = []

        if days:
            cutoff = datetime.now(timezone.utc).timestamp() - (days * 86400)
            query += " AND julianday(fetched_date) BETWEEN julianday(?, 'unixepoch') AND julianday('now')"
            params.append(cutoff)

        if category:
            query += " AND category = ?"
            params.append(category)

        if source:
            query += " AND source = ?"
            params.append(source)

        if min_score:
            query += " AND relevance_score >= ?"
            params.append(min_score)

        query += " ORDER BY relevance_score DESC, fetched_date DESC LIMIT ?"
        params.append(limit)

        rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def count_items(self, days: int = 7) -> int:
        """Total items retained in the window, independent of any list limit.

        Lets a report disclose what a list cap would otherwise hide.
        """
        conn = self._get_conn()
        if days:
            cutoff = datetime.now(timezone.utc).timestamp() - (days * 86400)
            row = conn.execute(
                "SELECT COUNT(*) FROM news_items WHERE julianday(fetched_date) BETWEEN julianday(?, 'unixepoch') AND julianday('now')",
                (cutoff,)).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) FROM news_items").fetchone()
        return int(row[0])

    def get_weekly_summary(self, weeks: int = 1) -> dict[str, Any]:
        """Get most-covered topics for the past N weeks."""
        conn = self._get_conn()

        # Top categories
        rows = conn.execute("""
            SELECT category, COUNT(*) as count
            FROM news_items
            WHERE julianday(fetched_date) BETWEEN julianday('now', ?) AND julianday('now')
            GROUP BY category
            ORDER BY count DESC
        """, (f"-{weeks * 7} days",)).fetchall()

        top_categories = [{"category": r["category"], "count": r["count"]} for r in rows]

        # Top sources
        rows = conn.execute("""
            SELECT source, COUNT(*) as count
            FROM news_items
            WHERE julianday(fetched_date) BETWEEN julianday('now', ?) AND julianday('now')
            GROUP BY source
            ORDER BY count DESC
        """, (f"-{weeks * 7} days",)).fetchall()

        top_sources = [{"source": r["source"], "count": r["count"]} for r in rows]

        # Total items
        row = conn.execute("""
            SELECT COUNT(*) as total FROM news_items
            WHERE julianday(fetched_date) BETWEEN julianday('now', ?) AND julianday('now')
        """, (f"-{weeks * 7} days",)).fetchone()

        return {
            "period_weeks": weeks,
            "total_items": row["total"] if row else 0,
            "top_categories": top_categories,
            "top_sources": top_sources,
        }

    def record_feedback(self, item_id: int, action: str) -> None:
        """Record user feedback (save, skip, etc.) for an item."""
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO feedback (item_id, action) VALUES (?, ?)",
            (item_id, action)
        )
        conn.commit()

    def get_feedback_for_item(self, item_id: int) -> list[dict[str, Any]]:
        """Get all feedback for a specific item."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM feedback WHERE item_id = ?",
            (item_id,)
        ).fetchall()
        return [dict(row) for row in rows]

    def save_source_health(self, name: str, health: dict[str, Any]) -> None:
        """Persist source health state."""
        conn = self._get_conn()
        conn.execute("""
            INSERT OR REPLACE INTO source_health
            (name, consecutive_failures, last_success, last_failure, is_disabled, total_items)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            name,
            health.get("consecutive_failures", 0),
            health.get("last_success"),
            health.get("last_failure"),
            1 if health.get("is_disabled") else 0,
            health.get("total_items", 0),
        ))
        conn.commit()

    def load_source_health(self) -> dict[str, dict[str, Any]]:
        """Load all source health records, skipping malformed entries."""
        conn = self._get_conn()
        rows = conn.execute("SELECT * FROM source_health").fetchall()
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            name = row["name"]
            cf = row["consecutive_failures"]
            ti = row["total_items"]
            if not isinstance(cf, int) or isinstance(cf, bool) or cf < 0:
                logger.warning(f"Skipping invalid health entry '{name}': consecutive_failures={cf!r}")
                continue
            if not isinstance(ti, int) or isinstance(ti, bool) or ti < 0:
                logger.warning(f"Skipping invalid health entry '{name}': total_items={ti!r}")
                continue
            result[name] = {
                "consecutive_failures": cf,
                "last_success": row["last_success"],
                "last_failure": row["last_failure"],
                "is_disabled": bool(row["is_disabled"]),
                "total_items": ti,
            }
        return result

    def close(self) -> None:
        """Close database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None

    def __del__(self):
        self.close()
