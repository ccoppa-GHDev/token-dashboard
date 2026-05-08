"""Snapshot regression tests pinning every existing /api/* GET route shape.

Captured before the Workspaces-tab change so any later edit that disturbs an
existing route's response will fail loudly. Snapshot files live in
``tests/snapshots/`` and are checked into git. Re-record by deleting the
matching snapshot file and re-running the suite.
"""
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


SNAPSHOT_DIR = os.path.join(os.path.dirname(__file__), "snapshots")


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _seed(db_path: str) -> None:
    """Two projects, two sessions, three models, a tool call, a skill mention."""
    with sqlite3.connect(db_path) as c:
        c.executescript(
            """
            INSERT INTO messages
              (uuid, parent_uuid, session_id, project_slug, type, timestamp, model,
               input_tokens, output_tokens, cache_read_tokens,
               cache_create_5m_tokens, cache_create_1h_tokens,
               prompt_text, prompt_chars)
            VALUES
              ('u1', NULL, 's1', 'projA', 'user',      '2026-04-19T00:00:00Z', NULL,
               0, 0, 0, 0, 0, 'analyze the repo', 16),
              ('a1', 'u1', 's1', 'projA', 'assistant', '2026-04-19T00:00:01Z', 'claude-opus-4-7',
               100, 200, 300, 50, 25, NULL, NULL),
              ('u2', NULL, 's1', 'projA', 'user',      '2026-04-20T00:00:00Z', NULL,
               0, 0, 0, 0, 0, 'fix the bug',       11),
              ('a2', 'u2', 's1', 'projA', 'assistant', '2026-04-20T00:00:01Z', 'claude-sonnet-4-6',
               40, 80, 0, 10, 0, NULL, NULL),
              ('u3', NULL, 's2', 'projB', 'user',      '2026-04-21T00:00:00Z', NULL,
               0, 0, 0, 0, 0, 'summarize',         9),
              ('a3', 'u3', 's2', 'projB', 'assistant', '2026-04-21T00:00:01Z', 'claude-haiku-4-5',
               10, 20, 0, 0, 0, NULL, NULL);
            INSERT INTO tool_calls
              (message_uuid, session_id, project_slug, tool_name, target, result_tokens, timestamp)
            VALUES
              ('a1', 's1', 'projA', 'Read',  '/etc/hosts', 50, '2026-04-19T00:00:01Z'),
              ('a2', 's1', 'projA', 'Write', '/tmp/x.txt',  20, '2026-04-20T00:00:01Z');
            """
        )
        c.commit()


# Routes whose response shape we want to pin. /api/scan is mutating, /api/stream
# is SSE, and /api/sessions/<sid> uses an id we know from the seed fixture.
SNAPSHOT_ROUTES = [
    ("api_overview",          "/api/overview"),
    ("api_prompts",           "/api/prompts?limit=50"),
    ("api_projects",          "/api/projects"),
    ("api_sessions",          "/api/sessions?limit=20"),
    ("api_tools",             "/api/tools"),
    ("api_daily",             "/api/daily"),
    ("api_by_model",          "/api/by-model"),
    ("api_skills",            "/api/skills"),
    ("api_session_detail_s1", "/api/sessions/s1"),
    ("api_tips",              "/api/tips"),
    ("api_plan",              "/api/plan"),
]


class NoBreakageSnapshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.db = os.path.join(cls.tmp, "t.db")
        init_db(cls.db)
        _seed(cls.db)
        cls.port = _free_port()
        H = build_handler(cls.db, projects_dir="/nonexistent")
        cls.httpd = http.server.HTTPServer(("127.0.0.1", cls.port), H)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        os.makedirs(SNAPSHOT_DIR, exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def _get(self, path):
        return urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}").read()

    def _check(self, label: str, path: str) -> None:
        body = json.loads(self._get(path))
        snapshot_path = os.path.join(SNAPSHOT_DIR, f"{label}.json")
        if not os.path.exists(snapshot_path):
            with open(snapshot_path, "w") as f:
                json.dump(body, f, indent=2, sort_keys=True)
            self.fail(
                f"recorded snapshot {snapshot_path} (re-run the test to verify replay)"
            )
        with open(snapshot_path) as f:
            expected = json.load(f)
        self.assertEqual(
            body, expected,
            f"\n{path} response drifted from snapshot {snapshot_path}.\n"
            "If this is intentional, delete the snapshot file and re-run.",
        )


def _make_test(label: str, path: str):
    def t(self):
        self._check(label, path)
    t.__name__ = f"test_{label}"
    return t


for _label, _path in SNAPSHOT_ROUTES:
    setattr(NoBreakageSnapshotTests, f"test_{_label}", _make_test(_label, _path))


if __name__ == "__main__":
    unittest.main()
