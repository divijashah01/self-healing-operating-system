import sys
import tempfile
import time
import unittest
from pathlib import Path

import psutil

from healer.commands import CommandExecutor
from healer.config import Config, HealerSettings, ProtectedFile, ServiceConfig
from healer.eventlog import EventLog, EventType
from healer.fileguard import VALIDATORS, FileGuard
from healer.filevault import FileState, FileVault
from healer.supervisor import Status, Supervisor
from tests.helpers import FakeClock

SLEEPER = "import time; time.sleep(60)"
HOG = "import time; x = b'x' * (60 * 1024 * 1024); time.sleep(60)"


class ChaosTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.eventlog = EventLog(self.root / "t.db")
        self.clock = FakeClock()
        self.target = self.root / "app.conf"
        self.target.write_text("mode=production\n")
        self.pf = ProtectedFile(path=self.target, validate="keyvalue")
        self.vault = FileVault(self.eventlog, self.root / "snap")
        self.sup = None

    def tearDown(self):
        if self.sup:
            self.sup.stop_all(grace=1)
        self.eventlog.close()
        self.tmp.cleanup()

    def build(self, code=SLEEPER, memory_limit_mb=None):
        service = ServiceConfig(name="a", command=(sys.executable, "-c", code),
                                memory_limit_mb=memory_limit_mb)
        self.sup = Supervisor([service], self.eventlog, self.root / "logs", clock=self.clock)
        self.guard = FileGuard([self.pf], self.vault, self.eventlog)
        settings = HealerSettings(data_dir=self.root, db_path=self.root / "t.db", poll_interval=1.0)
        config = Config(healer=settings, services=(service,), protected_files=(self.pf,))
        self.executor = CommandExecutor(config, self.eventlog, self.sup, self.vault,
                                        guard=self.guard)
        self.sup.start_all()
        self.svc = self.sup.services["a"]

    def command(self, action, target, **args):
        command_id = self.eventlog.enqueue_command(action, target, args)
        self.executor.process()
        return self.eventlog.get_command(command_id)

    def wait_until(self, predicate, timeout=5.0):
        end = time.time() + timeout
        while time.time() < end:
            self.sup.check()
            if predicate():
                return
            time.sleep(0.02)
        self.fail("condition not reached in time")

    def test_chaos_kill_is_healed_when_healing_is_on(self):
        self.build()
        self.assertEqual(self.command("chaos_kill", "a")["status"], "done")
        self.wait_until(lambda: self.svc.status != Status.RUNNING)
        self.assertEqual(self.svc.status, Status.RESTARTING)
        self.clock.advance(1.5)
        self.sup.check()
        self.assertEqual(self.svc.status, Status.RUNNING)

    def test_healing_off_detects_but_does_not_restart_and_on_revives(self):
        self.build()
        self.command("set_healing", "off")
        self.command("chaos_kill", "a")
        self.wait_until(lambda: self.svc.status != Status.RUNNING)
        self.assertEqual(self.svc.status, Status.CRASHED)     # detected and logged ...
        self.clock.advance(100)
        self.sup.check()
        self.assertEqual(self.svc.status, Status.CRASHED)     # ... but never repaired
        self.command("set_healing", "on")
        self.assertEqual(self.svc.status, Status.RESTARTING)
        self.clock.advance(1.5)
        self.sup.check()
        self.assertEqual(self.svc.status, Status.RUNNING)
        self.assertEqual(self.eventlog.get_meta("healing"), "on")

    def test_chaos_freeze_stops_the_process(self):
        self.build()
        pid = self.svc.proc.pid
        self.assertEqual(self.command("chaos_freeze", "a")["status"], "done")
        time.sleep(0.2)
        self.assertEqual(psutil.Process(pid).status(), psutil.STATUS_STOPPED)

    def test_chaos_file_modes(self):
        self.build()
        key = str(self.target)
        self.vault.adopt(key, 5)
        self.command("chaos_file", key, how="tamper")
        check = self.vault.verify(key)
        self.assertEqual(check.state, FileState.MODIFIED)
        self.assertTrue(VALIDATORS["keyvalue"](check.data))     # tampered, but still valid
        self.vault.restore(key)
        self.command("chaos_file", key, how="corrupt")
        self.assertFalse(VALIDATORS["keyvalue"](self.vault.verify(key).data))
        self.vault.restore(key)
        self.command("chaos_file", key, how="delete")
        self.assertEqual(self.vault.verify(key).state, FileState.MISSING)
        self.assertEqual(self.command("chaos_file", key, how="explode")["status"], "failed")

    def test_leak_flag_is_set_and_cleared_on_restart(self):
        self.build()
        self.assertEqual(self.command("chaos_leak", "a")["status"], "done")
        flag = self.root / "chaos" / "a" / "leak"
        self.assertTrue(flag.exists())
        self.command("restart_service", "a")
        self.assertFalse(flag.exists())

    def test_memory_limit_restarts_a_hog(self):
        self.build(code=HOG, memory_limit_mb=30)
        self.wait_until(lambda: self.svc.status != Status.RUNNING, timeout=8)
        self.assertEqual(self.svc.status, Status.RESTARTING)
        types = [e["event_type"] for e in self.eventlog.recent(50)]
        self.assertIn(EventType.RESOURCE_LIMIT, types)

    def test_guard_respects_the_healing_switch(self):
        self.build()
        self.guard.check()                                      # records the baseline
        self.command("set_healing", "off")
        self.target.write_text("mode=debug\n")
        self.guard.check()
        self.assertEqual(self.target.read_text(), "mode=debug\n")   # reported, not repaired
        self.command("set_healing", "on")
        self.guard.check()
        self.assertEqual(self.target.read_text(), "mode=production\n")

    def test_commands_validate_their_input(self):
        self.build()
        self.assertEqual(self.command("set_healing", "maybe")["status"], "failed")
        self.assertEqual(self.command("chaos_kill", "nope")["status"], "failed")
        self.assertEqual(self.command("chaos_file", "/etc/passwd", how="delete")["status"], "failed")
        self.sup.stop("a")
        self.assertEqual(self.command("chaos_kill", "a")["status"], "failed")   # not running


if __name__ == "__main__":
    unittest.main()