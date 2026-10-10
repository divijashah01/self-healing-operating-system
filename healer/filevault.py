"""Versioned file vault: SHA-256 baselines, content-addressed snapshots, atomic restore."""
import hashlib
import logging
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .eventlog import EventLog, EventType, Severity

log = logging.getLogger("healer.files")


class FileState:
    OK = "ok"
    MISSING = "missing"
    MODIFIED = "modified"
    NO_BASELINE = "no_baseline"


@dataclass
class Check:
    state: str
    sha256: Optional[str] = None
    data: Optional[bytes] = None


def emit(eventlog: EventLog, event_type: str, severity: str, message: str, **details) -> None:
    """Write to the console log and the event log."""
    log.log(getattr(logging, severity.upper()), "%s", message)
    eventlog.log(event_type, severity=severity, message=message, **details)


def atomic_write(path: Path, data: bytes, mode: int) -> None:
    """Write via a temp file + fsync + rename, so readers never see a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


class FileVault:
    def __init__(self, eventlog: EventLog, snapshot_dir):
        self.eventlog = eventlog
        self.snapshot_dir = Path(snapshot_dir)
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)

    # ---- helpers -------------------------------------------------------
    @staticmethod
    def _read(path: str) -> Optional[bytes]:
        try:
            return Path(path).read_bytes()
        except FileNotFoundError:
            return None

    def _store_blob(self, data: bytes):
        sha = hashlib.sha256(data).hexdigest()
        blob = self.snapshot_dir / sha            # content-addressed: same content, one blob
        if not blob.exists():
            atomic_write(blob, data, 0o600)
        return sha, blob

    def _record(self, path: str, data: bytes, mode: int, keep: int, note: str) -> int:
        sha, blob = self._store_blob(data)
        had_baseline = self.eventlog.get_baseline(path) is not None
        version = self.eventlog.add_file_version(path, sha, len(data), mode, str(blob),
                                                 note=note, baseline=True)
        self._prune(path, keep)
        name = Path(path).name
        if had_baseline:
            emit(self.eventlog, EventType.FILE_APPROVED, Severity.INFO,
                 f"{name}: new baseline v{version} approved",
                 path=path, version=version, sha256=sha)
        else:
            emit(self.eventlog, EventType.FILE_BASELINED, Severity.INFO,
                 f"{name}: baseline v{version} recorded",
                 path=path, version=version, sha256=sha)
        return version

    def _prune(self, path: str, keep: int) -> None:
        for row in self.eventlog.file_versions(path)[keep:]:   # newest first
            if row["is_baseline"]:
                continue                                       # never drop the baseline
            self.eventlog.delete_file_version(path, row["version"])
            if self.eventlog.blob_refs(row["blob_path"]) == 0:
                try:
                    Path(row["blob_path"]).unlink()
                except FileNotFoundError:
                    pass

    # ---- public API ----------------------------------------------------
    def verify(self, path: str) -> Check:
        baseline = self.eventlog.get_baseline(path)
        if baseline is None:
            return Check(FileState.NO_BASELINE)
        data = self._read(path)
        if data is None:
            return Check(FileState.MISSING)
        sha = hashlib.sha256(data).hexdigest()
        state = FileState.OK if sha == baseline["sha256"] else FileState.MODIFIED
        return Check(state, sha, data)

    def adopt(self, path: str, keep: int, note: str = "") -> int:
        """Make the file's CURRENT content the new baseline (a new version)."""
        data = self._read(path)
        if data is None:
            raise FileNotFoundError(path)
        mode = stat.S_IMODE(os.stat(path).st_mode)
        return self._record(path, data, mode, keep, note)

    def update(self, path: str, data: bytes, keep: int, note: str = "") -> int:
        """Authorized change: record the new baseline FIRST, then write the file,
        so the guard never sees the new content as a violation."""
        try:
            mode = stat.S_IMODE(os.stat(path).st_mode)
        except FileNotFoundError:
            mode = 0o644
        version = self._record(path, data, mode, keep, note)
        atomic_write(Path(path), data, mode)
        return version

    def restore(self, path: str, version: Optional[int] = None, reason: str = "manual") -> int:
        """Write a stored version back to disk (default: the baseline). Restoring a
        specific version also makes it the baseline."""
        row = (self.eventlog.get_file_version(path, version) if version is not None
               else self.eventlog.get_baseline(path))
        if row is None:
            raise KeyError(f"no such version for {path}")
        data = Path(row["blob_path"]).read_bytes()
        if hashlib.sha256(data).hexdigest() != row["sha256"]:
            raise ValueError(f"snapshot v{row['version']} is itself corrupted; refusing to restore")
        atomic_write(Path(path), data, row["mode"])
        self.eventlog.set_baseline(path, row["version"])
        emit(self.eventlog, EventType.FILE_RESTORED,
             Severity.WARNING if reason == "auto-heal" else Severity.INFO,
             f"{Path(path).name}: restored from v{row['version']} ({reason})",
             path=path, version=row["version"], sha256=row["sha256"], reason=reason)
        return row["version"]