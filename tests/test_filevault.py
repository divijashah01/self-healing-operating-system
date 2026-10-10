import os
import tempfile
import unittest
from pathlib import Path

from healer.eventlog import EventLog, EventType
from healer.filevault import FileState, FileVault


class VaultTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.snap = root / "snap"
        self.eventlog = EventLog(root / "t.db")
        self.vault = FileVault(self.eventlog, self.snap)
        self.target = root / "app.conf"
        self.target.write_text("a=1\n")
        self.key = str(self.target)

    def tearDown(self):
        self.eventlog.close()
        self.tmp.cleanup()

    def test_adopt_records_baseline(self):
        self.assertEqual(self.vault.adopt(self.key, keep=5), 1)
        self.assertEqual(self.vault.verify(self.key).state, FileState.OK)
        types = [e["event_type"] for e in self.eventlog.recent()]
        self.assertIn(EventType.FILE_BASELINED, types)

    def test_modified_file_is_detected_and_restored(self):
        self.vault.adopt(self.key, 5)
        self.target.write_text("a=2\n")
        self.assertEqual(self.vault.verify(self.key).state, FileState.MODIFIED)
        self.vault.restore(self.key)
        self.assertEqual(self.target.read_text(), "a=1\n")
        self.assertEqual(self.vault.verify(self.key).state, FileState.OK)

    def test_missing_file_is_detected_and_restored(self):
        self.vault.adopt(self.key, 5)
        os.unlink(self.target)
        self.assertEqual(self.vault.verify(self.key).state, FileState.MISSING)
        self.vault.restore(self.key)
        self.assertEqual(self.target.read_text(), "a=1\n")

    def test_update_moves_baseline_and_old_version_is_restorable(self):
        self.vault.adopt(self.key, 5)
        self.assertEqual(self.vault.update(self.key, b"a=2\n", 5, note="new"), 2)
        self.assertEqual(self.target.read_text(), "a=2\n")
        self.assertEqual(self.vault.verify(self.key).state, FileState.OK)
        self.vault.restore(self.key, version=1)
        self.assertEqual(self.target.read_text(), "a=1\n")
        self.assertEqual(self.eventlog.get_baseline(self.key)["version"], 1)

    def test_prune_keeps_newest_versions_and_deletes_blobs(self):
        self.vault.adopt(self.key, keep=2)
        for i in range(2, 6):
            self.vault.update(self.key, f"a={i}\n".encode(), keep=2)
        versions = self.eventlog.file_versions(self.key)
        self.assertEqual([v["version"] for v in versions], [5, 4])
        self.assertEqual(len(list(self.snap.iterdir())), 2)

    def test_identical_content_shares_one_blob(self):
        self.vault.adopt(self.key, 5)
        self.vault.update(self.key, b"a=1\n", 5)
        self.assertEqual(len(self.eventlog.file_versions(self.key)), 2)
        self.assertEqual(len(list(self.snap.iterdir())), 1)

    def test_corrupted_snapshot_is_not_restored(self):
        self.vault.adopt(self.key, 5)
        Path(self.eventlog.get_baseline(self.key)["blob_path"]).write_bytes(b"garbage")
        self.target.write_text("a=2\n")
        with self.assertRaises(ValueError):
            self.vault.restore(self.key)
        self.assertEqual(self.target.read_text(), "a=2\n")


if __name__ == "__main__":
    unittest.main()