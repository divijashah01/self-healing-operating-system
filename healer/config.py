"""Load and validate the healer configuration."""
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Optional

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "healer.yaml"


class ConfigError(ValueError):
    pass

VALID_POLICIES = ("never", "on-failure", "always")
_FLOAT_OPTIONS = ("backoff_base", "backoff_max", "window", "stable_after", "cooldown")

@dataclass(frozen=True)
class RestartPolicy:
    policy: str = "on-failure"
    backoff_base: float = 1.0     # seconds before the 1st restart; doubles per consecutive failure
    backoff_max: float = 30.0
    max_failures: int = 5         # this many failures within `window` seconds -> quarantine
    window: float = 60.0
    stable_after: float = 20.0    # healthy this long -> failure counters reset
    cooldown: float = 60.0        # quarantine length before one probe restart (0 = manual reset only)

@dataclass(frozen=True)
class HeartbeatPolicy:
    timeout: float = 5.0         # no heartbeat for this long -> hung
    startup_grace: float = 5.0   # don't judge a service this soon after (re)start
    term_grace: float = 3.0      # after SIGTERM, wait this long before SIGKILL

VALID_FILE_MODES = ("restore", "alert")
VALID_VALIDATORS = ("keyvalue",)

@dataclass(frozen=True)
class ProtectedFile:
    path: Path
    mode: str = "restore"            # restore = auto-heal, alert = only report
    keep_versions: int = 5           # snapshot versions kept (the baseline is always kept)
    validate: Optional[str] = None   # content check; failing it is reported as "corrupted"

@dataclass(frozen=True)
class CheckpointPolicy:
    keep: int = 5                # checkpoints kept per service
    trust_after: float = 10.0    # a restored service crashing sooner than this blames its checkpoint

@dataclass(frozen=True)
class ServiceConfig:
    name: str
    command: tuple
    env: dict = field(default_factory=dict)
    restart: RestartPolicy = field(default_factory=RestartPolicy)
    heartbeat: Optional[HeartbeatPolicy] = None
    checkpoint: Optional[CheckpointPolicy] = None
    memory_limit_mb: Optional[float] = None
    depends_on: tuple = ()


@dataclass(frozen=True)
class HealerSettings:
    data_dir: Path
    db_path: Path
    poll_interval: float


@dataclass(frozen=True)
class Config:
    healer: HealerSettings
    services: tuple
    protected_files: tuple = ()


def _resolve(path_str: str) -> Path:
    p = Path(path_str)
    return p if p.is_absolute() else PROJECT_ROOT / p

def _parse_restart(owner: str, raw, base: Optional[RestartPolicy] = None) -> RestartPolicy:
    raw = dict(raw or {})
    unknown = set(raw) - {f.name for f in fields(RestartPolicy)}
    if unknown:
        raise ConfigError(f"{owner}: unknown restart option(s): {', '.join(sorted(unknown))}")
    try:
        for key in _FLOAT_OPTIONS:
            if key in raw:
                raw[key] = float(raw[key])
        if "max_failures" in raw:
            raw["max_failures"] = int(raw["max_failures"])
    except (TypeError, ValueError):
        raise ConfigError(f"{owner}: restart options must be numbers") from None
    p = replace(base or RestartPolicy(), **raw)
    if p.policy not in VALID_POLICIES:
        raise ConfigError(f"{owner}: restart policy must be one of {VALID_POLICIES}")
    if (p.backoff_base <= 0 or p.backoff_max < p.backoff_base or p.max_failures < 1
            or p.window <= 0 or p.stable_after <= 0 or p.cooldown < 0):
        raise ConfigError(f"{owner}: invalid restart settings")
    return p

def _parse_heartbeat(owner: str, raw) -> Optional[HeartbeatPolicy]:
    if raw is None:
        return None                      # heartbeat monitoring not enabled
    if not isinstance(raw, dict):
        raise ConfigError(f"{owner}: 'heartbeat' must be a mapping")
    unknown = set(raw) - {f.name for f in fields(HeartbeatPolicy)}
    if unknown:
        raise ConfigError(f"{owner}: unknown heartbeat option(s): {', '.join(sorted(unknown))}")
    try:
        p = HeartbeatPolicy(**{k: float(v) for k, v in raw.items()})
    except (TypeError, ValueError):
        raise ConfigError(f"{owner}: heartbeat options must be numbers") from None
    if p.timeout <= 0 or p.startup_grace < 0 or p.term_grace <= 0:
        raise ConfigError(f"{owner}: invalid heartbeat settings")
    return p

def _parse_checkpoint(owner: str, raw) -> Optional[CheckpointPolicy]:
    if raw is None:
        return None                      # checkpointing not enabled ({} enables the defaults)
    if not isinstance(raw, dict):
        raise ConfigError(f"{owner}: 'checkpoint' must be a mapping")
    unknown = set(raw) - {"keep", "trust_after"}
    if unknown:
        raise ConfigError(f"{owner}: unknown checkpoint option(s): {', '.join(sorted(unknown))}")
    try:
        p = CheckpointPolicy(keep=int(raw.get("keep", 5)),
                             trust_after=float(raw.get("trust_after", 10.0)))
    except (TypeError, ValueError):
        raise ConfigError(f"{owner}: checkpoint options must be numbers") from None
    if p.keep < 1 or p.trust_after <= 0:
        raise ConfigError(f"{owner}: invalid checkpoint settings")
    return p

def _parse_protected(entry) -> ProtectedFile:
    if not isinstance(entry, dict) or not entry.get("path"):
        raise ConfigError("every protected file needs a 'path'")
    unknown = set(entry) - {"path", "mode", "keep_versions", "validate"}
    if unknown:
        raise ConfigError(f"protected file: unknown option(s): {', '.join(sorted(unknown))}")
    mode = entry.get("mode", "restore")
    if mode not in VALID_FILE_MODES:
        raise ConfigError(f"protected file: mode must be one of {VALID_FILE_MODES}")
    try:
        keep = int(entry.get("keep_versions", 5))
    except (TypeError, ValueError):
        raise ConfigError("protected file: keep_versions must be an integer") from None
    if keep < 1:
        raise ConfigError("protected file: keep_versions must be >= 1")
    validate = entry.get("validate")
    if validate is not None and validate not in VALID_VALIDATORS:
        raise ConfigError(f"protected file: validate must be one of {VALID_VALIDATORS}")
    return ProtectedFile(path=_resolve(str(entry["path"])).resolve(), mode=mode,
                         keep_versions=keep, validate=validate)


def _parse_memory_limit(owner: str, raw) -> Optional[float]:
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ConfigError(f"{owner}: memory_limit_mb must be a number") from None
    if value <= 0:
        raise ConfigError(f"{owner}: memory_limit_mb must be > 0")
    return value

def load_config(path: Optional[Path] = None) -> Config:
    path = Path(path) if path else DEFAULT_CONFIG_PATH
    with open(path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    h = raw.get("healer") or {}
    settings = HealerSettings(
        data_dir=_resolve(h.get("data_dir", "data")),
        db_path=_resolve(h.get("db_path", "data/healer.db")),
        poll_interval=float(h.get("poll_interval", 1.0)),
    )
    if settings.poll_interval <= 0:
        raise ConfigError("healer.poll_interval must be > 0")

    defaults = _parse_restart("restart_defaults", raw.get("restart_defaults"))
    services, seen = [], set()
    for entry in raw.get("services") or []:
        name = entry.get("name")
        command = entry.get("command")
        if not name:
            raise ConfigError("every service needs a 'name'")
        if name in seen:
            raise ConfigError(f"duplicate service name: {name}")
        if (not isinstance(command, list) or not command
                or not all(isinstance(c, str) for c in command)):
            raise ConfigError(f"service '{name}': 'command' must be a non-empty list of strings")
        depends = entry.get("depends_on") or []
        if not isinstance(depends, list) or not all(isinstance(d, str) for d in depends):
            raise ConfigError(f"service '{name}': 'depends_on' must be a list of service names")        
        seen.add(name)
        services.append(ServiceConfig(
            name=name, command=tuple(command), env=dict(entry.get("env") or {}),
            restart=_parse_restart(f"service '{name}'", entry.get("restart"), base=defaults),
            heartbeat=_parse_heartbeat(f"service '{name}'", entry.get("heartbeat")),
            checkpoint=_parse_checkpoint(f"service '{name}'", entry.get("checkpoint")),
            memory_limit_mb=_parse_memory_limit(f"service '{name}'", entry.get("memory_limit_mb")),
            depends_on=tuple(depends),
        ))
    if not services:
        raise ConfigError("no services configured")
    _check_dependencies(services)
    protected, seen_paths = [], set()
    for entry in raw.get("protected_files") or []:
        pf = _parse_protected(entry)
        if pf.path in seen_paths:
            raise ConfigError(f"duplicate protected file: {pf.path}")
        seen_paths.add(pf.path)
        protected.append(pf)
    return Config(healer=settings, services=tuple(services), protected_files=tuple(protected))

def _check_dependencies(services) -> None:
    graph = {s.name: s.depends_on for s in services}
    for name, deps in graph.items():
        for dep in deps:
            if dep not in graph:
                raise ConfigError(f"service '{name}' depends on unknown service '{dep}'")
            if dep == name:
                raise ConfigError(f"service '{name}' cannot depend on itself")
    visiting, done = set(), set()

    def visit(node, path):
        if node in done:
            return
        if node in visiting:
            raise ConfigError("dependency cycle: " + " -> ".join(path + [node]))
        visiting.add(node)
        for dep in graph[node]:
            visit(dep, path + [node])
        visiting.discard(node)
        done.add(node)

    for node in graph:
        visit(node, [])