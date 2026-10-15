"""Healer side of application-level state checkpointing: validate and select checkpoints.

File format (written by services/checkpoint.py):
  {"format": 1, "service": str, "seq": int, "ts": float, "state": {...}, "sha256": hex}
where sha256 covers the canonical JSON of "state" only.
"""
import hashlib
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .eventlog import EventLog, EventType, Severity
from .filevault import emit

log = logging.getLogger("healer.checkpoints")
FORMAT = 1


@dataclass
class Checkpoint:
    path: Path
    seq: int
    ts: float


def validate(path: Path, service: str) -> Checkpoint:
    """Parse and verify a checkpoint file; raises ValueError describing the problem."""
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"unreadable: {exc}") from None
    if not isinstance(doc, dict) or doc.get("format") != FORMAT:
        raise ValueError("unknown format")
    if doc.get("service") != service:
        raise ValueError(f"belongs to service '{doc.get('service')}'")
    state = doc.get("state")
    if not isinstance(state, dict) or not isinstance(doc.get("seq"), int):
        raise ValueError("malformed")
    canonical = json.dumps(state, sort_keys=True, separators=(",", ":")).encode()
    if hashlib.sha256(canonical).hexdigest() != doc.get("sha256"):
        raise ValueError("checksum mismatch")
    return Checkpoint(Path(path), doc["seq"], float(doc.get("ts", 0.0)))


class CheckpointManager:
    def __init__(self, eventlog: EventLog, root):
        self.eventlog = eventlog
        self.root = Path(root)

    def dir_for(self, service: str) -> Path:
        return self.root / service

    def pick(self, service: str) -> Optional[Checkpoint]:
        """Newest checkpoint that passes validation. Invalid ones are set aside so they
        are never tried again."""
        for path in sorted(self.dir_for(service).glob("ckpt-*.json"), reverse=True):
            try:
                return validate(path, service)
            except ValueError as exc:
                self.reject(service, path, f"corrupted: {exc}")
        return None

    def reject(self, service: str, path: Path, reason: str) -> None:
        path = Path(path)
        try:
            os.replace(path, path.with_name(path.name + ".bad"))
        except OSError:
            pass
        emit(self.eventlog, EventType.CHECKPOINT_REJECTED, Severity.WARNING,
             f"rejected {path.name}: {reason}", service=service, path=str(path))

    def suspect(self, service: str, path: Path, reason: str) -> None:
        """Set a checkpoint aside because the service crashed right after restoring it.
        Not final: see exonerate() and condemn()."""
        path = Path(path)
        try:
            os.replace(path, path.with_name(path.name + ".suspect"))
        except OSError:
            pass
        emit(self.eventlog, EventType.CHECKPOINT_REJECTED, Severity.WARNING,
             f"set aside {path.name} as suspect: {reason}", service=service, path=str(path))

    def _rename_suspects(self, service: str, new_suffix: str) -> list:
        moved = []
        for path in sorted(self.dir_for(service).glob("ckpt-*.json.suspect")):
            target = path.with_name(path.name[:-len(".suspect")] + new_suffix)
            try:
                os.replace(path, target)
                moved.append(target.name)
            except OSError:
                pass
        return moved

    def exonerate(self, service: str) -> int:
        """A start without any checkpoint crashed too: the suspects were not the cause."""
        moved = self._rename_suspects(service, "")
        if moved:
            emit(self.eventlog, EventType.CHECKPOINT_EXONERATED, Severity.INFO,
                 f"restored {len(moved)} suspect checkpoint(s): a start without them "
                 f"crashed too, so they were not the cause", service=service)
        return len(moved)

    def condemn(self, service: str) -> int:
        """The service runs stably without the suspects: they really were bad."""
        moved = self._rename_suspects(service, ".bad")
        if moved:
            emit(self.eventlog, EventType.CHECKPOINT_REJECTED, Severity.WARNING,
                 f"condemned {len(moved)} suspect checkpoint(s): the service runs fine "
                 f"without them", service=service)
        return len(moved)