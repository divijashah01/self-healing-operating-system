#!/usr/bin/env python3
"""Demo worker: processes 'jobs' and keeps its progress only in memory."""
import argparse
import os
import signal
import sys
import time
from heartbeat import Heartbeat

# In-memory state. Phase 6 will checkpoint and restore this.
state = {"processed": 0, "last_job": None}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=float, default=1.0)
    args = parser.parse_args()

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # clean stop
    print(f"[worker pid={os.getpid()}] started", flush=True)
    try:
        heartbeat = Heartbeat()
        while True:
            heartbeat.beat()
            state["processed"] += 1
            state["last_job"] = f"job-{state['processed']}"
            print(f"[worker pid={os.getpid()}] processed={state['processed']} "
                  f"last_job={state['last_job']}", flush=True)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        print("[worker] stopped", flush=True)


if __name__ == "__main__":
    main()