"""Detect deletion / corruption / tampering of protected files and auto-restore them."""
import logging

from .config import ProtectedFile
from .eventlog import EventLog, EventType, Severity
from .filevault import Check, FileState, FileVault, emit

log = logging.getLogger("healer.fileguard")


def _valid_keyvalue(data: bytes) -> bool:
    lines = [ln for ln in data.decode("utf-8", errors="replace").splitlines() if ln.strip()]
    return bool(lines) and all("=" in ln for ln in lines)


VALIDATORS = {"keyvalue": _valid_keyvalue}      # names must match config.VALID_VALIDATORS


class FileGuard:
    def __init__(self, files, vault: FileVault, eventlog: EventLog):
        self.files = list(files)
        self.vault = vault
        self.eventlog = eventlog
        self._reported = {}        # path -> observed state already reported (avoids log spam)

    def check(self) -> None:
        """One pass over all protected files; call every poll interval."""
        for pf in self.files:
            try:
                self._check_one(pf)
            except Exception:                   # one bad file must not stop the others
                log.exception("checking %s failed", pf.path)

    def _check_one(self, pf: ProtectedFile) -> None:
        path = str(pf.path)
        result = self.vault.verify(path)
        if result.state == FileState.NO_BASELINE:
            if pf.path.exists():                # first sight of the file: trust it
                self.vault.adopt(path, pf.keep_versions, note="initial baseline")
            return                              # (a service may not have created it yet)
        if result.state == FileState.OK:
            self._reported.pop(path, None)
            return
        observed = result.sha256 or "missing"
        if self._reported.get(path) != observed:    # report each distinct bad state once
            self._reported[path] = observed
            self._report(pf, result)
        if pf.mode == "restore":
            self.vault.restore(path, reason="auto-heal")
            self._reported.pop(path, None)

    def _report(self, pf: ProtectedFile, result: Check) -> None:
        path, name = str(pf.path), pf.path.name
        baseline = self.eventlog.get_baseline(path)
        details = dict(path=path, expected_sha256=baseline["sha256"],
                       observed_sha256=result.sha256)
        if result.state == FileState.MISSING:
            emit(self.eventlog, EventType.FILE_MISSING, Severity.ERROR,
                 f"{name}: file deleted", **details)
            return
        validator = VALIDATORS.get(pf.validate) if pf.validate else None
        if validator and not validator(result.data):
            emit(self.eventlog, EventType.FILE_CORRUPTED, Severity.ERROR,
                 f"{name}: content is corrupted (failed '{pf.validate}' check)", **details)
        else:
            emit(self.eventlog, EventType.FILE_MODIFIED, Severity.WARNING,
                 f"{name}: content changed from baseline (unauthorized)", **details)