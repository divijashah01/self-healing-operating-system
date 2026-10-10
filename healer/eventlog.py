"""Shared event log (SQLite, WAL mode).

Written by the healer daemon, read by the Django dashboard.
"""
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

SCHEMA_VERSION = 3

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
    name        TEXT PRIMARY KEY,
    status      TEXT NOT NULL,
    pid         INTEGER,
    restarts    INTEGER NOT NULL DEFAULT 0,
    cpu_percent REAL,
    rss_mb      REAL,
    updated_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS file_versions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    path        TEXT NOT NULL,
    version     INTEGER NOT NULL,
    sha256      TEXT NOT NULL,
    size        INTEGER NOT NULL,
    mode        INTEGER NOT NULL,
    created_at  REAL NOT NULL,
    note        TEXT NOT NULL DEFAULT '',
    blob_path   TEXT NOT NULL,
    is_baseline INTEGER NOT NULL DEFAULT 0,
    UNIQUE(path, version)
);
CREATE INDEX IF NOT EXISTS idx_file_versions_path ON file_versions(path, version);
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
    SERVICE_STOPPED = "service_stopped"
    ZOMBIE_DETECTED = "zombie_detected"
    ZOMBIE_REAPED = "zombie_reaped"
    RESTART_SCHEDULED = "restart_scheduled"
    CIRCUIT_CLOSED = "circuit_closed"
    HANG_ESCALATED = "hang_escalated"
    FILE_MISSING = "file_missing"
    FILE_MODIFIED = "file_modified"
    FILE_BASELINED = "file_baselined"
    FILE_APPROVED = "file_approved"


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
    def set_state(self, name: str, status: str, pid: Optional[int] = None,
                  restarts: int = 0, cpu_percent: Optional[float] = None,
                  rss_mb: Optional[float] = None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO service_state"
                "(name, status, pid, restarts, cpu_percent, rss_mb, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET status=excluded.status, "
                "pid=excluded.pid, restarts=excluded.restarts, "
                "cpu_percent=excluded.cpu_percent, rss_mb=excluded.rss_mb, "
                "updated_at=excluded.updated_at",
                (name, status, pid, restarts, cpu_percent, rss_mb, time.time()),
            )

    def get_states(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM service_state ORDER BY name"
            ).fetchall()
        return [dict(r) for r in rows]

    # ---- file versions (used by the file vault) ------------------------
    def add_file_version(self, path: str, sha256: str, size: int, mode: int,
                         blob_path: str, note: str = "", baseline: bool = False) -> int:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")      # version numbering must be atomic
            try:
                version = self._conn.execute(
                    "SELECT COALESCE(MAX(version), 0) + 1 FROM file_versions WHERE path = ?",
                    (path,)).fetchone()[0]
                if baseline:
                    self._conn.execute(
                        "UPDATE file_versions SET is_baseline = 0 WHERE path = ?", (path,))
                self._conn.execute(
                    "INSERT INTO file_versions(path, version, sha256, size, mode, created_at, "
                    "note, blob_path, is_baseline) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (path, version, sha256, size, mode, time.time(), note, blob_path,
                     int(baseline)))
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            return version

    def file_versions(self, path: str) -> list:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM file_versions WHERE path = ? ORDER BY version DESC",
                (path,)).fetchall()
        return [dict(r) for r in rows]

    def get_file_version(self, path: str, version: int) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM file_versions WHERE path = ? AND version = ?",
                (path, version)).fetchone()
        return dict(row) if row else None

    def get_baseline(self, path: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM file_versions WHERE path = ? AND is_baseline = 1",
                (path,)).fetchone()
        return dict(row) if row else None

    def set_baseline(self, path: str, version: int) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE file_versions SET is_baseline = (version = ?) WHERE path = ? "
                "AND EXISTS (SELECT 1 FROM file_versions WHERE path = ? AND version = ?)",
                (version, path, path, version))
            return cur.rowcount > 0

    def delete_file_version(self, path: str, version: int) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM file_versions WHERE path = ? AND version = ?", (path, version))

    def blob_refs(self, blob_path: str) -> int:
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) FROM file_versions WHERE blob_path = ?",
                (blob_path,)).fetchone()[0]