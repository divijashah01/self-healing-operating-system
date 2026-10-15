#!/usr/bin/env python3
"""Demo client: calls the web service every second and crashes if it is unreachable.
Gives the 'depends_on' feature a real dependency to manage."""
import argparse
import os
import signal
import sys
import time
import urllib.request

from heartbeat import Heartbeat


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8001/health")
    parser.add_argument("--interval", type=float, default=1.0)
    args = parser.parse_args()

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # clean stop
    pid = os.getpid()
    heartbeat = Heartbeat()
    print(f"[client pid={pid}] started", flush=True)
    calls = 0
    try:
        while True:
            heartbeat.beat()
            with urllib.request.urlopen(args.url, timeout=1) as resp:   # raises if web is down
                ok = resp.status == 200
            calls += 1
            print(f"[client pid={pid}] call #{calls} -> {'ok' if ok else 'bad status'}", flush=True)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        print("[client] stopped", flush=True)


if __name__ == "__main__":
    main()