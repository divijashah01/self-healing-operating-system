"""Process supervision: launch services, detect exits, sample resources, reap zombies."""
import logging
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import psutil

from .config import PROJECT_ROOT, ServiceConfig
from .eventlog import EventLog, EventType, Severity

log = logging.getLogger("healer.supervisor")

_LOG_LEVELS = {
    Severity.INFO: logging.INFO,
    Severity.WARNING: logging.WARNING,
    Severity.ERROR: logging.ERROR,
    Severity.CRITICAL: logging.CRITICAL,
}


class Status:
    RUNNING = "running"
    STOPPED = "stopped"   # exited cleanly (code 0) or stopped by the healer
    CRASHED = "crashed"   # non-zero exit, killed by a signal, or failed to start


def classify_exit(returncode: int):
    """Return (status, human-readable reason) for a finished process."""
    if returncode == 0:
        return Status.STOPPED, "exited cleanly (code 0)"
    if returncode < 0:
        try:
            name = signal.Signals(-returncode).name
        except ValueError:
            name = str(-returncode)
        return Status.CRASHED, f"killed by signal {name}"
    return Status.CRASHED, f"exited with code {returncode}"


def tail(path: Path, lines: int = 5, max_bytes: int = 4096) -> list:
    """Last few lines of a log file (used as crash diagnostics)."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - max_bytes))
            data = fh.read().decode(errors="replace")
    except OSError:
        return []
    return data.splitlines()[-lines:]


@dataclass
class ManagedService:
    config: ServiceConfig
    proc: Optional[subprocess.Popen] = None
    ps: Optional[psutil.Process] = None
    status: str = Status.STOPPED
    restarts: int = 0
    started_at: Optional[float] = None


class Supervisor:
    def __init__(self, services, eventlog: EventLog, logs_dir):
        self.eventlog = eventlog
        self.logs_dir = Path(logs_dir)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.services = {s.name: ManagedService(s) for s in services}
        self._reported_zombies = set()

    # ---- helpers -------------------------------------------------------
    def _event(self, event_type, service, severity, message, **details):
        log.log(_LOG_LEVELS[severity], "[%s] %s", service or "healer", message)
        self.eventlog.log(event_type, service=service, severity=severity,
                          message=message, **details)

    def _signal(self, svc: ManagedService, sig: int) -> None:
        try:
            os.killpg(svc.proc.pid, sig)   # service is its own process-group leader
        except ProcessLookupError:
            pass

    # ---- lifecycle -----------------------------------------------------
    def start_all(self) -> None:
        for svc in self.services.values():
            self.start(svc)

    def start(self, svc: ManagedService) -> bool:
        name = svc.config.name
        log_path = self.logs_dir / f"{name}.log"
        try:
            with open(log_path, "ab") as out:
                svc.proc = subprocess.Popen(
                    list(svc.config.command), cwd=PROJECT_ROOT,
                    env={**os.environ, **svc.config.env},
                    stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
        except OSError as exc:
            svc.proc = svc.ps = None
            svc.status = Status.CRASHED
            self._event(EventType.SERVICE_CRASHED, name, Severity.ERROR,
                        f"failed to start: {exc}")
            self.eventlog.set_state(name, svc.status, restarts=svc.restarts)
            return False
        svc.ps = psutil.Process(svc.proc.pid)
        svc.ps.cpu_percent(None)            # prime the CPU counter
        svc.status = Status.RUNNING
        svc.started_at = time.time()
        self.eventlog.set_state(name, svc.status, pid=svc.proc.pid, restarts=svc.restarts)
        self._event(EventType.SERVICE_STARTED, name, Severity.INFO,
                    f"started (pid {svc.proc.pid})", pid=svc.proc.pid)
        return True

    def stop_all(self, grace: float = 5.0) -> None:
        live = [s for s in self.services.values() if s.proc is not None]
        for svc in live:
            self._signal(svc, signal.SIGTERM)
        deadline = time.time() + grace
        for svc in live:
            try:
                svc.proc.wait(timeout=max(0.0, deadline - time.time()))
            except subprocess.TimeoutExpired:
                self._signal(svc, signal.SIGKILL)
                svc.proc.wait()
            name, pid = svc.config.name, svc.proc.pid
            svc.status, svc.proc, svc.ps = Status.STOPPED, None, None
            self._event(EventType.SERVICE_STOPPED, name, Severity.INFO,
                        "stopped by healer", pid=pid)
            self.eventlog.set_state(name, Status.STOPPED, restarts=svc.restarts)

    # ---- monitoring ----------------------------------------------------
    def check(self) -> None:
        """One monitoring pass; call this every poll interval."""
        for svc in self.services.values():
            if svc.proc is None:
                continue
            returncode = svc.proc.poll()    # also reaps the child
            if returncode is None:
                self._sample(svc)
            else:
                self._handle_exit(svc, returncode)
        self._reap_zombies()

    def _sample(self, svc: ManagedService) -> None:
        try:
            cpu = svc.ps.cpu_percent(interval=None)
            rss_mb = svc.ps.memory_info().rss / (1024 * 1024)
        except psutil.NoSuchProcess:
            return
        self.eventlog.set_state(svc.config.name, Status.RUNNING, pid=svc.proc.pid,
                                restarts=svc.restarts, cpu_percent=cpu,
                                rss_mb=round(rss_mb, 1))

    def _handle_exit(self, svc: ManagedService, returncode: int) -> None:
        name, pid = svc.config.name, svc.proc.pid
        status, reason = classify_exit(returncode)
        uptime = round(time.time() - svc.started_at, 1) if svc.started_at else None
        svc.status, svc.proc, svc.ps = status, None, None
        if status == Status.CRASHED:
            self._event(EventType.SERVICE_CRASHED, name, Severity.ERROR, reason,
                        pid=pid, exit_code=returncode, uptime_s=uptime,
                        last_output=tail(self.logs_dir / f"{name}.log"))
        else:
            self._event(EventType.SERVICE_STOPPED, name, Severity.INFO, reason,
                        pid=pid, exit_code=returncode, uptime_s=uptime)
        self.eventlog.set_state(name, status, restarts=svc.restarts)

    def _reap_zombies(self) -> None:
        managed = {s.proc.pid for s in self.services.values() if s.proc}
        me = os.getpid()
        for child in psutil.Process(me).children(recursive=True):
            if child.pid in managed or child.pid in self._reported_zombies:
                continue
            try:
                if child.status() != psutil.STATUS_ZOMBIE:
                    continue
                ppid = child.ppid()
            except psutil.NoSuchProcess:
                continue
            if ppid == me:                  # our own un-waited child: we can reap it
                try:
                    os.waitpid(child.pid, os.WNOHANG)
                except ChildProcessError:
                    pass
                self._event(EventType.ZOMBIE_REAPED, None, Severity.WARNING,
                            f"reaped zombie child pid {child.pid}", pid=child.pid)
            else:                           # a service's child: only its parent can reap it
                self._reported_zombies.add(child.pid)
                self._event(EventType.ZOMBIE_DETECTED, None, Severity.WARNING,
                            f"zombie pid {child.pid} (parent {ppid}) awaiting its parent",
                            pid=child.pid, ppid=ppid)