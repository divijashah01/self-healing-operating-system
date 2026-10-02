import sys
import tempfile
import time
import unittest
from pathlib import Path

from healer.config import RestartPolicy, ServiceConfig
from healer.eventlog import EventLog, EventType
from healer.supervisor import Status, Supervisor

CRASH = "import sys; sys.exit(1)"
CLEAN = "import sys; sys.exit(0)"
SLEEPER = "import time; time.sleep(60)"


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class RestartTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.eventlog = EventLog(Path(self.tmp.name) / "t.db")
        self.clock = FakeClock()
        self.sup = None

    def tearDown(self):
        if self.sup:
            self.sup.stop_all(grace=1)
        self.eventlog.close()
        self.tmp.cleanup()

    def make(self, code, **overrides):
        opts = dict(policy="on-failure", backoff_base=1.0, backoff_max=30.0,
                    max_failures=10, window=1000.0, stable_after=1000.0, cooldown=60.0)
        opts.update(overrides)
        cfg = ServiceConfig(name="a", command=(sys.executable, "-c", code),
                            restart=RestartPolicy(**opts))
        self.sup = Supervisor([cfg], self.eventlog, Path(self.tmp.name) / "logs",
                              clock=self.clock)
        self.sup.start_all()
        return self.sup.services["a"]

    def wait_exit(self, svc, timeout=5.0):
        """Poll (real time) until the running process has exited and been handled."""
        end = time.time() + timeout
        while time.time() < end:
            self.sup.check()
            if svc.status != Status.RUNNING:
                return
            time.sleep(0.02)
        self.fail("process did not exit")

    def events(self, event_type):
        return [e for e in reversed(self.eventlog.recent(200)) if e["event_type"] == event_type]

    def test_crash_is_restarted_after_backoff(self):
        svc = self.make(CRASH)
        first_pid = svc.proc.pid
        self.wait_exit(svc)
        self.assertEqual(svc.status, Status.RESTARTING)
        self.sup.check()                              # 1s backoff not elapsed yet
        self.assertEqual(svc.status, Status.RESTARTING)
        self.clock.advance(1.1)
        self.sup.check()
        self.assertEqual(svc.status, Status.RUNNING)
        self.assertEqual(svc.restarts, 1)
        self.assertNotEqual(svc.proc.pid, first_pid)
        self.assertEqual(len(self.events(EventType.SERVICE_RESTARTED)), 1)

    def test_backoff_doubles(self):
        svc = self.make(CRASH)
        for _ in range(3):
            self.wait_exit(svc)
            self.clock.advance(100)
            self.sup.check()
        delays = [e["details"]["delay_s"] for e in self.events(EventType.RESTART_SCHEDULED)]
        self.assertEqual(delays, [1.0, 2.0, 4.0])

    def test_circuit_opens_then_probe_then_reopens(self):
        svc = self.make(CRASH, max_failures=3)
        for _ in range(2):                            # failures 1 and 2 -> restarted
            self.wait_exit(svc)
            self.clock.advance(10)
            self.sup.check()
        self.wait_exit(svc)                           # failure 3 -> circuit opens
        self.assertEqual(svc.status, Status.QUARANTINED)
        self.assertEqual(len(self.events(EventType.CIRCUIT_OPEN)), 1)
        self.clock.advance(30)                        # cooldown (60s) not over
        self.sup.check()
        self.assertEqual(svc.status, Status.QUARANTINED)
        self.clock.advance(31)                        # cooldown over -> probe restart
        self.sup.check()
        self.assertEqual(svc.status, Status.RUNNING)
        self.assertTrue(svc.probation)
        self.wait_exit(svc)                           # probe crashes -> reopen immediately
        self.assertEqual(svc.status, Status.QUARANTINED)
        self.assertEqual(len(self.events(EventType.CIRCUIT_OPEN)), 2)

    def test_manual_reset_restarts_quarantined_service(self):
        svc = self.make(CRASH, max_failures=1)
        self.wait_exit(svc)
        self.assertEqual(svc.status, Status.QUARANTINED)
        self.assertTrue(self.sup.reset_circuit("a"))
        self.sup.check()
        self.assertEqual(svc.status, Status.RUNNING)
        self.assertFalse(svc.probation)
        self.assertFalse(self.sup.reset_circuit("a"))  # not quarantined any more

    def test_clean_exit_not_restarted_by_default(self):
        svc = self.make(CLEAN)
        self.wait_exit(svc)
        self.assertEqual(svc.status, Status.STOPPED)
        self.clock.advance(100)
        self.sup.check()
        self.assertEqual(svc.status, Status.STOPPED)

    def test_always_policy_restarts_clean_exit(self):
        svc = self.make(CLEAN, policy="always")
        self.wait_exit(svc)
        self.assertEqual(svc.status, Status.RESTARTING)

    def test_stable_run_resets_failure_counters(self):
        svc = self.make(SLEEPER, stable_after=5.0)
        svc.consecutive_failures = 2
        svc.failure_times = [self.clock()]
        self.clock.advance(6)
        self.sup.check()
        self.assertEqual(svc.consecutive_failures, 0)
        self.assertEqual(svc.failure_times, [])


if __name__ == "__main__":
    unittest.main()