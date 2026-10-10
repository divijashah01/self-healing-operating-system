import importlib.util
import os
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


def load_service_module(name: str):
    """Import services/<name>.py (services are scripts, not a package)."""
    spec = importlib.util.spec_from_file_location(
        f"svc_{name}", PROJECT_ROOT / "services" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_checkpoints(root, service, states, keep=10):
    """Write checkpoints with the SERVICE-side helper, so tests prove both sides agree."""
    module = load_service_module("checkpoint")
    directory = Path(root) / service
    env = {"HEALER_CHECKPOINT_DIR": str(directory), "HEALER_CHECKPOINT_KEEP": str(keep)}
    with mock.patch.dict(os.environ, env):
        checkpointer = module.Checkpointer(service)
        return [checkpointer.save(state) for state in states]