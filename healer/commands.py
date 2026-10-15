"""Executes operator commands queued by the dashboard (the Django -> healer channel)."""
import logging
import signal

from .config import Config
from .eventlog import EventLog, EventType, Severity
from .fileguard import VALIDATORS
from .filevault import FileVault, emit
from .supervisor import Status, Supervisor

log = logging.getLogger("healer.commands")

SERVICE_ACTIONS = {"restart_service", "stop_service", "start_service", "reset_circuit",
                   "chaos_kill", "chaos_freeze", "chaos_leak"}


class CommandError(Exception):
    """A command that can't be carried out (shown to the operator as-is)."""


class CommandExecutor:
    def __init__(self, config: Config, eventlog: EventLog, supervisor: Supervisor,
                 vault: FileVault, guard=None):
        self.eventlog = eventlog
        self.supervisor = supervisor
        self.vault = vault
        self.guard = guard
        self._files = {str(pf.path): pf for pf in config.protected_files}
        self._handlers = {
            "restart_service": self._restart_service,
            "stop_service": self._stop_service,
            "start_service": self._start_service,
            "reset_circuit": self._reset_circuit,
            "restore_file": self._restore_file,
            "update_file": self._update_file,
            "chaos_kill": self._chaos_kill,
            "chaos_freeze": self._chaos_freeze,
            "chaos_leak": self._chaos_leak,
            "chaos_file": self._chaos_file,
            "set_healing": self._set_healing,
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
        action = cmd["action"]
        if not ok:
            emit(self.eventlog, EventType.COMMAND_FAILED, Severity.WARNING,
                 f"operator command '{action}' failed: {message}", service=service,
                 command_id=cmd["id"], action=action)
            return
        if action.startswith("chaos_"):
            event_type, prefix, severity = EventType.CHAOS_INJECTED, "chaos", Severity.WARNING
        elif action == "set_healing":
            event_type, prefix, severity = EventType.HEALING_CHANGED, "operator", Severity.WARNING
        else:
            event_type, prefix, severity = EventType.COMMAND_EXECUTED, "operator", Severity.INFO
        emit(self.eventlog, event_type, severity, f"{prefix}: {message}", service=service,
             command_id=cmd["id"], action=action)

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

    # ---- chaos injection -----------------------------------------------
    def _inject(self, target, sig) -> int:
        name = self._service(target)
        try:
            return self.supervisor.inject_signal(name, sig)
        except ValueError as exc:
            raise CommandError(str(exc)) from None

    def _chaos_kill(self, target, args) -> str:
        pid = self._inject(target, signal.SIGKILL)
        return f"killed {target} (pid {pid}, SIGKILL)"

    def _chaos_freeze(self, target, args) -> str:
        pid = self._inject(target, signal.SIGSTOP)
        return f"froze {target} (pid {pid}, SIGSTOP)"

    def _chaos_leak(self, target, args) -> str:
        name = self._service(target)
        if self.supervisor.services[name].proc is None:
            raise CommandError(f"{name} is not running")
        self.supervisor.set_chaos_flag(name, "leak")
        return f"started a memory leak in {name}"


    def _chaos_file(self, target, args) -> str:
        pf = self._file(target)
        how = args.get("how")
        if how == "delete":
            pf.path.unlink(missing_ok=True)
        elif how == "corrupt":
            pf.path.write_bytes(b"\x00\xffGARBAGE not a config\n")
        elif how == "tamper":
            with pf.path.open("ab") as fh:        # still valid key=value, but not the baseline
                fh.write(b"debug=true\n")
        else:
            raise CommandError(f"unknown 'how': {how!r} (delete | corrupt | tamper)")
        return f"{how} {pf.path.name}"

    # ---- healing switch ------------------------------------------------
    def _set_healing(self, target, args) -> str:
        if target not in ("on", "off"):
            raise CommandError("target must be 'on' or 'off'")
        enabled = target == "on"
        self.supervisor.set_healing(enabled)
        if self.guard is not None:
            self.guard.healing = enabled
        self.eventlog.set_meta("healing", target)
        return f"healing turned {target}"