# services/chaos.py
"""Chaos flags: the healer drops flag files; a service opts in by checking them."""
import os
from pathlib import Path


class Chaos:
    def __init__(self):
        directory = os.environ.get("HEALER_CHAOS_DIR")
        self.dir = Path(directory) if directory else None

    def flag(self, name: str) -> bool:
        return self.dir is not None and (self.dir / name).exists()