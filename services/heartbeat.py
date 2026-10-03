"""Heartbeat helper for demo services.

The healer passes the heartbeat file path via HEALER_HEARTBEAT_FILE. Call beat()
from the service's MAIN loop (not a background thread), so that a stuck main
loop means a missing heartbeat.
"""
import os
from pathlib import Path


class Heartbeat:
    def __init__(self):
        path = os.environ.get("HEALER_HEARTBEAT_FILE")
        self.path = Path(path) if path else None

    def beat(self) -> None:
        if self.path is not None:
            self.path.touch()       # updates mtime; the healer reads it