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
WRITEBACK_SCHEMA = """
CREATE TABLE IF NOT EXISTS writebacks (
    investigation_id TEXT PRIMARY KEY,
    target TEXT NOT NULL,
    outcome TEXT NOT NULL,
    error TEXT
)
"""
# Added after the first release; older files get them through ALTER TABLE, in this order.
ADDED_COLUMNS = {"error": "TEXT", "worker": "TEXT", "claimed_at": "REAL", "attempts": "INTEGER NOT NULL DEFAULT 0"}
INSERT_COLUMNS = "id, created_at, source, delivery_id, status, request, result"
COLUMNS = INSERT_COLUMNS + ", error, worker, " \
    "(SELECT outcome FROM writebacks WHERE investigation_id = investigations.id), " \
    "(SELECT error FROM writebacks WHERE investigation_id = investigations.id)"
TARGET = "replace(lower(investigations.source), 'github:pull_request:', 'github:issues:')"


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
    writeback: str | None = None  # "pending", "created", "updated" or "error"
    writeback_error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _row(row: tuple) -> Investigation:
    *head, request, result, error, worker, writeback, writeback_error = row
    return Investigation(*head, json.loads(request), json.loads(result) if result is not None else None, error, worker,
                         writeback, writeback_error)


class Store:
    def __init__(self, path: Path):
        self.path = path
        with self._immediate() as conn:
            conn.execute(SCHEMA)
            present = {column[1] for column in conn.execute("PRAGMA table_info(investigations)")}
            for name, declaration in ADDED_COLUMNS.items():
                if name not in present:
                    conn.execute(f"ALTER TABLE investigations ADD COLUMN {name} {declaration}")
            conn.execute(WRITEBACK_SCHEMA)
            conn.execute("CREATE INDEX IF NOT EXISTS writebacks_target ON writebacks (target, outcome)")
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
                return _row(row + (None, None, None, None)), True
            existing = conn.execute(f"SELECT {COLUMNS} FROM investigations WHERE delivery_id = ?",
                                    (delivery_id,)).fetchone()
        return _row(existing), False

    def get(self, investigation_id: str) -> Investigation | None:
        with self._connect() as conn:
            row = conn.execute(f"SELECT {COLUMNS} FROM investigations WHERE id = ?", (investigation_id,)).fetchone()
        return _row(row) if row else None

    def list(self, status: str | None = None, limit: int = 50, offset: int = 0) -> list[Investigation]:
        """Investigations newest first (insertion order breaks ties), optionally only one status."""
        where, params = ("WHERE status = ?", (status,)) if status is not None else ("", ())
        with self._connect() as conn:
            found = conn.execute(f"SELECT {COLUMNS} FROM investigations {where} "
                                 "ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?",
                                 params + (limit, offset)).fetchall()
        return [_row(row) for row in found]

    def claim(self, worker: str, stale_after: float, max_attempts: int = 3,
              now: float | None = None, *, writeback: bool = False) -> Investigation | None:
        """Mark the oldest `received` row (or a `processing` row whose worker has held it for over
        `stale_after` seconds) as `processing` by `worker`, and return it. Concurrent callers, in
        any process, never get the same row. A row claimed `max_attempts` times without being
        finished is failed rather than handed out again. With writeback, the target stays reserved
        until the comment attempt finishes, so concurrent checks cannot create duplicate comments."""
        now = time.time() if now is None else now
        cutoff = now - stale_after
        with self._immediate() as conn:
            conn.execute("UPDATE writebacks SET outcome = 'error', error = 'abandoned writeback: no worker finished it' "
                         "WHERE outcome = 'pending' AND investigation_id IN "
                         "(SELECT id FROM investigations WHERE status = 'completed' AND claimed_at < ?)", (cutoff,))
            conn.execute("UPDATE investigations SET status = 'failed', "
                         "error = 'abandoned after ' || attempts || ' attempts: no worker finished it' "
                         "WHERE status = 'processing' AND claimed_at < ? AND attempts >= ?", (cutoff, max_attempts))
            conn.execute("DELETE FROM writebacks WHERE outcome = 'pending' AND investigation_id IN "
                         "(SELECT id FROM investigations WHERE status IN ('failed', 'skipped'))")
            found = conn.execute("SELECT id FROM investigations WHERE (status = 'received' "
                                 "OR (status = 'processing' AND claimed_at < ?)) AND NOT EXISTS "
                                 "(SELECT 1 FROM writebacks AS pending JOIN investigations AS active "
                                 "ON active.id = pending.investigation_id WHERE pending.outcome = 'pending' "
                                 f"AND active.claimed_at >= ? AND pending.target = {TARGET}) "
                                 "ORDER BY created_at, rowid LIMIT 1", (cutoff, cutoff)).fetchone()
            if found is None:
                return None
            conn.execute("UPDATE investigations SET status = 'processing', worker = ?, claimed_at = ?, "
                         "attempts = attempts + 1, error = NULL "
                         "WHERE id = ?", (worker, now, found[0]))
            conn.execute("DELETE FROM writebacks WHERE investigation_id = ?", found)
            if writeback:
                conn.execute("INSERT INTO writebacks (investigation_id, target, outcome) "
                             f"SELECT id, {TARGET}, 'pending' FROM investigations WHERE id = ?", found)
            row = conn.execute(f"SELECT {COLUMNS} FROM investigations WHERE id = ?", found).fetchone()
        return _row(row)

    def finish(self, investigation_id: str, worker: str, status: str, result: dict | None = None,
               error: str | None = None) -> bool:
        """Store the outcome, but only while `worker` still holds the claim: a slow worker whose
        row was reclaimed must not overwrite the newer attempt."""
        with self._connect() as conn:
            finished = conn.execute(
                "UPDATE investigations SET status = ?, result = ?, error = ? "
                "WHERE id = ? AND worker = ? AND status = 'processing'",
                (status, json.dumps(result) if result is not None else None, error, investigation_id, worker),
            ).rowcount == 1
            if finished and status != "completed":
                conn.execute("DELETE FROM writebacks WHERE investigation_id = ?", (investigation_id,))
            return finished

    def set_writeback(self, investigation_id: str, worker: str, outcome: str, error: str | None = None) -> bool:
        """Release the target after commenting without changing the check's verdict."""
        with self._connect() as conn:
            return conn.execute(
                "UPDATE writebacks SET outcome = ?, error = ? WHERE investigation_id = ? AND outcome = 'pending' "
                "AND investigation_id IN (SELECT id FROM investigations WHERE worker = ? AND status = 'completed')",
                (outcome, error, investigation_id, worker),
            ).rowcount == 1
