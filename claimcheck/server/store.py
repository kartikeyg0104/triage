"""Investigations persisted in SQLite (stdlib only).

A connection is opened per call so the store is safe to use from the server's threads and from
several worker processes sharing the same file.
"""

import json
import sqlite3
import time
import uuid
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS investigations (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    source TEXT NOT NULL,
    delivery_id TEXT UNIQUE NULL,
    status TEXT NOT NULL,
    request TEXT NOT NULL,
    result TEXT,
    error TEXT,
    worker TEXT,
    claimed_at REAL,
    attempts INTEGER NOT NULL DEFAULT 0
)
"""
# Added after the first release; older files get them through ALTER TABLE, in this order.
ADDED_COLUMNS = {"error": "TEXT", "worker": "TEXT", "claimed_at": "REAL", "attempts": "INTEGER NOT NULL DEFAULT 0"}
INSERT_COLUMNS = "id, created_at, source, delivery_id, status, request, result"
COLUMNS = INSERT_COLUMNS + ", error, worker"


@dataclass(frozen=True)
class Investigation:
    id: str
    created_at: str
    source: str
    delivery_id: str | None
    status: str
    request: dict
    result: dict | None
    error: str | None = None
    worker: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _row(row: tuple) -> Investigation:
    *head, request, result, error, worker = row
    return Investigation(*head, json.loads(request), json.loads(result) if result is not None else None, error, worker)


class Store:
    def __init__(self, path: Path):
        self.path = path
        with self._immediate() as conn:
            conn.execute(SCHEMA)
            present = {column[1] for column in conn.execute("PRAGMA table_info(investigations)")}
            for name, declaration in ADDED_COLUMNS.items():
                if name not in present:
                    conn.execute(f"ALTER TABLE investigations ADD COLUMN {name} {declaration}")
            conn.execute("CREATE INDEX IF NOT EXISTS investigations_status ON investigations (status, claimed_at)")

    @contextmanager
    def _connect(self):
        """One transaction: commit (or roll back), then close. sqlite3's own `with` does not close."""
        with closing(sqlite3.connect(self.path, timeout=30)) as conn, conn:
            yield conn

    @contextmanager
    def _immediate(self):
        """A transaction that holds the write lock from its first statement, so a read followed by
        an update inside it cannot interleave with another connection's writes."""
        with closing(sqlite3.connect(self.path, timeout=30, isolation_level=None)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    def create(self, source: str, status: str, request: dict, result: dict | None = None,
               delivery_id: str | None = None) -> tuple[Investigation, bool]:
        """Insert a new investigation. With a delivery_id already stored, return that one instead
        (False = not created), so a redelivered webhook never makes a second row."""
        row = (uuid.uuid4().hex, datetime.now(timezone.utc).isoformat(timespec="seconds"), source, delivery_id,
               status, json.dumps(request), json.dumps(result) if result is not None else None)
        with self._connect() as conn:
            inserted = conn.execute(f"INSERT INTO investigations ({INSERT_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?) "
                                    "ON CONFLICT(delivery_id) DO NOTHING", row).rowcount
            if inserted:
                return _row(row + (None, None)), True
            existing = conn.execute(f"SELECT {COLUMNS} FROM investigations WHERE delivery_id = ?",
                                    (delivery_id,)).fetchone()
        return _row(existing), False

    def get(self, investigation_id: str) -> Investigation | None:
        with self._connect() as conn:
            row = conn.execute(f"SELECT {COLUMNS} FROM investigations WHERE id = ?", (investigation_id,)).fetchone()
        return _row(row) if row else None

    def claim(self, worker: str, stale_after: float, max_attempts: int = 3,
              now: float | None = None) -> Investigation | None:
        """Mark the oldest `received` row (or a `processing` row whose worker has held it for over
        `stale_after` seconds) as `processing` by `worker`, and return it. Concurrent callers, in
        any process, never get the same row. A row claimed `max_attempts` times without being
        finished is failed rather than handed out again, so one bad event cannot loop forever."""
        now = time.time() if now is None else now
        cutoff = now - stale_after
        with self._immediate() as conn:
            conn.execute("UPDATE investigations SET status = 'failed', "
                         "error = 'abandoned after ' || attempts || ' attempts: no worker finished it' "
                         "WHERE status = 'processing' AND claimed_at < ? AND attempts >= ?", (cutoff, max_attempts))
            found = conn.execute("SELECT id FROM investigations WHERE status = 'received' "
                                 "OR (status = 'processing' AND claimed_at < ?) "
                                 "ORDER BY created_at, rowid LIMIT 1", (cutoff,)).fetchone()
            if found is None:
                return None
            conn.execute("UPDATE investigations SET status = 'processing', worker = ?, claimed_at = ?, "
                         "attempts = attempts + 1, error = NULL WHERE id = ?", (worker, now, found[0]))
            row = conn.execute(f"SELECT {COLUMNS} FROM investigations WHERE id = ?", found).fetchone()
        return _row(row)

    def finish(self, investigation_id: str, worker: str, status: str, result: dict | None = None,
               error: str | None = None) -> bool:
        """Store the outcome, but only while `worker` still holds the claim: a slow worker whose
        row was reclaimed must not overwrite the newer attempt."""
        with self._connect() as conn:
            return conn.execute(
                "UPDATE investigations SET status = ?, result = ?, error = ? "
                "WHERE id = ? AND worker = ? AND status = 'processing'",
                (status, json.dumps(result) if result is not None else None, error, investigation_id, worker),
            ).rowcount == 1
