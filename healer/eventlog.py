"""Shared event log (SQLite, WAL mode).

Written by the healer daemon, read by the Django dashboard.
"""
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         REAL NOT NULL,
    service    TEXT,
    event_type TEXT NOT NULL,
    severity   TEXT NOT NULL DEFAULT 'info',
    message    TEXT NOT NULL DEFAULT '',
    details    TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_service ON events(service, ts);

CREATE TABLE IF NOT EXISTS service_state (
    name       TEXT PRIMARY KEY,
    status     TEXT NOT NULL,
    pid        INTEGER,
    restarts   INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL
);
"""


class Severity:
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class EventType:
    """Shared vocabulary; new phases add entries here."""
    HEALER_STARTED = "healer_started"
    HEALER_STOPPED = "healer_stopped"
    SERVICE_STARTED = "service_started"
    SERVICE_CRASHED = "service_crashed"
    SERVICE_HUNG = "service_hung"
    SERVICE_RESTARTED = "service_restarted"
    CIRCUIT_OPEN = "circuit_open"
    FILE_CORRUPTED = "file_corrupted"
    FILE_RESTORED = "file_restored"
    CHECKPOINT_SAVED = "checkpoint_saved"
    CHECKPOINT_RESTORED = "checkpoint_restored"


class EventLog:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            self.path, timeout=10, isolation_level=None, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- events -------------------------------------------------------
    def log(self, event_type: str, service: Optional[str] = None,
            severity: str = Severity.INFO, message: str = "", **details: Any) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO events(ts, service, event_type, severity, message, details) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (time.time(), service, event_type, severity, message,
                 json.dumps(details) if details else None),
            )
            return cur.lastrowid

    def recent(self, limit: int = 50, service: Optional[str] = None) -> list[dict]:
        sql, params = "SELECT * FROM events", []
        if service:
            sql += " WHERE service = ?"
            params.append(service)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        events = []
        for row in rows:
            event = dict(row)
            event["details"] = json.loads(event["details"]) if event["details"] else {}
            events.append(event)
        return events

    # ---- current service state ---------------------------------------
    def set_state(self, name: str, status: str,
                  pid: Optional[int] = None, restarts: int = 0) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO service_state(name, status, pid, restarts, updated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET status=excluded.status, "
                "pid=excluded.pid, restarts=excluded.restarts, "
                "updated_at=excluded.updated_at",
                (name, status, pid, restarts, time.time()),
            )

    def get_states(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM service_state ORDER BY name"
            ).fetchall()
        return [dict(r) for r in rows]