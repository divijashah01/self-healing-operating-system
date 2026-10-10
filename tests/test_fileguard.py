import tempfile
import unittest
from pathlib import Path

from healer.config import ProtectedFile
from healer.eventlog import EventLog, EventType
from healer.fileguard import FileGuard
from healer.filevault import FileVault

GOOD = "mode=production\n"


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.eventlog = EventLog(root / "t.db")
        self.vault = FileVault(self.eventlog, root / "snap")
        self.target = root / "app.conf"
        self.target.write_text(GOOD)

    def tearDown(self):
        self.eventlog.close()
        self.tmp.cleanup()

    def make(self, **options):
        self.pf = ProtectedFile(path=self.target, **options)
        return FileGuard([self.pf], self.vault, self.eventlog)

    def types(self):
        return [e["event_type"] for e in reversed(self.eventlog.recent(100))]

    def test_first_check_adopts_baseline(self):
        self.make().check()
        self.assertEqual(self.types(), [EventType.FILE_BASELINED])

    def test_deleted_file_is_restored(self):
        guard = self.make()
        guard.check()
        self.target.unlink()
        guard.check()
        self.assertEqual(self.target.read_text(), GOOD)
        self.assertEqual(self.types()[-2:], [EventType.FILE_MISSING, EventType.FILE_RESTORED])

    def test_valid_change_is_modified_and_invalid_is_corrupted(self):
        guard = self.make(validate="keyvalue")
        guard.check()
        self.target.write_text("mode=debug\n")
        guard.check()
        self.target.write_text("garbage without equals\n")
        guard.check()
        types = self.types()
        self.assertIn(EventType.FILE_MODIFIED, types)
        self.assertIn(EventType.FILE_CORRUPTED, types)
        self.assertEqual(self.target.read_text(), GOOD)

    def test_alert_mode_reports_once_and_does_not_restore(self):
        guard = self.make(mode="alert")
        guard.check()
        self.target.write_text("mode=debug\n")
        guard.check()
        guard.check()
        self.assertEqual(self.types().count(EventType.FILE_MODIFIED), 1)
        self.assertEqual(self.target.read_text(), "mode=debug\n")

    def test_file_appearing_late_is_baselined(self):
        self.target.unlink()
        guard = self.make()
        guard.check()                          # nothing to baseline yet; must not raise
        self.assertEqual(self.types(), [])
        self.target.write_text(GOOD)
        guard.check()
        self.assertEqual(self.types(), [EventType.FILE_BASELINED])

    def test_approved_update_is_not_flagged(self):
        guard = self.make()
        guard.check()
        self.vault.update(str(self.pf.path), b"mode=staging\n", 5, note="approved")
        guard.check()
        self.assertEqual(self.target.read_text(), "mode=staging\n")
        self.assertNotIn(EventType.FILE_MODIFIED, self.types())


if __name__ == "__main__":
    unittest.main()