import unittest

from healer.eventlog import EventType as E
from healer.metrics import compute

START, RESTART = E.SERVICE_STARTED, E.SERVICE_RESTARTED
CRASH, HUNG, STOP = E.SERVICE_CRASHED, E.SERVICE_HUNG, E.SERVICE_STOPPED


def events(name, *items):
    return [(ts, name, etype) for ts, etype in items]


class MetricsTests(unittest.TestCase):
    def test_steady_service_is_100_percent_up(self):
        m = compute(events("a", (0, START)), now=100)["services"]["a"]
        self.assertEqual(m["uptime_pct"], 100.0)
        self.assertIsNone(m["mttr_s"])
        self.assertIsNone(m["success_pct"])

    def test_crash_and_recovery(self):
        m = compute(events("a", (0, START), (40, CRASH), (42, RESTART)), now=100)["services"]["a"]
        self.assertEqual((m["uptime_pct"], m["incidents"], m["recovered"], m["mttr_s"],
                          m["success_pct"]), (98.0, 1, 1, 2.0, 100.0))

    def test_unrecovered_incident_counts_against_success(self):
        m = compute(events("a", (0, START), (40, CRASH)), now=100)["services"]["a"]
        self.assertEqual((m["uptime_pct"], m["open"], m["success_pct"]), (40.0, 1, 0.0))

    def test_hang_then_kill_then_restart_is_one_incident(self):
        m = compute(events("a", (0, START), (30, HUNG), (31, CRASH), (33, RESTART)),
                    now=100)["services"]["a"]
        self.assertEqual((m["incidents"], m["mttr_s"]), (1, 3.0))

    def test_operator_stop_is_downtime_but_not_an_incident(self):
        m = compute(events("a", (0, START), (50, STOP), (60, RESTART)), now=100)["services"]["a"]
        self.assertEqual((m["incidents"], m["uptime_pct"]), (0, 90.0))

    def test_overall_aggregates_all_services(self):
        items = sorted(events("a", (0, START), (40, CRASH), (44, RESTART))
                       + events("b", (0, START)), key=lambda e: e[0])
        m = compute(items, now=100)["overall"]
        self.assertEqual((m["incidents"], m["mttr_s"], m["uptime_pct"]), (1, 4.0, 98.0))


if __name__ == "__main__":
    unittest.main()