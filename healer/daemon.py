"""The healer daemon: start services, then monitor them until told to stop."""
import logging
import os
import signal
import threading

from .config import Config
from .eventlog import EventLog, EventType
from .supervisor import Supervisor

log = logging.getLogger("healer.daemon")


class Healer:
    def __init__(self, config: Config):
        self.config = config
        self.eventlog = EventLog(config.healer.db_path)
        self.supervisor = Supervisor(config.services, self.eventlog,
                                     config.healer.data_dir / "logs",
                                     heartbeat_dir=config.healer.data_dir / "heartbeats",)
        self._stop = threading.Event()

    def request_stop(self, *_args) -> None:
        self._stop.set()

    def run(self) -> None:
        signal.signal(signal.SIGINT, self.request_stop)
        signal.signal(signal.SIGTERM, self.request_stop)
        self.eventlog.log(EventType.HEALER_STARTED, message="healer started",
                          pid=os.getpid(), services=[s.name for s in self.config.services])
        log.info("healer started (pid %d), supervising %d services",
                 os.getpid(), len(self.config.services))
        self.supervisor.start_all()
        try:
            while not self._stop.is_set():
                self.supervisor.check()
                self._stop.wait(self.config.healer.poll_interval)
        finally:
            log.info("shutting down, stopping services")
            self.supervisor.stop_all()
            self.eventlog.log(EventType.HEALER_STOPPED, message="healer stopped")
            self.eventlog.close()