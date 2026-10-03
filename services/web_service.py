#!/usr/bin/env python3
"""Demo web service: a tiny HTTP server for the healer to supervise."""
import argparse
import json
import os
import signal
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from heartbeat import Heartbeat

START = time.time()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"status": "ok"})
        elif self.path == "/":
            self._send(200, {"service": "web", "pid": os.getpid(),
                             "uptime_s": round(time.time() - START, 1)})
        else:
            self._send(404, {"error": "not found"})

    def _send(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        print(f"[web pid={os.getpid()}] {self.address_string()} {fmt % args}", flush=True)

class Server(ThreadingHTTPServer):
    """Beats from serve_forever's own loop, so a stuck main loop stops the heartbeat."""
    heartbeat = Heartbeat()

    def service_actions(self):
        self.heartbeat.beat()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8001)
    args = parser.parse_args()

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # clean stop
    server = Server((args.host, args.port), Handler)
    print(f"[web pid={os.getpid()}] listening on {args.host}:{args.port}", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        print("[web] stopped", flush=True)


if __name__ == "__main__":
    main()