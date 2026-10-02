import tempfile
import unittest
from pathlib import Path

from healer.config import ConfigError, load_config


def write(tmp, text):
    p = Path(tmp) / "c.yaml"
    p.write_text(text)
    return p


class ConfigTests(unittest.TestCase):
    def test_default_config_loads(self):
        cfg = load_config()
        self.assertEqual([s.name for s in cfg.services], ["web", "worker", "filewriter"])
        self.assertTrue(cfg.healer.db_path.is_absolute())

    def test_duplicate_names_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = write(tmp, "services:\n  - {name: a, command: [x]}\n  - {name: a, command: [y]}\n")
            with self.assertRaises(ConfigError):
                load_config(p)

    def test_bad_command_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = write(tmp, "services:\n  - {name: a, command: 'not a list'}\n")
            with self.assertRaises(ConfigError):
                load_config(p)

    def test_restart_defaults_and_per_service_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = write(tmp, "restart_defaults: {backoff_base: 2, max_failures: 3}\n"
                           "services:\n"
                           "  - {name: a, command: [x]}\n"
                           "  - {name: b, command: [y], restart: {policy: always, max_failures: 9}}\n")
            cfg = load_config(p)
        a, b = cfg.services
        self.assertEqual((a.restart.backoff_base, a.restart.max_failures, a.restart.policy),
                         (2.0, 3, "on-failure"))
        self.assertEqual((b.restart.backoff_base, b.restart.max_failures, b.restart.policy),
                         (2.0, 9, "always"))

    def test_invalid_restart_policy_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = write(tmp, "services:\n  - {name: a, command: [x], restart: {policy: sometimes}}\n")
            with self.assertRaises(ConfigError):
                load_config(p)


if __name__ == "__main__":
    unittest.main()