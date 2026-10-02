import tempfile
import unittest
from pathlib import Path

from healer.eventlog import EventLog, EventType, Severity


class EventLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = EventLog(Path(self.tmp.name) / "sub" / "test.db")

    def tearDown(self):
        self.log.close()
        self.tmp.cleanup()

    def test_log_and_read_back(self):
        eid = self.log.log(EventType.SERVICE_STARTED, service="web",
                           message="up", pid=123)
        rows = self.log.recent()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], eid)
        self.assertEqual(rows[0]["details"], {"pid": 123})
        self.assertEqual(rows[0]["severity"], Severity.INFO)

    def test_recent_is_newest_first_and_filterable(self):
        self.log.log(EventType.SERVICE_STARTED, service="web")
        self.log.log(EventType.SERVICE_CRASHED, service="worker",
                     severity=Severity.ERROR)
        self.log.log(EventType.SERVICE_RESTARTED, service="worker")
        all_rows = self.log.recent()
        self.assertEqual(all_rows[0]["event_type"], EventType.SERVICE_RESTARTED)
        worker_rows = self.log.recent(service="worker")
        self.assertEqual(len(worker_rows), 2)

    def test_state_upsert(self):
        self.log.set_state("web", "running", pid=10)
        self.log.set_state("web", "restarting", pid=None, restarts=1)
        states = self.log.get_states()
        self.assertEqual(len(states), 1)
        self.assertEqual(states[0]["status"], "restarting")
        self.assertEqual(states[0]["restarts"], 1)


if __name__ == "__main__":
    unittest.main()