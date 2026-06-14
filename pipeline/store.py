"""SQLite/libSQL store for Parallel Frontier.

One store, three tables (see SPEC.md "Data model"):

  items          -- identity: one row per logical item (stable URL)
  item_versions  -- one row per (item, normalized-content hash) ever seen;
                    this is what makes docket change-detection possible AND
                    is the "future dataset"
  source_runs    -- per-source result of each run; powers the digest heartbeat

Connection target is chosen by environment:

  * TURSO_DATABASE_URL set  -> remote libSQL (Turso), the production store.
                               Requires TURSO_AUTH_TOKEN.
  * otherwise               -> local SQLite file at $PF_DB_PATH or ./data/items.db,
                               for development and tests.

Both backends speak DB-API 2.0 (execute / executemany / commit / fetchall), so
the rest of this module is backend-agnostic. Queries select explicit columns and
read rows positionally to avoid relying on a row factory.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Literal, Optional

# ── enums (mirrored as CHECK constraints in the schema) ──────────────────────

Tier = Literal["alert", "digest", "archive"]
Category = Literal["regulation", "research", "discourse", "rivals"]

TIERS: tuple[str, ...] = ("alert", "digest", "archive")
CATEGORIES: tuple[str, ...] = ("regulation", "research", "discourse", "rivals")

# Result of an upsert — drives ALERT detection for docket pages.
UpsertStatus = Literal["new", "changed", "unchanged"]

DEFAULT_LOCAL_DB = Path("data/items.db")


def utc_now() -> str:
    """UTC ISO-8601 timestamp, microsecond precision.

    Microseconds matter: alert detection captures a `started` timestamp before a
    run and selects versions with `fetched_at >= started`. Second precision lets
    the previous run's write collide with the next run's start, causing a false
    re-alert. Full precision keeps the boundary strictly ordered.
    """
    return datetime.now(timezone.utc).isoformat()


_now = utc_now  # internal alias


# ── data model ───────────────────────────────────────────────────────────────


@dataclass(slots=True)
class Item:
    """An item as produced by a collector, before storage.

    `content_hash` must be computed over NORMALIZED content (boilerplate
    stripped — see pipeline/normalize.py), or the alert rate will be wrong.
    """

    source: str
    url: str
    title: str
    content_hash: str
    raw_excerpt: str = ""
    title_en: str = ""
    published_at: Optional[str] = None
    tier: Tier = "archive"
    category: Optional[Category] = None
    pinned: bool = False


SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
  id            INTEGER PRIMARY KEY,
  source        TEXT NOT NULL,
  url           TEXT UNIQUE NOT NULL,
  title         TEXT,
  title_en      TEXT,
  published_at  TEXT,
  first_seen    TEXT NOT NULL,
  last_seen     TEXT NOT NULL,
  current_hash  TEXT,
  tier          TEXT NOT NULL CHECK (tier IN ('alert','digest','archive')),
  category      TEXT CHECK (category IS NULL OR
                            category IN ('regulation','research','discourse','rivals')),
  pinned        INTEGER NOT NULL DEFAULT 0,   -- roster hits pin to top of their section
  used_in_issue INTEGER
);

CREATE TABLE IF NOT EXISTS item_versions (
  id           INTEGER PRIMARY KEY,
  item_id      INTEGER NOT NULL REFERENCES items(id),
  content_hash TEXT NOT NULL,
  raw_excerpt  TEXT,
  fetched_at   TEXT NOT NULL,
  UNIQUE (item_id, content_hash)
);

CREATE TABLE IF NOT EXISTS source_runs (
  id         INTEGER PRIMARY KEY,
  source     TEXT NOT NULL,
  run_at     TEXT NOT NULL,
  ok         INTEGER NOT NULL,
  item_count INTEGER,
  error      TEXT
);

CREATE INDEX IF NOT EXISTS idx_items_tier        ON items(tier);
CREATE INDEX IF NOT EXISTS idx_items_category    ON items(category);
CREATE INDEX IF NOT EXISTS idx_items_last_seen   ON items(last_seen);
CREATE INDEX IF NOT EXISTS idx_versions_item     ON item_versions(item_id);
CREATE INDEX IF NOT EXISTS idx_runs_source_runat ON source_runs(source, run_at);
"""


# ── connection ───────────────────────────────────────────────────────────────


def _connect_raw():
    """Open a connection to the configured backend (caller owns closing)."""
    url = os.environ.get("TURSO_DATABASE_URL")
    if url:
        try:
            import libsql_experimental as libsql  # type: ignore
        except ImportError as exc:  # pragma: no cover - depends on env
            raise RuntimeError(
                "TURSO_DATABASE_URL is set but libsql is not installed; "
                "`pip install libsql-experimental`"
            ) from exc
        token = os.environ.get("TURSO_AUTH_TOKEN")
        return libsql.connect(database=url, auth_token=token)

    path = Path(os.environ.get("PF_DB_PATH", DEFAULT_LOCAL_DB))
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


class Store:
    """Thin wrapper holding a connection and the domain operations."""

    def __init__(self, conn) -> None:
        self.conn = conn

    # -- lifecycle -----------------------------------------------------------

    def init_schema(self) -> None:
        """Create tables and indexes if absent. Idempotent."""
        cur = self.conn.cursor()
        for statement in filter(str.strip, SCHEMA.split(";")):
            cur.execute(statement)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # -- writes --------------------------------------------------------------

    def upsert(self, item: Item) -> UpsertStatus:
        """Insert or update an item, tracking versions.

        Returns:
            "new"       -- url not seen before
            "changed"   -- url seen, but normalized content hash differs
                           (this is the docket-diff ALERT trigger)
            "unchanged" -- url seen, same content hash (only last_seen bumped)
        """
        now = _now()
        cur = self.conn.cursor()
        cur.execute(
            "SELECT id, current_hash FROM items WHERE url = ?", (item.url,)
        )
        row = cur.fetchone()

        if row is None:
            cur.execute(
                """INSERT INTO items
                     (source, url, title, title_en, published_at,
                      first_seen, last_seen, current_hash, tier, category, pinned, used_in_issue)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)""",
                (
                    item.source, item.url, item.title, item.title_en,
                    item.published_at, now, now, item.content_hash,
                    item.tier, item.category, 1 if item.pinned else 0,
                ),
            )
            # Resolve the id via the UNIQUE url rather than cursor.lastrowid:
            # remote libSQL doesn't reliably populate lastrowid over HTTP.
            cur.execute("SELECT id FROM items WHERE url = ?", (item.url,))
            item_id = cur.fetchone()[0]
            self._add_version(cur, item_id, item.content_hash, item.raw_excerpt, now)
            self.conn.commit()
            return "new"

        item_id, current_hash = row[0], row[1]

        if current_hash == item.content_hash:
            cur.execute("UPDATE items SET last_seen = ? WHERE id = ?", (now, item_id))
            self.conn.commit()
            return "unchanged"

        # Content changed: record the new version, then advance current_hash.
        cur.execute(
            """UPDATE items
                 SET current_hash = ?, last_seen = ?, title = ?, tier = ?,
                     category = ?, pinned = ?
               WHERE id = ?""",
            (item.content_hash, now, item.title, item.tier, item.category,
             1 if item.pinned else 0, item_id),
        )
        self._add_version(cur, item_id, item.content_hash, item.raw_excerpt, now)
        self.conn.commit()
        return "changed"

    @staticmethod
    def _add_version(cur, item_id: int, content_hash: str, excerpt: str, ts: str) -> None:
        # UNIQUE(item_id, content_hash): a hash that reappears after a revert
        # is harmless to skip.
        cur.execute(
            """INSERT OR IGNORE INTO item_versions
                 (item_id, content_hash, raw_excerpt, fetched_at)
               VALUES (?, ?, ?, ?)""",
            (item_id, content_hash, excerpt, ts),
        )

    def record_run(
        self, source: str, ok: bool, item_count: Optional[int] = None,
        error: Optional[str] = None,
    ) -> None:
        """Record a per-source run result for the digest heartbeat."""
        cur = self.conn.cursor()
        cur.execute(
            """INSERT INTO source_runs (source, run_at, ok, item_count, error)
               VALUES (?, ?, ?, ?, ?)""",
            (source, _now(), 1 if ok else 0, item_count, error),
        )
        self.conn.commit()

    def mark_used_in_issue(self, item_id: int, issue: int) -> None:
        """Editorial bookkeeping: tag an item as used in a published issue."""
        cur = self.conn.cursor()
        cur.execute("UPDATE items SET used_in_issue = ? WHERE id = ?", (issue, item_id))
        self.conn.commit()

    # -- reads ---------------------------------------------------------------

    def digest_items(self, category: Category, since: str) -> list[tuple]:
        """digest-tier items in a category first seen at/after `since` (ISO ts).

        Returns rows of
        (id, source, url, title, title_en, published_at, first_seen, pinned),
        pinned (roster hits) first.
        """
        cur = self.conn.cursor()
        cur.execute(
            """SELECT id, source, url, title, title_en, published_at, first_seen, pinned
                 FROM items
                WHERE tier = 'digest' AND category = ? AND first_seen >= ?
                ORDER BY pinned DESC, first_seen DESC""",
            (category, since),
        )
        return cur.fetchall()

    def alerts_since(self, since: str) -> list[tuple]:
        """alert-tier items whose latest version was fetched at/after `since`."""
        cur = self.conn.cursor()
        cur.execute(
            """SELECT i.id, i.source, i.url, i.title, MAX(v.fetched_at) AS changed_at
                 FROM items i
                 JOIN item_versions v ON v.item_id = i.id
                WHERE i.tier = 'alert' AND v.fetched_at >= ?
                GROUP BY i.id
                ORDER BY changed_at DESC""",
            (since,),
        )
        return cur.fetchall()

    def latest_runs(self) -> list[tuple]:
        """Most-recent run per source, for the heartbeat footer.

        Returns rows of (source, run_at, ok, item_count, error).
        """
        cur = self.conn.cursor()
        cur.execute(
            """SELECT s.source, s.run_at, s.ok, s.item_count, s.error
                 FROM source_runs s
                 JOIN (SELECT source, MAX(run_at) AS m
                         FROM source_runs GROUP BY source) latest
                   ON latest.source = s.source AND latest.m = s.run_at
                ORDER BY s.source""",
        )
        return cur.fetchall()


@contextmanager
def connect() -> Iterator[Store]:
    """Context manager yielding an initialized Store."""
    store = Store(_connect_raw())
    try:
        store.init_schema()
        yield store
    finally:
        store.close()


if __name__ == "__main__":  # `python -m pipeline.store init`
    import sys

    cmd = sys.argv[1] if len(sys.argv) > 1 else "init"
    with connect() as s:
        if cmd == "init":
            target = os.environ.get("TURSO_DATABASE_URL", os.environ.get("PF_DB_PATH", DEFAULT_LOCAL_DB))
            print(f"schema initialized on {target}")
        else:
            sys.exit(f"unknown command: {cmd}")
