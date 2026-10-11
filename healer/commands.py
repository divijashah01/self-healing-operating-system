"""Executes operator commands queued by the dashboard (the Django -> healer channel)."""
import logging

from .config import Config
from .eventlog import EventLog, EventType, Severity
from .fileguard import VALIDATORS
from .filevault import FileVault, emit
from .supervisor import Status, Supervisor

log = logging.getLogger("healer.commands")

SERVICE_ACTIONS = {"restart_service", "stop_service", "start_service", "reset_circuit"}


class CommandError(Exception):
    """A command that can't be carried out (shown to the operator as-is)."""


class CommandExecutor:
    def __init__(self, config: Config, eventlog: EventLog, supervisor: Supervisor,
                 vault: FileVault):
        self.eventlog = eventlog
        self.supervisor = supervisor
        self.vault = vault
        self._files = {str(pf.path): pf for pf in config.protected_files}
        self._handlers = {
            "restart_service": self._restart_service,
            "stop_service": self._stop_service,
            "start_service": self._start_service,
            "reset_circuit": self._reset_circuit,
            "restore_file": self._restore_file,
            "update_file": self._update_file,
        }

    def process(self) -> None:
        """Run every pending command once; call each healer loop."""
        for cmd in self.eventlog.pending_commands():
            action, target = cmd["action"], cmd["target"]
            service = target if action in SERVICE_ACTIONS else None
            try:
                handler = self._handlers.get(action)
                if handler is None:
                    raise CommandError(f"unknown action {action!r}")
                message = handler(target, cmd["args"])
            except CommandError as exc:
                self._finish(cmd, False, str(exc), service)
            except Exception as exc:
                log.exception("command %s crashed", cmd["id"])
                self._finish(cmd, False, f"{type(exc).__name__}: {exc}", service)
            else:
                self._finish(cmd, True, message, service)

    def _finish(self, cmd: dict, ok: bool, message: str, service) -> None:
        self.eventlog.finish_command(cmd["id"], ok, message)
        if ok:
            emit(self.eventlog, EventType.COMMAND_EXECUTED, Severity.INFO,
                 f"operator: {message}", service=service,
                 command_id=cmd["id"], action=cmd["action"])
        else:
            emit(self.eventlog, EventType.COMMAND_FAILED, Severity.WARNING,
                 f"operator command '{cmd['action']}' failed: {message}", service=service,
                 command_id=cmd["id"], action=cmd["action"])

    # ---- service actions -----------------------------------------------
    def _service(self, target) -> str:
        if target not in self.supervisor.services:
            raise CommandError(f"unknown service {target!r}")
        return target

    def _restart_service(self, target, args) -> str:
        name = self._service(target)
        if not self.supervisor.restart(name):
            raise CommandError(f"{name} failed to start")
        return f"restarted {name}"

    def _stop_service(self, target, args) -> str:
        name = self._service(target)
        self.supervisor.stop(name)
        return f"stopped {name}"

    def _start_service(self, target, args) -> str:
        name = self._service(target)
        if self.supervisor.services[name].status == Status.RUNNING:
            raise CommandError(f"{name} is already running")
        if not self.supervisor.start_service(name):
            raise CommandError(f"{name} failed to start")
        return f"started {name}"

    def _reset_circuit(self, target, args) -> str:
        name = self._service(target)
        if not self.supervisor.reset_circuit(name):
            raise CommandError(f"{name} is not quarantined")
        return f"reset circuit for {name}"

    # ---- file actions --------------------------------------------------
    def _file(self, target):
        pf = self._files.get(str(target))
        if pf is None:
            raise CommandError(f"{target} is not a protected file")
        return pf

    def _restore_file(self, target, args) -> str:
        pf = self._file(target)
        version = args.get("version")
        version = int(version) if version not in (None, "") else None
        v = self.vault.restore(str(pf.path), version, reason="operator")
        return f"restored {pf.path.name} to v{v}"

    def _update_file(self, target, args) -> str:
        pf = self._file(target)
        content = args.get("content")
        if not isinstance(content, str):
            raise CommandError("missing 'content'")
        data = content.replace("\r\n", "\n").encode("utf-8")   # browsers send CRLF
        if pf.validate and not VALIDATORS[pf.validate](data):
            raise CommandError(f"content fails the '{pf.validate}' check; not applied")
        v = self.vault.update(str(pf.path), data, pf.keep_versions,
                              note="approved via dashboard")
        return f"applied new content to {pf.path.name} (baseline v{v})"