import os
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path

import psutil

from healer.config import ServiceConfig
from healer.eventlog import EventLog, EventType
from healer.supervisor import Status, Supervisor
from tests.helpers import FakeClock

SLEEPER = "import time; time.sleep(60)"


class DependencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.eventlog = EventLog(self.root / "t.db")
        self.clock = FakeClock()
        services = [
            ServiceConfig(name="db", command=(sys.executable, "-c", SLEEPER)),
            ServiceConfig(name="app", command=(sys.executable, "-c", SLEEPER),
                          depends_on=("db",)),
        ]
        self.sup = Supervisor(services, self.eventlog, self.root / "logs",
                              clock=self.clock, dep_settle=2.0)
        self.sup.start_all()
        self.db, self.app = self.sup.services["db"], self.sup.services["app"]

    def tearDown(self):
        self.sup.stop_all(grace=1)
        self.eventlog.close()
        self.tmp.cleanup()

    def wait_until(self, predicate, timeout=5.0):
        end = time.time() + timeout
        while time.time() < end:
            self.sup.check()
            if predicate():
                return
            time.sleep(0.02)
        self.fail("condition not reached in time")

    def settle(self):
        """Let a freshly started dependency count as ready."""
        self.clock.advance(2.1)
        self.sup.check()

    def test_dependent_waits_for_its_dependency_at_boot(self):
        self.assertEqual(self.db.status, Status.RUNNING)
        self.assertEqual(self.app.status, Status.WAITING)
        self.sup.check()
        self.assertEqual(self.app.status, Status.WAITING)       # db has not settled yet
        self.settle()
        self.assertEqual(self.app.status, Status.RUNNING)

    def test_dependency_failure_stops_dependents_without_blaming_them(self):
        self.settle()
        app_pid = self.app.proc.pid
        os.kill(self.db.proc.pid, signal.SIGKILL)
        self.wait_until(lambda: self.db.status != Status.RUNNING)
        self.assertEqual(self.app.status, Status.WAITING)
        self.assertEqual(self.app.consecutive_failures, 0)
        self.assertFalse(psutil.pid_exists(app_pid))
        types = [e["event_type"] for e in self.eventlog.recent(50)]
        self.assertIn(EventType.SERVICE_WAITING, types)
        self.clock.advance(1.5)
        self.sup.check()                                         # db restarts after its backoff
        self.assertEqual(self.db.status, Status.RUNNING)
        self.assertEqual(self.app.status, Status.WAITING)        # db has not settled yet
        self.settle()
        self.assertEqual(self.app.status, Status.RUNNING)
        self.assertEqual(self.app.restarts, 1)

    def test_dependent_failing_alone_does_not_touch_its_dependency(self):
        self.settle()
        os.kill(self.app.proc.pid, signal.SIGKILL)
        self.wait_until(lambda: self.app.status != Status.RUNNING)
        self.assertEqual(self.app.status, Status.RESTARTING)     # the normal backoff path
        self.assertEqual(self.db.status, Status.RUNNING)

    def test_operator_restart_of_a_dependency_cascades(self):
        self.settle()
        self.assertTrue(self.sup.restart("db"))
        self.assertEqual(self.app.status, Status.WAITING)
        self.settle()
        self.assertEqual(self.app.status, Status.RUNNING)

    def test_no_cascade_while_healing_is_off(self):
        self.settle()
        self.sup.set_healing(False)
        os.kill(self.db.proc.pid, signal.SIGKILL)
        self.wait_until(lambda: self.db.status != Status.RUNNING)
        self.assertEqual(self.db.status, Status.CRASHED)
        self.assertEqual(self.app.status, Status.RUNNING)        # nothing is stopped or repaired


if __name__ == "__main__":
    unittest.main()