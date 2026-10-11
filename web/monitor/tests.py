import json
import tempfile
import time
from pathlib import Path

from django.test import SimpleTestCase, override_settings

from healer.eventlog import EventLog
from monitor import services

CONFIG = """\
healer:
  data_dir: {data}
  db_path: {data}/healer.db
services:
  - {{name: a, command: [x], checkpoint: {{}}}}
protected_files:
  - {{path: {data}/app.conf, validate: keyvalue}}
"""


class DashboardTests(SimpleTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = Path(tmp.name)
        (self.data / "app.conf").write_text("mode=production\n")
        config = self.data / "healer.yaml"
        config.write_text(CONFIG.format(data=self.data))
        override = override_settings(HEALER_CONFIG_PATH=str(config))
        override.enable()
        self.addCleanup(override.disable)
        self.addCleanup(services.close_all)
        self.log = EventLog(self.data / "healer.db")
        self.addCleanup(self.log.close)

    def post(self, body):
        return self.client.post("/api/command", data=body, content_type="application/json")

    def test_state_api_lists_services_and_reports_healer_down(self):
        self.log.set_state("a", "running", pid=7)
        data = self.client.get("/api/state").json()
        self.assertEqual(data["services"][0]["name"], "a")
        self.assertFalse(data["healer"]["running"])

    def test_healer_counts_as_running_after_a_fresh_heartbeat(self):
        self.log.set_meta("healer_last_seen", str(time.time()))
        self.assertTrue(self.client.get("/api/state").json()["healer"]["running"])

    def test_command_is_queued_for_the_healer(self):
        resp = self.post(json.dumps({"action": "restart_service", "target": "a"}))
        self.assertEqual(resp.status_code, 200)
        queued = self.log.pending_commands()
        self.assertEqual((queued[0]["action"], queued[0]["target"]), ("restart_service", "a"))

    def test_malformed_commands_are_rejected(self):
        self.assertEqual(self.post("not json").status_code, 400)
        self.assertEqual(self.post(json.dumps({"action": "format_disk", "target": "a"})).status_code, 400)
        self.assertEqual(self.post(json.dumps({"action": "restart_service"})).status_code, 400)
        self.assertEqual(self.log.pending_commands(), [])

    def test_command_status_endpoint(self):
        command_id = self.log.enqueue_command("restart_service", "a")
        self.log.finish_command(command_id, True, "restarted a")
        data = self.client.get(f"/api/command/{command_id}").json()
        self.assertEqual((data["status"], data["result"]), ("done", "restarted a"))
        self.assertEqual(self.client.get("/api/command/9999").status_code, 404)

    def test_pages_render(self):
        for url in ("/", "/files/", "/checkpoints/", "/chaos/"):
            self.assertEqual(self.client.get(url).status_code, 200, url)
        self.assertContains(self.client.get("/files/"), "app.conf")

    def test_healing_flag_defaults_on_and_follows_the_switch(self):
        self.assertTrue(self.client.get("/api/state").json()["healer"]["healing"])
        self.log.set_meta("healing", "off")
        self.assertFalse(self.client.get("/api/state").json()["healer"]["healing"])

    def test_chaos_page_shows_the_last_comparison(self):
        self.log.set_meta("last_comparison", json.dumps({
            "ts": time.time(), "off_window": 15, "timeout": 40,
            "rows": [{"fault": "worker killed", "off": None, "on": 1.9}]}))
        page = self.client.get("/chaos/")
        self.assertContains(page, "worker killed")
        self.assertContains(page, "1.9")