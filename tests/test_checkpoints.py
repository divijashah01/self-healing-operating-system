import json
import tempfile
import unittest
from pathlib import Path

from healer.checkpoints import CheckpointManager, validate
from healer.eventlog import EventLog, EventType
from tests.helpers import make_checkpoints


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "ckpt"
        self.eventlog = EventLog(Path(self.tmp.name) / "t.db")
        self.mgr = CheckpointManager(self.eventlog, self.root)

    def tearDown(self):
        self.eventlog.close()
        self.tmp.cleanup()

    def types(self):
        return [e["event_type"] for e in self.eventlog.recent(50)]

    def test_service_written_checkpoint_validates(self):
        (path,) = make_checkpoints(self.root, "w", [{"n": 1}])
        self.assertEqual(validate(path, "w").seq, 1)

    def test_pick_returns_newest_valid(self):
        make_checkpoints(self.root, "w", [{"n": 1}, {"n": 2}, {"n": 3}])
        self.assertEqual(self.mgr.pick("w").seq, 3)

    def test_corrupt_newest_is_rejected_and_previous_used(self):
        paths = make_checkpoints(self.root, "w", [{"n": 1}, {"n": 2}, {"n": 3}])
        paths[-1].write_text("")                       # truncated file
        self.assertEqual(self.mgr.pick("w").seq, 2)
        self.assertFalse(paths[-1].exists())
        self.assertTrue(paths[-1].with_name(paths[-1].name + ".bad").exists())
        self.assertIn(EventType.CHECKPOINT_REJECTED, self.types())

    def test_tampered_state_fails_checksum(self):
        (path,) = make_checkpoints(self.root, "w", [{"n": 1}])
        doc = json.loads(path.read_text())
        doc["state"]["n"] = 999                        # edited without fixing the checksum
        path.write_text(json.dumps(doc))
        self.assertIsNone(self.mgr.pick("w"))
        self.assertIn(EventType.CHECKPOINT_REJECTED, self.types())

    def test_checkpoint_of_another_service_is_rejected(self):
        (path,) = make_checkpoints(self.root, "other", [{"n": 1}])
        with self.assertRaises(ValueError):
            validate(path, "w")

    def test_no_checkpoints_means_none(self):
        self.assertIsNone(self.mgr.pick("w"))

    def test_pruning_keeps_last_n_and_numbering_resumes(self):
        make_checkpoints(self.root, "w", [{"n": i} for i in range(5)], keep=2)
        names = sorted(p.name for p in (self.root / "w").glob("ckpt-*.json"))
        self.assertEqual(names, ["ckpt-00000004.json", "ckpt-00000005.json"])
        (new,) = make_checkpoints(self.root, "w", [{"n": 9}], keep=2)
        self.assertEqual(new.name, "ckpt-00000006.json")


if __name__ == "__main__":
    unittest.main()