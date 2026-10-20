# worker/queue.py
"""The worker's outbox.

02_ARCHITECTURE.md promises that server downtime loses no data. That promise
is kept by this file or it is not kept at all, so it is a SQLite database in
WAL mode rather than a hand-rolled append log: crash-safe append, ordered
read, delete-on-acknowledge and a bounded size are a small database, and
writing one badly is the usual way to lose the data you promised to keep.

Events are generated with their UUID here, before any send is attempted. That
is what makes a retry after a timeout safe: the server inserts
ON CONFLICT DO NOTHING on the same id, so a resend cannot double-count
(04_API_CONTRACT.md).

When the queue is full the oldest events are dropped. The alternative is to
stop observing, which loses the present to protect the past. Drops are counted
and logged, never silent.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
    rowid_alias INTEGER PRIMARY KEY AUTOINCREMENT,
    id          TEXT NOT NULL UNIQUE,
    payload     TEXT NOT NULL,
    queued_at   TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class QueuedEvent:
    row: int
    id: str
    payload: dict


class EventQueue:
    def __init__(self, path: Path, max_events: int = 50_000) -> None:
        self.path = path
        self.max_events = max_events
        self.dropped = 0
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript(SCHEMA)
        self._db.commit()
        depth = self.depth()
        if depth:
            logger.info("%d event(s) recovered from the previous run", depth)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ------------------------------------------------------------- writing
    def append(self, *, slot_id, previous_status: str, status: str,
               confidence: float, observed_at: datetime,
               model_version: str) -> dict:
        payload = {
            "id": str(uuid.uuid4()),
            "slot_id": str(slot_id),
            "previous_status": previous_status,
            "status": status,
            "confidence": round(float(confidence), 3),
            "observed_at": observed_at.isoformat(),
            "model_version": model_version,
        }
        with self._lock:
            self._db.execute(
                "INSERT INTO outbox (id, payload, queued_at) VALUES (?, ?, ?)",
                (payload["id"], json.dumps(payload), observed_at.isoformat()))
            self._db.commit()
            self._trim()
        return payload

    def _trim(self) -> None:
        (depth,) = self._db.execute("SELECT count(*) FROM outbox").fetchone()
        excess = depth - self.max_events
        if excess <= 0:
            return
        self._db.execute(
            "DELETE FROM outbox WHERE rowid_alias IN "
            "(SELECT rowid_alias FROM outbox ORDER BY rowid_alias LIMIT ?)",
            (excess,))
        self._db.commit()
        self.dropped += excess
        logger.error("queue full at %d events: dropped the %d oldest "
                     "(%d dropped in total this run)",
                     self.max_events, excess, self.dropped)

    # ------------------------------------------------------------- reading
    def depth(self) -> int:
        with self._lock:
            (depth,) = self._db.execute("SELECT count(*) FROM outbox").fetchone()
        return depth

    def peek(self, limit: int) -> list[QueuedEvent]:
        """Oldest first: the server's state derivation is order-independent,
        but a queue that reorders would make a human reading the log despair."""
        with self._lock:
            rows = self._db.execute(
                "SELECT rowid_alias, id, payload FROM outbox "
                "ORDER BY rowid_alias LIMIT ?", (limit,)).fetchall()
        return [QueuedEvent(row=row[0], id=row[1], payload=json.loads(row[2]))
                for row in rows]

    def acknowledge(self, events: list[QueuedEvent]) -> None:
        """Removes events the server has accepted — including ones it reported
        as duplicates or rejected. A rejected event will be rejected again; a
        duplicate is already stored. Either way it must leave the queue or it
        blocks everything behind it forever."""
        if not events:
            return
        with self._lock:
            self._db.executemany("DELETE FROM outbox WHERE rowid_alias = ?",
                                 [(event.row,) for event in events])
            self._db.commit()