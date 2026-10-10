"""Checkpoint helper for demo services (application-level state checkpointing).

The healer sets HEALER_CHECKPOINT_DIR / HEALER_CHECKPOINT_KEEP, and HEALER_RESTORE_FROM
when it has picked a valid checkpoint for this start. File format: see healer/checkpoints.py.
"""
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Optional

FORMAT = 1


def _canonical(state) -> bytes:
    return json.dumps(state, sort_keys=True, separators=(",", ":")).encode()


class Checkpointer:
    def __init__(self, service: str):
        directory = os.environ.get("HEALER_CHECKPOINT_DIR")
        self.dir = Path(directory) if directory else None
        self.keep = int(os.environ.get("HEALER_CHECKPOINT_KEEP", "5"))
        self.service = service
        self.seq = self._latest_seq()

    def _latest_seq(self) -> int:
        """Resume numbering after restarts (rejected *.bad files still count)."""
        if self.dir is None or not self.dir.exists():
            return 0
        seqs = []
        for p in self.dir.glob("ckpt-*"):
            try:
                seqs.append(int(p.name[5:13]))
            except ValueError:
                pass
        return max(seqs, default=0)

    def load_restored(self) -> Optional[dict]:
        """State to resume from, or None. Re-verifies the checksum (defense in depth)."""
        path = os.environ.get("HEALER_RESTORE_FROM")
        if not path:
            return None
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
        if hashlib.sha256(_canonical(doc["state"])).hexdigest() != doc["sha256"]:
            raise ValueError("checkpoint checksum mismatch")
        return doc["state"]

    def save(self, state: dict) -> Optional[Path]:
        """Atomically write a new checkpoint and prune old ones. Returns its path."""
        if self.dir is None:
            return None
        self.dir.mkdir(parents=True, exist_ok=True)
        self.seq += 1
        doc = {"format": FORMAT, "service": self.service, "seq": self.seq,
               "ts": time.time(), "state": state,
               "sha256": hashlib.sha256(_canonical(state)).hexdigest()}
        path = self.dir / f"ckpt-{self.seq:08d}.json"
        fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=".ckpt-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(doc, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)               # atomic: never a half-written checkpoint
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise
        for old in sorted(self.dir.glob("ckpt-*.json"))[:-self.keep]:
            old.unlink(missing_ok=True)
        return path