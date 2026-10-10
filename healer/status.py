"""Print current service state and recent events: python3 -m healer.status"""
import argparse
import time

from .config import load_config
from .eventlog import EventLog


def main() -> None:
    parser = argparse.ArgumentParser(prog="healer.status")
    parser.add_argument("--config")
    parser.add_argument("--events", type=int, default=15, help="how many recent events")
    args = parser.parse_args()

    elog = EventLog(load_config(args.config).healer.db_path)
    print(f"{'SERVICE':<12}{'STATUS':<10}{'PID':<8}{'RESTARTS':<10}{'CPU%':<7}{'RSS MB':<8}")
    for s in elog.get_states():
        cpu = "-" if s["cpu_percent"] is None else f"{s['cpu_percent']:.1f}"
        rss = "-" if s["rss_mb"] is None else f"{s['rss_mb']:.1f}"
        print(f"{s['name']:<12}{s['status']:<10}{s['pid'] or '-':<8}"
              f"{s['restarts']:<10}{cpu:<7}{rss:<8}")
    print("\nRecent events (oldest first):")
    for e in reversed(elog.recent(args.events)):
        t = time.strftime("%H:%M:%S", time.localtime(e["ts"]))
        print(f"{t}  {e['severity']:<9}{(e['service'] or 'healer'):<11}"
              f"{e['event_type']:<20}{e['message']}")


if __name__ == "__main__":
    main()