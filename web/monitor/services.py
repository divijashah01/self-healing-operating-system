"""Dashboard data access: reads the healer's DB and files. The ONLY write path is
queueing commands (EventLog.enqueue_command); the healer does the actual work."""
import threading
import time

from django.conf import settings

from healer.config import load_config
from healer.eventlog import EventLog

_logs = {}
_lock = threading.Lock()


def get_config():
    return load_config(getattr(settings, "HEALER_CONFIG_PATH", None))


def get_log(cfg) -> EventLog:
    """One shared connection per DB path (EventLog is thread-safe)."""
    key = str(cfg.healer.db_path)
    with _lock:
        if key not in _logs:
            _logs[key] = EventLog(cfg.healer.db_path)
        return _logs[key]


def close_all() -> None:
    with _lock:
        for log in _logs.values():
            log.close()
        _logs.clear()


def healer_info(log: EventLog) -> dict:
    last = float(log.get_meta("healer_last_seen") or 0)
    age = time.time() - last if last else None
    return {"running": age is not None and age < 5,
            "last_seen_age": age,
            "pid": log.get_meta("healer_pid")}


def snapshot(log: EventLog, event_limit: int = 40) -> dict:
    commands = [{k: c[k] for k in ("id", "ts", "action", "target", "status", "result")}
                for c in log.recent_commands(8)]
    return {"now": time.time(), "healer": healer_info(log), "services": log.get_states(),
            "events": log.recent(event_limit), "commands": commands}