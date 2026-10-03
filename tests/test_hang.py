import os
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path

from healer.config import HeartbeatPolicy, RestartPolicy, ServiceConfig
from healer.eventlog import EventLog, EventType
from healer.supervisor import Status, Supervisor

NO_RESTART = RestartPolicy(policy="never")
SILENT = "import time; time.sleep(60)"            # alive, but never sends heartbeats
IGNORES_TERM = ("import signal, time; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)")
BEATER = ("import os, pathlib, time\n"
          "p = pathlib.Path(os.environ['HEALER_HEARTBEAT_FILE'])\n"
          "while True:\n"
          "    p.touch()\n"
          "    time.sleep(0.1)\n")


class HangTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.eventlog = EventLog(Path(self.tmp.name) / "t.db")
        self.sup = None

    def tearDown(self):
        if self.sup:
            self.sup.stop_all(grace=1)
        self.eventlog.close()
        self.tmp.cleanup()

    def make(self, code, restart=NO_RESTART, **hb):
        opts = dict(timeout=0.5, startup_grace=0.2, term_grace=1.0)
        opts.update(hb)
        cfg = ServiceConfig(name="a", command=(sys.executable, "-c", code),
                            restart=restart, heartbeat=HeartbeatPolicy(**opts))
        self.sup = Supervisor([cfg], self.eventlog, Path(self.tmp.name) / "logs")
        self.sup.start_all()
        return self.sup.services["a"]

    def run_until(self, done, timeout=10.0):
        end = time.time() + timeout
        while time.time() < end:
            self.sup.check()
            if done():
                return
            time.sleep(0.05)
        self.fail("condition not reached in time")

    def events(self, event_type):
        return [e for e in reversed(self.eventlog.recent(200)) if e["event_type"] == event_type]

    def test_silent_service_is_detected_and_terminated(self):
        svc = self.make(SILENT)
        self.run_until(lambda: svc.status != Status.RUNNING)
        self.assertEqual(svc.status, Status.CRASHED)
        self.assertEqual(len(self.events(EventType.SERVICE_HUNG)), 1)
        self.assertIn("hang", self.events(EventType.SERVICE_CRASHED)[0]["message"])

    def test_beating_service_is_left_alone(self):
        svc = self.make(BEATER)
        end = time.time() + 1.5
        while time.time() < end:
            self.sup.check()
            time.sleep(0.05)
        self.assertEqual(svc.status, Status.RUNNING)
        self.assertEqual(self.events(EventType.SERVICE_HUNG), [])

    def test_ignored_sigterm_escalates_to_sigkill(self):
        svc = self.make(IGNORES_TERM, term_grace=0.5)
        self.run_until(lambda: svc.status != Status.RUNNING)
        self.assertEqual(len(self.events(EventType.HANG_ESCALATED)), 1)
        self.assertIn("SIGKILL", self.events(EventType.SERVICE_CRASHED)[0]["message"])

    def test_stopped_process_is_detected_and_killed(self):
        svc = self.make(BEATER)
        time.sleep(0.4)
        os.kill(svc.proc.pid, signal.SIGSTOP)          # frozen, but PID still exists
        self.run_until(lambda: svc.status != Status.RUNNING)
        hung = self.events(EventType.SERVICE_HUNG)[0]
        self.assertEqual(hung["details"]["proc_status"], "stopped")
        self.assertEqual(svc.status, Status.CRASHED)

    def test_hung_service_is_restarted_by_policy(self):
        svc = self.make(SILENT, restart=RestartPolicy())   # default policy: on-failure
        self.run_until(lambda: svc.status != Status.RUNNING)
        self.assertEqual(svc.status, Status.RESTARTING)


if __name__ == "__main__":
    unittest.main()