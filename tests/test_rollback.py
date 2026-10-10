import os
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path

from healer.config import CheckpointPolicy, RestartPolicy, ServiceConfig
from healer.eventlog import EventLog, EventType
from healer.supervisor import Status, Supervisor
from tests.helpers import FakeClock, make_checkpoints

# exits 0 only if the supervisor pointed it at checkpoint #2
WANTS_SEQ2 = ("import os, sys; "
              "sys.exit(0 if os.environ.get('HEALER_RESTORE_FROM', '').endswith('00000002.json') else 1)")
# "crashes" when restored from the poisoned checkpoint #2, fine otherwise
CRASH_IF_SEQ2 = ("import os, sys; "
                 "sys.exit(3 if '00000002' in os.environ.get('HEALER_RESTORE_FROM', '') else 0)")


class RollbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ckpt_root = self.root / "ckpt"
        self.eventlog = EventLog(self.root / "t.db")
        self.clock = FakeClock()
        self.sup = None

    def tearDown(self):
        if self.sup:
            self.sup.stop_all(grace=1)
        self.eventlog.close()
        self.tmp.cleanup()

    def make(self, code, restart=None):
        cfg = ServiceConfig(name="a", command=(sys.executable, "-c", code),
                            restart=restart or RestartPolicy(policy="never"),
                            checkpoint=CheckpointPolicy(keep=5, trust_after=10.0))
        self.sup = Supervisor([cfg], self.eventlog, self.root / "logs", clock=self.clock,
                              checkpoint_dir=self.ckpt_root)
        self.sup.start_all()
        return self.sup.services["a"]

    def wait_exit(self, svc, timeout=5.0):
        end = time.time() + timeout
        while time.time() < end:
            self.sup.check()
            if svc.status != Status.RUNNING:
                return
            time.sleep(0.02)
        self.fail("process did not exit")

    def test_newest_valid_checkpoint_is_passed_to_service(self):
        make_checkpoints(self.ckpt_root, "a", [{"n": 1}, {"n": 2}])
        svc = self.make(WANTS_SEQ2)
        self.wait_exit(svc)
        self.assertEqual(svc.status, Status.STOPPED)    # exit 0 => it received checkpoint #2
        types = [e["event_type"] for e in self.eventlog.recent(50)]
        self.assertIn(EventType.CHECKPOINT_RESTORED, types)

    def test_crash_right_after_restore_rolls_back_to_previous_checkpoint(self):
        paths = make_checkpoints(self.ckpt_root, "a", [{"n": 1}, {"n": 2}])
        svc = self.make(CRASH_IF_SEQ2, restart=RestartPolicy())
        self.wait_exit(svc)                              # restored from #2 -> crashes
        self.assertEqual(svc.status, Status.RESTARTING)
        self.assertTrue(paths[1].with_name(paths[1].name + ".bad").exists())
        self.clock.advance(1.5)
        self.sup.check()                                 # restarts, now restoring from #1
        self.wait_exit(svc)
        self.assertEqual(svc.status, Status.STOPPED)     # exit 0 => it received checkpoint #1
        restored = [e for e in self.eventlog.recent(50)
                    if e["event_type"] == EventType.CHECKPOINT_RESTORED]
        self.assertEqual([e["details"]["seq"] for e in reversed(restored)], [2, 1])

    def test_checkpoint_is_not_blamed_for_a_crash_after_the_trust_period(self):
        (path,) = make_checkpoints(self.ckpt_root, "a", [{"n": 1}])
        svc = self.make("import time; time.sleep(60)")
        self.clock.advance(20)                           # ran well past trust_after (10s)
        os.kill(svc.proc.pid, signal.SIGKILL)
        self.wait_exit(svc)
        self.assertEqual(svc.status, Status.CRASHED)
        self.assertTrue(path.exists())                   # not set aside


if __name__ == "__main__":
    unittest.main()