"""Healing OFF vs ON experiment, driven through the command channel.

Needs a RUNNING healer with the demo config (services 'worker' and 'web', and at least
one protected file). Injects the same three faults twice (healing OFF, then ON) and
reports what recovered and how fast. The result is also saved for the dashboard.

    python3 -m healer.compare [--off-window 15] [--timeout 40] [--port 8001]
"""
import argparse
import json
import sys
import time
import urllib.request

from .config import load_config
from .eventlog import EventLog

FAULTS = ("worker killed", "web frozen", "app.conf deleted")


class Experiment:
    def __init__(self, cfg, log: EventLog, port: int, timeout: float):
        self.log, self.port, self.timeout = log, port, timeout
        self.conf = cfg.protected_files[0].path
        self.original = None

    # ---- observations (what a user would see) --------------------------
    def state(self, name: str) -> dict:
        return next((s for s in self.log.get_states() if s["name"] == name), {})

    def web_ok(self) -> bool:
        try:
            url = f"http://127.0.0.1:{self.port}/health"
            with urllib.request.urlopen(url, timeout=1) as resp:
                return resp.status == 200
        except Exception:
            return False

    def conf_ok(self) -> bool:
        try:
            return self.conf.read_text() == self.original
        except OSError:
            return False

    def healthy(self) -> bool:
        return (self.state("worker").get("status") == "running"
                and self.state("web").get("status") == "running"
                and self.web_ok() and self.conf_ok())

    def wait_healthy(self, timeout: float) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            if self.healthy():
                return True
            time.sleep(0.5)
        return False

    # ---- commands -------------------------------------------------------
    def send(self, *commands) -> float:
        """Queue commands together; return when the last one has finished."""
        ids = [self.log.enqueue_command(a, t, args) for a, t, args in commands]
        end = time.time() + 15
        while time.time() < end:
            rows = [self.log.get_command(i) for i in ids]
            if all(r["status"] != "pending" for r in rows):
                failed = [r for r in rows if r["status"] == "failed"]
                if failed:
                    sys.exit(f"command failed: {failed[0]['action']}: {failed[0]['result']}")
                return max(r["finished_at"] for r in rows)
            time.sleep(0.2)
        sys.exit("the healer did not run the commands: is it running?")

    def inject(self):
        pids = {"worker": self.state("worker").get("pid"), "web": self.state("web").get("pid")}
        t0 = self.send(("chaos_kill", "worker", {}), ("chaos_freeze", "web", {}),
                       ("chaos_file", str(self.conf), {"how": "delete"}))
        return pids, t0

    def measure(self, pids: dict, t0: float, window: float) -> dict:
        """Seconds until each fault was repaired, or None if it never was within the window."""
        pending = {
            "worker killed": lambda: (self.state("worker").get("status") == "running"
                                      and self.state("worker").get("pid") != pids["worker"]),
            "web frozen": lambda: self.web_ok() and self.state("web").get("pid") != pids["web"],
            "app.conf deleted": self.conf_ok,
        }
        results, end = {}, time.time() + window
        while pending and time.time() < end:
            for name, probe in list(pending.items()):
                if probe():
                    results[name] = round(time.time() - t0, 1)
                    del pending[name]
            time.sleep(0.25)
        results.update({name: None for name in pending})
        return results


def cell(value, window: float) -> str:
    return f"recovered in {value:.1f}s" if value is not None else f"NOT recovered within {window:g}s"


def main() -> None:
    parser = argparse.ArgumentParser(prog="healer.compare")
    parser.add_argument("--config")
    parser.add_argument("--off-window", type=float, default=15.0,
                        help="seconds to watch with healing OFF")
    parser.add_argument("--timeout", type=float, default=40.0,
                        help="seconds to wait for recovery with healing ON")
    parser.add_argument("--port", type=int, default=8001, help="the web service's port")
    args = parser.parse_args()

    cfg = load_config(args.config)
    if not {"worker", "web"} <= {s.name for s in cfg.services} or not cfg.protected_files:
        sys.exit("this experiment needs the demo config (services 'worker' and 'web' + a protected file)")
    log = EventLog(cfg.healer.db_path)
    if time.time() - float(log.get_meta("healer_last_seen") or 0) > 5:
        sys.exit("the healer is not running: start it first with  python3 -m healer")

    exp = Experiment(cfg, log, args.port, args.timeout)
    try:
        exp.original = exp.conf.read_text()
    except OSError:
        sys.exit(f"{exp.conf} does not exist yet; wait for the healer to start the file service")

    try:
        print("1/4 waiting for a healthy system ...")
        if not exp.wait_healthy(30):
            sys.exit("the system is not healthy; fix that first (see the dashboard)")
        print(f"2/4 healing OFF: injecting 3 faults, watching for {args.off_window:g}s ...")
        exp.send(("set_healing", "off", {}))
        pids, t0 = exp.inject()
        off = exp.measure(pids, t0, args.off_window)
        print("3/4 healing ON: letting the system repair itself, then injecting the same faults ...")
        exp.send(("set_healing", "on", {}))
        if not exp.wait_healthy(args.timeout):
            sys.exit("the system did not recover after healing was turned on")
        pids, t0 = exp.inject()
        on = exp.measure(pids, t0, args.timeout)
    finally:
        exp.send(("set_healing", "on", {}))               # always leave healing enabled

    print("4/4 done\n")
    print(f"{'Fault':<20}{'Healing OFF':<30}{'Healing ON'}")
    for fault in FAULTS:
        print(f"{fault:<20}{cell(off[fault], args.off_window):<30}{cell(on[fault], args.timeout)}")
    log.set_meta("last_comparison", json.dumps({
        "ts": time.time(), "off_window": args.off_window, "timeout": args.timeout,
        "rows": [{"fault": f, "off": off[f], "on": on[f]} for f in FAULTS]}))
    print("\n(saved: shown on the dashboard's Chaos page)")


if __name__ == "__main__":
    main()