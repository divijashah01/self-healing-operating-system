"""The healer daemon: start services, then monitor them until told to stop."""
import logging
import os
import signal
import threading
import time

from .commands import CommandExecutor
from .config import Config
from .eventlog import EventLog, EventType
from .fileguard import FileGuard
from .filevault import FileVault
from .supervisor import Supervisor

log = logging.getLogger("healer.daemon")


class Healer:
    def __init__(self, config: Config):
        self.config = config
        self.eventlog = EventLog(config.healer.db_path)
        self.supervisor = Supervisor(config.services, self.eventlog,
                                     config.healer.data_dir / "logs",
                                     heartbeat_dir=config.healer.data_dir / "heartbeats",
                                     checkpoint_dir=config.healer.data_dir / "checkpoints")
        self.vault = FileVault(self.eventlog, config.healer.data_dir / "snapshots")
        self.guard = FileGuard(config.protected_files, self.vault, self.eventlog)
        self.commands = CommandExecutor(config, self.eventlog, self.supervisor, self.vault)
        self._stop = threading.Event()

    def request_stop(self, *_args) -> None:
        self._stop.set()

    @staticmethod
    def _safe(fn, what: str) -> None:
        try:
            fn()
        except Exception:      # the healer must never die from a bug in one check
            log.exception("%s failed", what)

    def _beat(self) -> None:
        self.eventlog.set_meta("healer_last_seen", str(time.time()))

    def run(self) -> None:
        signal.signal(signal.SIGINT, self.request_stop)
        signal.signal(signal.SIGTERM, self.request_stop)
        self.eventlog.set_meta("healer_pid", str(os.getpid()))
        self.eventlog.log(EventType.HEALER_STARTED, message="healer started",
                          pid=os.getpid(), services=[s.name for s in self.config.services])
        log.info("healer started (pid %d), supervising %d services",
                 os.getpid(), len(self.config.services))
        self.supervisor.start_all()
        try:
            while not self._stop.is_set():
                self._safe(self.commands.process, "command processing")
                self._safe(self.supervisor.check, "supervisor check")
                self._safe(self.guard.check, "file guard check")
                self._safe(self._beat, "liveness update")
                self._stop.wait(self.config.healer.poll_interval)
        finally:
            log.info("shutting down, stopping services")
            self.supervisor.stop_all()
            self.eventlog.log(EventType.HEALER_STOPPED, message="healer stopped")
            self.eventlog.set_meta("healer_last_seen", "0")     # dashboard shows "not running" at once
            self.eventlog.close()