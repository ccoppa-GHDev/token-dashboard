import http.server
import json
import os
import socket
import sqlite3
import tempfile
import threading
import unittest
import urllib.request

from token_dashboard.db import init_db
from token_dashboard.server import build_handler


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "t.db")
        init_db(self.db)
        with sqlite3.connect(self.db) as c:
            c.execute("INSERT INTO messages (uuid, parent_uuid, session_id, project_slug, type, timestamp, model, input_tokens, output_tokens, cache_read_tokens, cache_create_5m_tokens, cache_create_1h_tokens, prompt_text, prompt_chars) VALUES ('u',NULL,'s','p','user','2026-04-19T00:00:00Z',NULL,0,0,0,0,0,'hi',2)")
            c.execute("INSERT INTO messages (uuid, parent_uuid, session_id, project_slug, type, timestamp, model, input_tokens, output_tokens, cache_read_tokens, cache_create_5m_tokens, cache_create_1h_tokens) VALUES ('a','u','s','p','assistant','2026-04-19T00:00:01Z','claude-haiku-4-5',1,1,0,0,0)")
            c.commit()
        self.port = _free_port()
        H = build_handler(self.db, projects_dir="/nonexistent")
        self.httpd = http.server.HTTPServer(("127.0.0.1", self.port), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()

    def _get(self, path):
        return urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}").read()

    def test_index_html(self):
        body = self._get("/")
        self.assertIn(b"Token Dashboard", body)

    def test_overview_json(self):
        body = json.loads(self._get("/api/overview"))
        self.assertIn("sessions", body)
        self.assertEqual(body["sessions"], 1)

    def test_prompts_json(self):
        body = json.loads(self._get("/api/prompts?limit=10"))
        self.assertIsInstance(body, list)

    def test_projects_json_has_rows_and_meta(self):
        body = json.loads(self._get("/api/projects"))
        self.assertIn("rows", body)
        self.assertIn("_meta", body)
        self.assertEqual(body["rows"][0]["project_slug"], "p")
        self.assertIn("api_cost_usd", body["rows"][0])
        self.assertIn("cost_display", body["rows"][0])
        self.assertIn("months_in_range", body["_meta"])
        self.assertIn("total_api_cost_usd", body["_meta"])

    def test_projects_cost_changes_with_plan(self):
        def _post(path, payload):
            req = urllib.request.Request(
                f"http://127.0.0.1:{self.port}{path}",
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req).read()

        _post("/api/plan", {"plan": "api"})
        api_body = json.loads(self._get("/api/projects"))
        _post("/api/plan", {"plan": "max"})
        max_body = json.loads(self._get("/api/projects"))

        self.assertFalse(api_body["_meta"]["is_subscription"])
        self.assertTrue(max_body["_meta"]["is_subscription"])

        api_cost = api_body["rows"][0]["cost_display"]["display_usd"]
        max_cost = max_body["rows"][0]["cost_display"]["display_usd"]
        # On API plan the display is the raw API cost; on Max it's the allocated share.
        # With a single project in the fixture, Max allocates the full monthly fee × months.
        self.assertEqual(max_body["rows"][0]["cost_display"]["share_of_plan"], 1.0)
        self.assertNotEqual(api_cost, max_cost)

    def test_sessions_json_has_rows_and_meta(self):
        body = json.loads(self._get("/api/sessions?limit=5"))
        self.assertIn("rows", body)
        self.assertIn("_meta", body)
        self.assertIn("cost_display", body["rows"][0])

    def test_plan_json(self):
        body = json.loads(self._get("/api/plan"))
        self.assertIn("plan", body)
        self.assertIn("pricing", body)

    def test_head_returns_200_not_501(self):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/", method="HEAD")
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.read(), b"")

    def test_head_api_endpoint(self):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/overview", method="HEAD")
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.read(), b"")


if __name__ == "__main__":
    unittest.main()
