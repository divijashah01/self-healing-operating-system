"""Load and validate the healer configuration."""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "healer.yaml"


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ServiceConfig:
    name: str
    command: tuple
    env: dict = field(default_factory=dict)


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
        services.append(ServiceConfig(name=name, command=tuple(command),
                                      env=dict(entry.get("env") or {})))
    if not services:
        raise ConfigError("no services configured")
    return Config(healer=settings, services=tuple(services))