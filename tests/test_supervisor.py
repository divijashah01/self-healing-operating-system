import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import psutil

from healer.config import ServiceConfig
from healer.eventlog import EventLog, EventType
from healer.supervisor import Status, Supervisor

SLEEPER = "import time; time.sleep(60)"


def svc(name, code):
    return ServiceConfig(name=name, command=(sys.executable, "-c", code))


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.eventlog = EventLog(Path(self.tmp.name) / "t.db")
        self.sup = None

    def tearDown(self):
        if self.sup:
            self.sup.stop_all(grace=1)
        self.eventlog.close()
        self.tmp.cleanup()

    def make(self, *services):
        self.sup = Supervisor(list(services), self.eventlog, Path(self.tmp.name) / "logs")
        self.sup.start_all()
        return self.sup

    def wait_until_done(self, name, timeout=5.0):
        end = time.time() + timeout
        while time.time() < end:
            self.sup.check()
            if self.sup.services[name].status != Status.RUNNING:
                return
            time.sleep(0.05)
        self.fail(f"{name} still running after {timeout}s")

    def event_types(self):
        return [e["event_type"] for e in self.eventlog.recent()]

    def test_nonzero_exit_is_crash(self):
        self.make(svc("a", "import sys; sys.exit(3)"))
        self.wait_until_done("a")
        self.assertEqual(self.sup.services["a"].status, Status.CRASHED)
        crash = next(e for e in self.eventlog.recent() if e["event_type"] == EventType.SERVICE_CRASHED)
        self.assertEqual(crash["details"]["exit_code"], 3)

    def test_clean_exit_is_stop_not_crash(self):
        self.make(svc("a", "import sys; sys.exit(0)"))
        self.wait_until_done("a")
        self.assertEqual(self.sup.services["a"].status, Status.STOPPED)
        self.assertNotIn(EventType.SERVICE_CRASHED, self.event_types())

    def test_kill_by_signal_is_crash(self):
        self.make(svc("a", SLEEPER))
        os.kill(self.sup.services["a"].proc.pid, signal.SIGKILL)
        self.wait_until_done("a")
        crash = next(e for e in self.eventlog.recent() if e["event_type"] == EventType.SERVICE_CRASHED)
        self.assertIn("SIGKILL", crash["message"])

    def test_missing_binary_is_crash(self):
        self.sup = Supervisor([ServiceConfig(name="a", command=("/no/such/binary",))],
                              self.eventlog, Path(self.tmp.name) / "logs")
        self.sup.start_all()
        self.assertEqual(self.sup.services["a"].status, Status.CRASHED)

    def test_running_service_reports_metrics(self):
        self.make(svc("a", SLEEPER))
        self.sup.check()
        self.sup.check()
        state = self.eventlog.get_states()[0]
        self.assertEqual(state["status"], Status.RUNNING)
        self.assertIsNotNone(state["pid"])
        self.assertIsNotNone(state["rss_mb"])

    def test_stop_all_terminates_processes(self):
        self.make(svc("a", SLEEPER))
        pid = self.sup.services["a"].proc.pid
        self.sup.stop_all(grace=2)
        self.assertEqual(self.sup.services["a"].status, Status.STOPPED)
        self.assertFalse(psutil.pid_exists(pid))

    def test_zombie_child_is_reaped(self):
        self.make(svc("a", SLEEPER))
        stray = subprocess.Popen([sys.executable, "-c", "pass"])  # exits -> zombie until waited
        time.sleep(0.5)
        self.sup.check()
        self.assertIn(EventType.ZOMBIE_REAPED, self.event_types())
        stray.poll()


if __name__ == "__main__":
    unittest.main()