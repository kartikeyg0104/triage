"""Investigations persisted in SQLite (stdlib only).

A connection is opened per call so the store is safe to use from the server's worker threads.
"""

import json
import sqlite3
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
    result TEXT
)
"""
COLUMNS = "id, created_at, source, delivery_id, status, request, result"


@dataclass(frozen=True)
class Investigation:
    id: str
    created_at: str
    source: str
    delivery_id: str | None
    status: str
    request: dict
    result: dict | None

    def to_dict(self) -> dict:
        return asdict(self)


def _row(row: tuple) -> Investigation:
    *head, request, result = row
    return Investigation(*head, json.loads(request), json.loads(result) if result is not None else None)


class Store:
    def __init__(self, path: Path):
        self.path = path
        with self._connect() as conn:
            conn.execute(SCHEMA)

    @contextmanager
    def _connect(self):
        """One transaction: commit (or roll back), then close. sqlite3's own `with` does not close."""
        with closing(sqlite3.connect(self.path, timeout=10)) as conn, conn:
            yield conn

    def create(self, source: str, status: str, request: dict, result: dict | None = None,
               delivery_id: str | None = None) -> tuple[Investigation, bool]:
        """Insert a new investigation. With a delivery_id already stored, return that one instead
        (False = not created), so a redelivered webhook never makes a second row."""
        row = (uuid.uuid4().hex, datetime.now(timezone.utc).isoformat(timespec="seconds"), source, delivery_id,
               status, json.dumps(request), json.dumps(result) if result is not None else None)
        with self._connect() as conn:
            inserted = conn.execute(f"INSERT INTO investigations ({COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?) "
                                    "ON CONFLICT(delivery_id) DO NOTHING", row).rowcount
            if inserted:
                return _row(row), True
            existing = conn.execute(f"SELECT {COLUMNS} FROM investigations WHERE delivery_id = ?",
                                    (delivery_id,)).fetchone()
        return _row(existing), False

    def get(self, investigation_id: str) -> Investigation | None:
        with self._connect() as conn:
            row = conn.execute(f"SELECT {COLUMNS} FROM investigations WHERE id = ?", (investigation_id,)).fetchone()
        return _row(row) if row else None
