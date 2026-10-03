#!/usr/bin/env python3
"""Demo file service: maintains a config file and an append-only journal."""
import argparse
import os
import signal
import sys
import time
from pathlib import Path
from heartbeat import Heartbeat

DEFAULT_CONF = "mode=production\nmax_retries=3\n"


def conf_is_valid(text: str) -> bool:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return bool(lines) and all("=" in ln for ln in lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", default="data/critical")
    parser.add_argument("--interval", type=float, default=1.0)
    args = parser.parse_args()

    directory = Path(args.dir)
    directory.mkdir(parents=True, exist_ok=True)
    conf, journal = directory / "app.conf", directory / "journal.log"
    if not conf.exists():                       # first run only
        conf.write_text(DEFAULT_CONF)

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # clean stop
    print(f"[filewriter pid={os.getpid()}] started, dir={directory}", flush=True)
    tick = 0
    try:
        heartbeat = Heartbeat()
        while True:
            heartbeat.beat()
            tick += 1
            try:
                ok = conf_is_valid(conf.read_text())
            except FileNotFoundError:
                ok = False
            if not ok:
                print("[filewriter] WARNING: app.conf missing or invalid", flush=True)
            with journal.open("a") as fh:
                fh.write(f"{time.strftime('%H:%M:%S')} tick={tick} conf_ok={ok}\n")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        print("[filewriter] stopped", flush=True)


if __name__ == "__main__":
    main()