"""Process supervision: launch services, detect exits, restart with backoff,
quarantine crash-looping services (circuit breaker), sample resources, reap zombies."""
import logging
import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import psutil

from .config import PROJECT_ROOT, ServiceConfig
from .eventlog import EventLog, EventType, Severity

from .checkpoints import CheckpointManager

log = logging.getLogger("healer.supervisor")

_LOG_LEVELS = {
    Severity.INFO: logging.INFO,
    Severity.WARNING: logging.WARNING,
    Severity.ERROR: logging.ERROR,
    Severity.CRITICAL: logging.CRITICAL,
}


class Status:
    RUNNING = "running"
    STOPPED = "stopped"          # exited cleanly (code 0) or stopped by the healer
    CRASHED = "crashed"          # crashed and not being restarted (policy 'never')
    RESTARTING = "restarting"    # waiting out the backoff delay
    QUARANTINED = "quarantined"  # circuit open: too many failures
    WAITING = "waiting"      # waiting for a dependency to be running


def classify_exit(returncode: int):
    """Return (status, human-readable reason) for a finished process."""
    if returncode == 0:
        return Status.STOPPED, "exited cleanly (code 0)"
    if returncode < 0:
        try:
            name = signal.Signals(-returncode).name
        except ValueError:
            name = str(-returncode)
        return Status.CRASHED, f"killed by signal {name}"
    return Status.CRASHED, f"exited with code {returncode}"


def tail(path: Path, lines: int = 5, max_bytes: int = 4096) -> list:
    """Last few lines of a log file (used as crash diagnostics)."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - max_bytes))
            data = fh.read().decode(errors="replace")
    except OSError:
        return []
    return data.splitlines()[-lines:]


@dataclass
class ManagedService:
    config: ServiceConfig
    proc: Optional[subprocess.Popen] = None
    ps: Optional[psutil.Process] = None
    status: str = Status.STOPPED
    restarts: int = 0                         # total restarts performed
    started_at: Optional[float] = None
    consecutive_failures: int = 0             # drives the backoff delay
    failure_times: list = field(default_factory=list)   # drives the circuit breaker
    next_restart_at: Optional[float] = None
    probation: bool = False                   # True while a post-quarantine probe is running
    hang_since: Optional[float] = None    # set once the healer starts killing a hung process
    kill_reason: Optional[str] = None     # "hung" while a healer-initiated kill is in progress
    kill_sent: bool = False               # SIGKILL already sent
    restored_from: Optional[Path] = None      # checkpoint this run was started from
    reported: set = field(default_factory=set)   # problems already reported while healing is OFF


class Supervisor:
    def __init__(self, services, eventlog: EventLog, logs_dir,
                 clock: Callable[[], float] = time.time, heartbeat_dir=None, checkpoint_dir=None,
                 chaos_dir=None, dep_settle: float = 2.0):
        self.eventlog = eventlog
        self.logs_dir = Path(logs_dir)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.heartbeat_dir = (Path(heartbeat_dir) if heartbeat_dir
                              else self.logs_dir.parent / "heartbeats")
        self.heartbeat_dir.mkdir(parents=True, exist_ok=True)

        self.clock = clock                    # injectable so tests can fake time
        self.services = {s.name: ManagedService(s) for s in services}
        self._reported_zombies = set()
        self.checkpoints = CheckpointManager(
            eventlog, checkpoint_dir or self.logs_dir.parent / "checkpoints")
        self.chaos_dir = Path(chaos_dir) if chaos_dir else self.logs_dir.parent / "chaos"
        self.healing = True      # False = detect and report only, never repair
        self.dep_settle = dep_settle   # a dependency must have run this long before dependents start

    # ---- helpers -------------------------------------------------------
    def _event(self, event_type, service, severity, message, **details):
        log.log(_LOG_LEVELS[severity], "[%s] %s", service or "healer", message)
        self.eventlog.log(event_type, service=service, severity=severity,
                          message=message, **details)

    def _signal(self, svc: ManagedService, sig: int) -> None:
        try:
            os.killpg(svc.proc.pid, sig)      # service is its own process-group leader
        except ProcessLookupError:
            pass

    def _heartbeat_path(self, svc: ManagedService) -> Path:
        return self.heartbeat_dir / f"{svc.config.name}.hb"

    def _env_for(self, svc: ManagedService, restore_path=None) -> dict:
        env = {**os.environ, **svc.config.env}
        if svc.config.heartbeat is not None:
            env["HEALER_HEARTBEAT_FILE"] = str(self._heartbeat_path(svc))
        if svc.config.checkpoint is not None:
            env["HEALER_CHECKPOINT_DIR"] = str(self.checkpoints.dir_for(svc.config.name))
            env["HEALER_CHECKPOINT_KEEP"] = str(svc.config.checkpoint.keep)
            if restore_path is not None:
                env["HEALER_RESTORE_FROM"] = str(restore_path)
        env["HEALER_CHAOS_DIR"] = str(self.chaos_dir / svc.config.name)
        return env

    # ---- lifecycle -----------------------------------------------------
    def start_all(self) -> None:
        for svc in self.services.values():
            if svc.config.depends_on:               # starts once its dependencies are running
                svc.status = Status.WAITING
                self.eventlog.set_state(svc.config.name, svc.status, restarts=svc.restarts)
            else:
                self.start(svc)

    def start(self, svc: ManagedService, restart: bool = False) -> bool:
        name = svc.config.name
        now = self.clock()
        log_path = self.logs_dir / f"{name}.log"
        chosen = None
        if svc.config.checkpoint is not None and self.healing:
            chosen = self.checkpoints.pick(name)      # newest checkpoint that validates
        svc.reported.clear()
        flags = self.chaos_dir / name
        if flags.is_dir():
            for flag in flags.iterdir():
                flag.unlink(missing_ok=True)      # chaos flags never survive a restart
        try:
            with open(log_path, "ab") as out:
                svc.proc = subprocess.Popen(
                    list(svc.config.command), cwd=PROJECT_ROOT,
                    env=self._env_for(svc, chosen.path if chosen else None),
                    stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
        except OSError as exc:
            svc.proc = svc.ps = None
            svc.status = Status.CRASHED
            self._event(EventType.SERVICE_CRASHED, name, Severity.ERROR,
                        f"failed to start: {exc}")
            self._after_exit(svc, Status.CRASHED, now)
            return False
        svc.ps = psutil.Process(svc.proc.pid)
        svc.restored_from = chosen.path if chosen else None
        svc.ps.cpu_percent(None)              # prime the CPU counter
        svc.status = Status.RUNNING
        svc.started_at = now
        svc.next_restart_at = None
        svc.hang_since, svc.kill_reason, svc.kill_sent = None, None, False
        self.eventlog.set_state(name, svc.status, pid=svc.proc.pid, restarts=svc.restarts)
        if restart:
            self._event(EventType.SERVICE_RESTARTED, name, Severity.INFO,
                        f"restarted (pid {svc.proc.pid}, restart #{svc.restarts})",
                        pid=svc.proc.pid, restarts=svc.restarts)
        else:
            self._event(EventType.SERVICE_STARTED, name, Severity.INFO,
                        f"started (pid {svc.proc.pid})", pid=svc.proc.pid)
        if chosen is not None:
            age = round(time.time() - chosen.ts, 1)
            self._event(EventType.CHECKPOINT_RESTORED, name, Severity.INFO,
                        f"restoring state from checkpoint #{chosen.seq} (saved {age:g}s ago)",
                        seq=chosen.seq, age_s=age, path=str(chosen.path))
        return True

    def stop_all(self, grace: float = 5.0) -> None:
        live = [s for s in self.services.values() if s.proc is not None]
        for svc in live:
            self._signal(svc, signal.SIGTERM)
        deadline = time.time() + grace
        for svc in live:
            try:
                svc.proc.wait(timeout=max(0.0, deadline - time.time()))
            except subprocess.TimeoutExpired:
                self._signal(svc, signal.SIGKILL)
                svc.proc.wait()
            name, pid = svc.config.name, svc.proc.pid
            svc.status, svc.proc, svc.ps = Status.STOPPED, None, None
            self._event(EventType.SERVICE_STOPPED, name, Severity.INFO,
                        "stopped by healer", pid=pid)
            self.eventlog.set_state(name, Status.STOPPED, restarts=svc.restarts)
        # services waiting to restart / quarantined have no process, just pending state
        for svc in self.services.values():
            if svc.status in (Status.RESTARTING, Status.QUARANTINED, Status.WAITING):
                svc.status, svc.next_restart_at = Status.STOPPED, None
                self.eventlog.set_state(svc.config.name, Status.STOPPED, restarts=svc.restarts)

    # ---- monitoring ----------------------------------------------------
    def check(self) -> None:
        """One monitoring pass; call this every poll interval."""
        now = self.clock()
        for svc in self.services.values():
            if svc.proc is not None:
                returncode = svc.proc.poll()  # also reaps the child
                if returncode is None:
                    self._sample(svc)
                    if self._check_memory(svc, now):
                        continue
                    self._maybe_mark_stable(svc, now)
                    self._check_heartbeat(svc, now)
                    self._check_checkpoint_trust(svc, now)
                else:
                    self._handle_exit(svc, returncode, now)
            elif svc.status in (Status.RESTARTING, Status.QUARANTINED):
                self._maybe_restart(svc, now)
            elif svc.status == Status.WAITING:
                if self._deps_ready(svc, now):
                    self._start_after_wait(svc)
        self._reap_zombies()

    def _sample(self, svc: ManagedService) -> None:
        try:
            cpu = svc.ps.cpu_percent(interval=None)
            rss_mb = svc.ps.memory_info().rss / (1024 * 1024)
        except psutil.NoSuchProcess:
            return
        self.eventlog.set_state(svc.config.name, Status.RUNNING, pid=svc.proc.pid,
                                restarts=svc.restarts, cpu_percent=cpu,
                                rss_mb=round(rss_mb, 1))

    # ---- hang detection ------------------------------------------------
    def _check_heartbeat(self, svc: ManagedService, now: float) -> None:
        hb = svc.config.heartbeat
        if hb is None:
            return
        if svc.hang_since is not None:        # already terminating it: escalate if ignored
            if not svc.kill_sent and now - svc.hang_since >= hb.term_grace:
                svc.kill_sent = True
                self._signal(svc, signal.SIGKILL)
                self._event(EventType.HANG_ESCALATED, svc.config.name, Severity.ERROR,
                            f"still alive {hb.term_grace:g}s after SIGTERM; sent SIGKILL",
                            pid=svc.proc.pid)
            return
        if svc.started_at is None or now - svc.started_at < hb.startup_grace:
            return
        try:
            last = self._heartbeat_path(svc).stat().st_mtime
        except OSError:
            last = 0.0                        # never beat since start
        age = now - max(last, svc.started_at)  # ignore stale files from a previous run
        if age <= hb.timeout:
            svc.reported.discard("hang")
        elif not self.healing:
            if "hang" not in svc.reported:        # report once, take no action
                svc.reported.add("hang")
                self._event(EventType.SERVICE_HUNG, svc.config.name, Severity.ERROR,
                            f"no heartbeat for {age:.1f}s (timeout {hb.timeout:g}s); "
                            f"healing is OFF, no action",
                            pid=svc.proc.pid, age_s=round(age, 1))
        else:
            self._declare_hung(svc, now, age)

    def _declare_hung(self, svc: ManagedService, now: float, age: float) -> None:
        hb, name = svc.config.heartbeat, svc.config.name
        try:
            proc_status = svc.ps.status()     # e.g. "stopped", "sleeping", "running"
        except psutil.NoSuchProcess:
            proc_status = "gone"
        svc.hang_since, svc.kill_reason = now, "hung"
        self._event(EventType.SERVICE_HUNG, name, Severity.ERROR,
                    f"no heartbeat for {age:.1f}s (timeout {hb.timeout:g}s); sending SIGTERM",
                    pid=svc.proc.pid, age_s=round(age, 1), proc_status=proc_status)
        self._signal(svc, signal.SIGTERM)
        self._signal(svc, signal.SIGCONT)     # a stopped process only acts on SIGTERM once continued

    def _handle_exit(self, svc: ManagedService, returncode: int, now: float) -> None:
        name, pid = svc.config.name, svc.proc.pid
        status, reason = classify_exit(returncode)
        if svc.kill_reason == "hung":   # we killed it: a failure even if it exited with code 0
            status = Status.CRASHED
            reason = ("terminated by healer after hang (SIGKILL)" if svc.kill_sent
                      else "terminated by healer after hang (SIGTERM)")
        svc.hang_since, svc.kill_reason, svc.kill_sent = None, None, False
        uptime = round(now - svc.started_at, 1) if svc.started_at else None
        svc.status, svc.proc, svc.ps = status, None, None
        if status == Status.CRASHED:
            self._event(EventType.SERVICE_CRASHED, name, Severity.ERROR, reason,
                        pid=pid, exit_code=returncode, uptime_s=uptime,
                        last_output=tail(self.logs_dir / f"{name}.log"))
        else:
            self._event(EventType.SERVICE_STOPPED, name, Severity.INFO, reason,
                        pid=pid, exit_code=returncode, uptime_s=uptime)
        cp = svc.config.checkpoint
        if (status == Status.CRASHED and cp is not None
                and uptime is not None and uptime < cp.trust_after):
            if svc.restored_from is not None:
                self.checkpoints.suspect(name, svc.restored_from,
                                         f"service crashed {uptime:.1f}s after restoring it")
            else:                       # crashed even without a checkpoint: not the state's fault
                self.checkpoints.exonerate(name)
        svc.restored_from = None
        self._after_exit(svc, status, now)
        self._cascade_down(name)

    # ---- restart policy / backoff / circuit breaker --------------------
    def _after_exit(self, svc: ManagedService, status: str, now: float) -> None:
        policy = svc.config.restart.policy
        wants_restart = ((status == Status.CRASHED and policy != "never")
                         or (status == Status.STOPPED and policy == "always"))
        if wants_restart and self.healing:
            self._schedule_restart(svc, now)
        else:
            svc.next_restart_at = None
            self.eventlog.set_state(svc.config.name, status, restarts=svc.restarts)

    def _schedule_restart(self, svc: ManagedService, now: float) -> None:
        p, name = svc.config.restart, svc.config.name
        svc.failure_times = [t for t in svc.failure_times if now - t <= p.window] + [now]
        svc.consecutive_failures += 1
        if svc.probation or len(svc.failure_times) >= p.max_failures:
            self._open_circuit(svc, now)
            return
        delay = min(p.backoff_base * 2 ** (svc.consecutive_failures - 1), p.backoff_max)
        svc.status = Status.RESTARTING
        svc.next_restart_at = now + delay
        self.eventlog.set_state(name, svc.status, restarts=svc.restarts)
        self._event(EventType.RESTART_SCHEDULED, name, Severity.WARNING,
                    f"restart in {delay:g}s (failure {len(svc.failure_times)}/"
                    f"{p.max_failures} within {p.window:g}s)",
                    delay_s=delay, failures_in_window=len(svc.failure_times))

    def _open_circuit(self, svc: ManagedService, now: float) -> None:
        p, name = svc.config.restart, svc.config.name
        reason = ("probe restart failed" if svc.probation
                  else f"{len(svc.failure_times)} failures within {p.window:g}s")
        svc.probation = False
        svc.status = Status.QUARANTINED
        svc.next_restart_at = now + p.cooldown if p.cooldown > 0 else None
        self.eventlog.set_state(name, svc.status, restarts=svc.restarts)
        retry = (f"probe restart in {p.cooldown:g}s" if p.cooldown > 0
                 else "manual reset required")
        self._event(EventType.CIRCUIT_OPEN, name, Severity.CRITICAL,
                    f"circuit open ({reason}); quarantined, {retry}",
                    failures=len(svc.failure_times), cooldown_s=p.cooldown)

    def _maybe_restart(self, svc: ManagedService, now: float) -> None:
        if not self.healing:
            return
        if svc.next_restart_at is None or now < svc.next_restart_at:
            return
        if not self._deps_ready(svc, now):
            return                                   # backoff is over, but a dependency is down
        if svc.status == Status.QUARANTINED:      # cooldown over: one probe restart
            svc.probation = True
            svc.failure_times.clear()
            svc.consecutive_failures = 0
        svc.restarts += 1
        self.start(svc, restart=True)

    def _maybe_mark_stable(self, svc: ManagedService, now: float) -> None:
        p = svc.config.restart
        if svc.started_at is None or now - svc.started_at < p.stable_after:
            return
        if not (svc.consecutive_failures or svc.failure_times or svc.probation):
            return
        svc.consecutive_failures = 0
        svc.failure_times.clear()
        if svc.probation:
            svc.probation = False
            self._event(EventType.CIRCUIT_CLOSED, svc.config.name, Severity.INFO,
                        f"stable for {p.stable_after:g}s; circuit closed")

    def reset_circuit(self, name: str) -> bool:
        """Manually release a quarantined service (used by the dashboard in Phase 7)."""
        svc = self.services.get(name)
        if svc is None or svc.status != Status.QUARANTINED:
            return False
        svc.failure_times.clear()
        svc.consecutive_failures = 0
        svc.probation = False
        svc.status = Status.RESTARTING            # fresh start, not a probe
        svc.next_restart_at = self.clock()
        self.eventlog.set_state(name, svc.status, restarts=svc.restarts)
        self._event(EventType.CIRCUIT_CLOSED, name, Severity.INFO,
                    "circuit manually reset; restarting")
        return True


    # ---- operator actions (called by the command executor) -------------
    def _terminate(self, svc: ManagedService, grace: float = 3.0,
                   reason: str = "operator") -> None:
        """Stop a running service: SIGTERM (+SIGCONT), then SIGKILL after `grace` seconds."""
        if svc.proc is None:
            return
        name, pid = svc.config.name, svc.proc.pid
        self._signal(svc, signal.SIGTERM)
        self._signal(svc, signal.SIGCONT)
        try:
            svc.proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            self._signal(svc, signal.SIGKILL)
            svc.proc.wait()
        svc.proc = svc.ps = None
        svc.hang_since, svc.kill_reason, svc.kill_sent = None, None, False
        svc.restored_from = None
        self._event(EventType.SERVICE_STOPPED, name, Severity.INFO,
                    f"stopped by {reason}", pid=pid)

    @staticmethod
    def _reset_counters(svc: ManagedService) -> None:
        svc.failure_times.clear()
        svc.consecutive_failures = 0
        svc.probation = False
        svc.next_restart_at = None

    def stop(self, name: str) -> None:
        svc = self.services[name]
        self._terminate(svc)
        self._cascade_down(name)
        self._reset_counters(svc)
        svc.status = Status.STOPPED               # a stopped service is never auto-restarted
        self.eventlog.set_state(name, Status.STOPPED, restarts=svc.restarts)

    def restart(self, name: str) -> bool:
        svc = self.services[name]
        self._terminate(svc)
        self._cascade_down(name)
        self._reset_counters(svc)                 # operator intent, not a failure
        svc.restarts += 1
        return self.start(svc, restart=True)

    def start_service(self, name: str) -> bool:
        svc = self.services[name]
        if svc.proc is not None:
            return False
        self._reset_counters(svc)
        return self.start(svc)

    # ---- zombies -------------------------------------------------------
    def _reap_zombies(self) -> None:
        managed = {s.proc.pid for s in self.services.values() if s.proc}
        me = os.getpid()
        for child in psutil.Process(me).children(recursive=True):
            if child.pid in managed or child.pid in self._reported_zombies:
                continue
            try:
                if child.status() != psutil.STATUS_ZOMBIE:
                    continue
                ppid = child.ppid()
            except psutil.NoSuchProcess:
                continue
            if ppid == me:                  # our own un-waited child: we can reap it
                try:
                    os.waitpid(child.pid, os.WNOHANG)
                except ChildProcessError:
                    pass
                self._event(EventType.ZOMBIE_REAPED, None, Severity.WARNING,
                            f"reaped zombie child pid {child.pid}", pid=child.pid)
            else:                           # a service's child: only its parent can reap it
                self._reported_zombies.add(child.pid)
                self._event(EventType.ZOMBIE_DETECTED, None, Severity.WARNING,
                            f"zombie pid {child.pid} (parent {ppid}) awaiting its parent",
                            pid=child.pid, ppid=ppid)


    # ---- memory limit --------------------------------------------------
    def _check_memory(self, svc: ManagedService, now: float) -> bool:
        """Enforce the per-service RSS limit. Returns True if the service was killed."""
        limit = svc.config.memory_limit_mb
        if limit is None or svc.ps is None:
            return False
        try:
            rss_mb = svc.ps.memory_info().rss / (1024 * 1024)
        except psutil.NoSuchProcess:
            return False
        if rss_mb <= limit:
            svc.reported.discard("memory")
            return False
        name = svc.config.name
        if not self.healing:
            if "memory" not in svc.reported:      # report once, take no action
                svc.reported.add("memory")
                self._event(EventType.RESOURCE_LIMIT, name, Severity.ERROR,
                            f"memory {rss_mb:.0f} MB exceeds limit {limit:g} MB; "
                            f"healing is OFF, no action",
                            rss_mb=round(rss_mb, 1), limit_mb=limit)
            return False
        self._event(EventType.RESOURCE_LIMIT, name, Severity.ERROR,
                    f"memory {rss_mb:.0f} MB exceeds limit {limit:g} MB; restarting",
                    rss_mb=round(rss_mb, 1), limit_mb=limit)
        self._terminate(svc, reason="memory limit")
        svc.status = Status.CRASHED
        self._after_exit(svc, Status.CRASHED, now)   # counts as a failure: backoff / breaker
        self._cascade_down(name)
        return True

    # ---- healing switch and chaos --------------------------------------
    def set_healing(self, enabled: bool) -> None:
        if enabled == self.healing:
            return
        self.healing = enabled
        now = self.clock()
        for svc in self.services.values():
            if svc.proc is not None:
                continue
            if enabled and svc.status == Status.CRASHED and svc.config.restart.policy != "never":
                self._schedule_restart(svc, now)      # repair what broke while healing was off
            elif not enabled and svc.status in (Status.RESTARTING, Status.QUARANTINED):
                svc.status, svc.next_restart_at = Status.CRASHED, None   # cancel pending repairs
                self.eventlog.set_state(svc.config.name, svc.status, restarts=svc.restarts)

    def inject_signal(self, name: str, sig: int) -> int:
        svc = self.services[name]
        if svc.proc is None:
            raise ValueError(f"{name} is not running")
        self._signal(svc, sig)
        return svc.proc.pid

    def set_chaos_flag(self, name: str, flag: str) -> None:
        directory = self.chaos_dir / self.services[name].config.name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / flag).touch()

    def _check_checkpoint_trust(self, svc: ManagedService, now: float) -> None:
        cp = svc.config.checkpoint
        if cp is None or "trusted" in svc.reported or svc.started_at is None:
            return
        if now - svc.started_at >= cp.trust_after:
            svc.reported.add("trusted")
            self.checkpoints.condemn(svc.config.name)   # stable without them: they were bad

    # ---- dependencies --------------------------------------------------
    def _deps_ready(self, svc: ManagedService, now: float) -> bool:
        """All dependencies running, each for at least `dep_settle` seconds."""
        for dep_name in svc.config.depends_on:
            dep = self.services[dep_name]
            if (dep.status != Status.RUNNING or dep.started_at is None
                    or now - dep.started_at < self.dep_settle):
                return False
        return True

    def _all_dependents(self, name: str) -> list:
        """Services that depend on `name`, directly or transitively."""
        seen, queue, found = set(), [name], []
        while queue:
            current = queue.pop()
            for svc in self.services.values():
                n = svc.config.name
                if current in svc.config.depends_on and n not in seen:
                    seen.add(n)
                    found.append(svc)
                    queue.append(n)
        return found

    def _cascade_down(self, name: str) -> None:
        """`name` went down: stop what depends on it. Dependents wait and start again once
        it has been running for `dep_settle` seconds. Not their fault: no failure counted."""
        if not self.healing:
            return
        for dep in self._all_dependents(name):
            if dep.proc is not None:
                self._terminate(dep, reason=f"dependency {name} is down")
            elif dep.status not in (Status.RESTARTING, Status.QUARANTINED):
                continue
            self._reset_counters(dep)
            dep.status = Status.WAITING
            self.eventlog.set_state(dep.config.name, Status.WAITING, restarts=dep.restarts)
            self._event(EventType.SERVICE_WAITING, dep.config.name, Severity.WARNING,
                        f"waiting for dependency {name}", dependency=name)

    def _start_after_wait(self, svc: ManagedService) -> None:
        again = svc.started_at is not None           # False on the very first start
        if again:
            svc.restarts += 1
        self.start(svc, restart=again)