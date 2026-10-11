#!/usr/bin/env python3
"""Demo worker: processes 'jobs'; progress lives in memory but is checkpointed."""
import argparse
import os
import signal
import sys
import time

from chaos import Chaos
from checkpoint import Checkpointer
from heartbeat import Heartbeat

# In-memory state. Checkpointed periodically; restored by the healer after a crash.
state = {"processed": 0, "last_job": None}
leaked = []          # chaos: memory held on purpose when the 'leak' flag is set


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--checkpoint-every", type=int, default=3,
                        help="save state every N jobs")
    args = parser.parse_args()

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # clean stop
    pid = os.getpid()
    heartbeat = Heartbeat()
    chaos = Chaos()
    checkpointer = Checkpointer("worker")
    restored = checkpointer.load_restored()
    if restored is not None:
        state.update(restored)
        print(f"[worker pid={pid}] restored state: processed={state['processed']}", flush=True)
    print(f"[worker pid={pid}] started", flush=True)
    try:
        while True:
            heartbeat.beat()
            state["processed"] += 1
            state["last_job"] = f"job-{state['processed']}"
            print(f"[worker pid={pid}] processed={state['processed']} "
                  f"last_job={state['last_job']}", flush=True)
            if chaos.flag("leak"):
                if not leaked:
                    print(f"[worker pid={pid}] CHAOS: memory leak started", flush=True)
                leaked.append(b"x" * (8 * 1024 * 1024))   # filled bytes => RSS really grows
            if state["processed"] % args.checkpoint_every == 0:
                saved = checkpointer.save(state)
                if saved:
                    print(f"[worker pid={pid}] checkpoint saved ({saved.name})", flush=True)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        print("[worker] stopped", flush=True)


if __name__ == "__main__":
    main()