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
        self.assertEqual([s.name for s in cfg.services], ["web", "worker", "filewriter", "client"])
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

    def test_heartbeat_is_optional_and_parsed(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = write(tmp, "services:\n"
                           "  - {name: a, command: [x]}\n"
                           "  - {name: b, command: [y], heartbeat: {timeout: 3, term_grace: 1}}\n")
            a, b = load_config(p).services
        self.assertIsNone(a.heartbeat)
        self.assertEqual((b.heartbeat.timeout, b.heartbeat.term_grace, b.heartbeat.startup_grace),
                         (3.0, 1.0, 5.0))

    def test_invalid_heartbeat_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = write(tmp, "services:\n  - {name: a, command: [x], heartbeat: {timeout: 0}}\n")
            with self.assertRaises(ConfigError):
                load_config(p)

    def test_protected_files_parsed(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = write(tmp, "services:\n  - {name: a, command: [x]}\n"
                           "protected_files:\n"
                           "  - {path: data/x.conf, keep_versions: 3, validate: keyvalue}\n")
            pf = load_config(p).protected_files[0]
        self.assertTrue(pf.path.is_absolute())
        self.assertEqual((pf.mode, pf.keep_versions, pf.validate), ("restore", 3, "keyvalue"))

    def test_invalid_protected_file_rejected(self):
        bad_entries = ["{path: x, mode: nuke}", "{path: x, keep_versions: 0}",
                       "{path: x, validate: magic}", "{mode: restore}"]
        for bad in bad_entries:
            with tempfile.TemporaryDirectory() as tmp:
                p = write(tmp, f"services:\n  - {{name: a, command: [x]}}\n"
                               f"protected_files:\n  - {bad}\n")
                with self.assertRaises(ConfigError, msg=bad):
                    load_config(p)

    def test_checkpoint_policy_parsed(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = write(tmp, "services:\n"
                           "  - {name: a, command: [x], checkpoint: {keep: 3}}\n"
                           "  - {name: b, command: [y]}\n")
            a, b = load_config(p).services
        self.assertEqual((a.checkpoint.keep, a.checkpoint.trust_after), (3, 10.0))
        self.assertIsNone(b.checkpoint)

    def test_invalid_checkpoint_policy_rejected(self):
        for bad in ("{keep: 0}", "{trust_after: -1}", "{nope: 1}"):
            with tempfile.TemporaryDirectory() as tmp:
                p = write(tmp, f"services:\n  - {{name: a, command: [x], checkpoint: {bad}}}\n")
                with self.assertRaises(ConfigError, msg=bad):
                    load_config(p)

    def test_memory_limit_parsed_and_validated(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = write(tmp, "services:\n  - {name: a, command: [x], memory_limit_mb: 80}\n"
                           "  - {name: b, command: [y]}\n")
            a, b = load_config(p).services
        self.assertEqual((a.memory_limit_mb, b.memory_limit_mb), (80.0, None))
        with tempfile.TemporaryDirectory() as tmp:
            p = write(tmp, "services:\n  - {name: a, command: [x], memory_limit_mb: -5}\n")
            with self.assertRaises(ConfigError):
                load_config(p)

    def test_depends_on_parsed(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = write(tmp, "services:\n  - {name: db, command: [x]}\n"
                           "  - {name: app, command: [y], depends_on: [db]}\n")
            db, app = load_config(p).services
        self.assertEqual((db.depends_on, app.depends_on), ((), ("db",)))

    def test_bad_dependencies_rejected(self):
        bad_configs = [
            "  - {name: a, command: [x], depends_on: [ghost]}",
            "  - {name: a, command: [x], depends_on: [a]}",
            "  - {name: a, command: [x], depends_on: [b]}\n  - {name: b, command: [y], depends_on: [a]}",
        ]
        for services in bad_configs:
            with tempfile.TemporaryDirectory() as tmp:
                p = write(tmp, "services:\n" + services + "\n")
                with self.assertRaises(ConfigError, msg=services):
                    load_config(p)


if __name__ == "__main__":
    unittest.main()