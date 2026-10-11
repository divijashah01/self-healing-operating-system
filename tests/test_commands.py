import sys
import tempfile
import unittest
from pathlib import Path

from healer.commands import CommandExecutor
from healer.config import Config, HealerSettings, ProtectedFile, ServiceConfig
from healer.eventlog import EventLog
from healer.filevault import FileVault
from healer.supervisor import Status, Supervisor

SLEEPER = "import time; time.sleep(60)"


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.eventlog = EventLog(root / "t.db")
        service = ServiceConfig(name="a", command=(sys.executable, "-c", SLEEPER))
        self.target = root / "app.conf"
        self.target.write_text("mode=production\n")
        protected = ProtectedFile(path=self.target, validate="keyvalue")
        self.sup = Supervisor([service], self.eventlog, root / "logs")
        self.vault = FileVault(self.eventlog, root / "snap")
        settings = HealerSettings(data_dir=root, db_path=root / "t.db", poll_interval=1.0)
        config = Config(healer=settings, services=(service,), protected_files=(protected,))
        self.executor = CommandExecutor(config, self.eventlog, self.sup, self.vault)
        self.sup.start_all()
        self.svc = self.sup.services["a"]

    def tearDown(self):
        self.sup.stop_all(grace=1)
        self.eventlog.close()
        self.tmp.cleanup()

    def run_command(self, action, target, **args):
        command_id = self.eventlog.enqueue_command(action, target, args)
        self.executor.process()
        return self.eventlog.get_command(command_id)

    def test_restart_service_replaces_the_process(self):
        old_pid = self.svc.proc.pid
        self.assertEqual(self.run_command("restart_service", "a")["status"], "done")
        self.assertEqual(self.svc.status, Status.RUNNING)
        self.assertNotEqual(self.svc.proc.pid, old_pid)
        self.assertEqual(self.svc.restarts, 1)

    def test_stop_then_start_service(self):
        self.assertEqual(self.run_command("stop_service", "a")["status"], "done")
        self.assertEqual(self.svc.status, Status.STOPPED)
        self.sup.check()                                   # a stopped service must stay stopped
        self.assertEqual(self.svc.status, Status.STOPPED)
        self.assertEqual(self.run_command("start_service", "a")["status"], "done")
        self.assertEqual(self.svc.status, Status.RUNNING)
        self.assertEqual(self.run_command("start_service", "a")["status"], "failed")

    def test_reset_circuit_requires_quarantine(self):
        cmd = self.run_command("reset_circuit", "a")
        self.assertEqual(cmd["status"], "failed")
        self.assertIn("not quarantined", cmd["result"])

    def test_unknown_action_and_service_fail_cleanly(self):
        self.assertEqual(self.run_command("format_disk", "a")["status"], "failed")
        cmd = self.run_command("restart_service", "nope")
        self.assertEqual(cmd["status"], "failed")
        self.assertIn("unknown service", cmd["result"])

    def test_update_file_applies_valid_content_and_rejects_invalid(self):
        self.vault.adopt(str(self.target), 5)
        ok = self.run_command("update_file", str(self.target), content="mode=staging\r\n")
        self.assertEqual(ok["status"], "done")
        self.assertEqual(self.target.read_text(), "mode=staging\n")       # CRLF normalised
        self.assertEqual(self.eventlog.get_baseline(str(self.target))["version"], 2)
        bad = self.run_command("update_file", str(self.target), content="garbage")
        self.assertEqual(bad["status"], "failed")
        self.assertEqual(self.target.read_text(), "mode=staging\n")

    def test_restore_file_and_unprotected_path(self):
        self.vault.adopt(str(self.target), 5)
        self.run_command("update_file", str(self.target), content="mode=staging\n")
        cmd = self.run_command("restore_file", str(self.target), version=1)
        self.assertEqual(cmd["status"], "done")
        self.assertEqual(self.target.read_text(), "mode=production\n")
        self.assertEqual(self.run_command("restore_file", "/etc/passwd")["status"], "failed")


if __name__ == "__main__":
    unittest.main()