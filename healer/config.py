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
class ServiceConfig:
    name: str
    command: tuple
    env: dict = field(default_factory=dict)
    restart: RestartPolicy = field(default_factory=RestartPolicy)


@dataclass(frozen=True)
class HealerSettings:
    data_dir: Path
    db_path: Path
    poll_interval: float


@dataclass(frozen=True)
class Config:
    healer: HealerSettings
    services: tuple


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
        seen.add(name)
        services.append(ServiceConfig(
            name=name, command=tuple(command), env=dict(entry.get("env") or {}),
            restart=_parse_restart(f"service '{name}'", entry.get("restart"), base=defaults),
        ))
    if not services:
        raise ConfigError("no services configured")
    return Config(healer=settings, services=tuple(services))