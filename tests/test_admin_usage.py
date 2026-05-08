"""Tests for the Admin Usage API client and the workspace DB helpers."""
import io
import json
import os
import sqlite3
import tempfile
import time
import unittest
import urllib.error
from unittest import mock

from token_dashboard import admin_usage
from token_dashboard.admin_usage import (
    MissingAdminKey,
    fetch_usage,
    list_workspaces,
)
from token_dashboard.db import (
    init_db,
    last_admin_sync,
    upsert_admin_usage,
    upsert_workspaces,
    workspaces_with_usage,
)
from token_dashboard.pricing import cost_for, load_pricing


def _http_response(payload: dict, status: int = 200):
    """Build a fake context manager mimicking urllib.request.urlopen()."""
    body = json.dumps(payload).encode("utf-8")
    cm = mock.MagicMock()
    cm.__enter__ = mock.MagicMock(
        return_value=mock.MagicMock(read=mock.MagicMock(return_value=body))
    )
    cm.__exit__ = mock.MagicMock(return_value=False)
    return cm


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="x", code=code, msg="err", hdrs=None, fp=io.BytesIO(b"")
    )


class AdminKeyTests(unittest.TestCase):
    def test_missing_key_raises_actionable_error(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(MissingAdminKey) as ctx:
                list_workspaces()
            self.assertIn("ANTHROPIC_ADMIN_API_KEY", str(ctx.exception))
            self.assertIn("console.anthropic.com", str(ctx.exception))


class ListWorkspacesTests(unittest.TestCase):
    def test_single_page(self):
        with mock.patch.object(admin_usage, "_urlopen") as up:
            up.return_value = _http_response({
                "data": [{"id": "wrk_1", "name": "Default"}],
                "has_more": False,
            })
            rows = list_workspaces(key="sk-ant-admin-test")
        self.assertEqual([r["id"] for r in rows], ["wrk_1"])

    def test_paginates(self):
        pages = [
            _http_response({"data": [{"id": "wrk_1"}], "has_more": True, "next_page": "c2"}),
            _http_response({"data": [{"id": "wrk_2"}], "has_more": False}),
        ]
        with mock.patch.object(admin_usage, "_urlopen", side_effect=pages):
            rows = list_workspaces(key="sk-ant-admin-test")
        self.assertEqual([r["id"] for r in rows], ["wrk_1", "wrk_2"])

    def test_empty_org_returns_empty(self):
        with mock.patch.object(admin_usage, "_urlopen") as up:
            up.return_value = _http_response({"data": [], "has_more": False})
            rows = list_workspaces(key="sk-ant-admin-test")
        self.assertEqual(rows, [])


class FetchUsageTests(unittest.TestCase):
    def test_flattens_buckets_and_results(self):
        # Field names match the Admin Usage Report schema: input is
        # `uncached_input_tokens` (not `input_tokens`), and cache creation is a
        # nested object split by TTL. We normalize the API field names to the
        # `messages` table convention so `pricing.cost_for` works on these rows.
        payload = {
            "data": [
                {
                    "starting_at": "2026-05-01T00:00:00Z",
                    "results": [
                        {
                            "workspace_id": "wrk_1", "api_key_id": "key_1",
                            "model": "claude-opus-4-7", "service_tier": "standard",
                            "uncached_input_tokens": 100, "output_tokens": 200,
                            "cache_read_input_tokens": 50,
                            "cache_creation": {
                                "ephemeral_5m_input_tokens": 8,
                                "ephemeral_1h_input_tokens": 2,
                            },
                        },
                        {
                            "workspace_id": "wrk_1", "api_key_id": "key_1",
                            "model": "claude-haiku-4-5", "service_tier": "standard",
                            "uncached_input_tokens": 5, "output_tokens": 8,
                            "cache_read_input_tokens": 0,
                            "cache_creation": {
                                "ephemeral_5m_input_tokens": 0,
                                "ephemeral_1h_input_tokens": 0,
                            },
                        },
                    ],
                },
            ],
            "has_more": False,
        }
        with mock.patch.object(admin_usage, "_urlopen") as up:
            up.return_value = _http_response(payload)
            rows = fetch_usage("2026-05-01T00:00:00Z", "2026-05-08T00:00:00Z", key="sk-ant-admin-test")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["bucket_start"], "2026-05-01T00:00:00Z")
        self.assertEqual(rows[0]["model"], "claude-opus-4-7")
        self.assertEqual(rows[0]["input_tokens"], 100)
        # The TTL sub-fields are preserved separately so pricing.cost_for can
        # apply different rates to 5m vs 1h cache writes.
        self.assertEqual(rows[0]["cache_create_5m_tokens"], 8)
        self.assertEqual(rows[0]["cache_create_1h_tokens"], 2)
        self.assertEqual(rows[0]["cache_read_tokens"], 50)

    def test_invalid_bucket_width_raises(self):
        with self.assertRaises(ValueError):
            fetch_usage("2026-05-01T00:00:00Z", "2026-05-08T00:00:00Z", bucket_width="2d", key="x")

    def test_429_retries_then_succeeds(self):
        responses = [
            _http_error(429),
            _http_response({"data": [], "has_more": False}),
        ]
        with mock.patch.object(admin_usage, "RETRY_BACKOFF_SECONDS", 0.0):
            with mock.patch.object(admin_usage, "_urlopen", side_effect=responses):
                rows = fetch_usage("2026-05-01T00:00:00Z", "2026-05-08T00:00:00Z", key="x")
        self.assertEqual(rows, [])

    def test_429_twice_surfaces_error(self):
        with mock.patch.object(admin_usage, "RETRY_BACKOFF_SECONDS", 0.0):
            with mock.patch.object(
                admin_usage, "_urlopen",
                side_effect=[_http_error(429), _http_error(429)],
            ):
                with self.assertRaises(urllib.error.HTTPError):
                    fetch_usage("2026-05-01T00:00:00Z", "2026-05-08T00:00:00Z", key="x")


class CostFromPricingTests(unittest.TestCase):
    """Verifies that flattened admin-usage rows can feed pricing.cost_for
    directly — i.e. that field names match the messages-table convention.
    """

    @classmethod
    def setUpClass(cls):
        cls.pricing = load_pricing(
            os.path.join(os.path.dirname(__file__), "..", "pricing.json")
        )

    def test_cost_for_handles_flattened_admin_row(self):
        # Mirror the field shape produced by admin_usage._flatten_usage.
        # Opus 4.7 rates per current pricing.json: 5 / 25 / 0.50 / 6.25 / 10.
        row = {
            "model": "claude-opus-4-7",
            "input_tokens": 1_000_000,            # $5
            "output_tokens": 1_000_000,           # $25
            "cache_read_tokens": 1_000_000,       # $0.50
            "cache_create_5m_tokens": 1_000_000,  # $6.25
            "cache_create_1h_tokens": 1_000_000,  # $10
        }
        c = cost_for(row["model"], row, self.pricing)
        # 5 + 25 + 0.50 + 6.25 + 10 = 46.75
        self.assertAlmostEqual(c["usd"], 46.75, places=2)

    def test_cost_for_per_model_rates_differ(self):
        haiku_row = {
            "model": "claude-haiku-4-5",
            "input_tokens": 1_000_000, "output_tokens": 1_000_000,
            "cache_read_tokens": 0,
            "cache_create_5m_tokens": 0, "cache_create_1h_tokens": 0,
        }
        opus_row = dict(haiku_row, model="claude-opus-4-7")
        sonnet_row = dict(haiku_row, model="claude-sonnet-4-6")
        haiku = cost_for(haiku_row["model"], haiku_row, self.pricing)
        sonnet = cost_for(sonnet_row["model"], sonnet_row, self.pricing)
        opus = cost_for(opus_row["model"], opus_row, self.pricing)
        # Tier ordering should hold: haiku < sonnet < opus.
        self.assertLess(haiku["usd"], sonnet["usd"])
        self.assertLess(sonnet["usd"], opus["usd"])


class WorkspaceDbTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "t.db")
        init_db(self.db)

    def test_upsert_workspaces_idempotent(self):
        ws = [
            {"id": "wrk_1", "name": "Default", "type": "default", "created_at": "2026-01-01T00:00:00Z"},
            {"id": "wrk_2", "name": "Client A", "type": "regular", "created_at": "2026-02-01T00:00:00Z"},
        ]
        with sqlite3.connect(self.db) as c:
            self.assertEqual(upsert_workspaces(c, ws, sync_ts=time.time()), 2)
            self.assertEqual(upsert_workspaces(c, ws, sync_ts=time.time()), 2)
            c.commit()
            n = c.execute("SELECT COUNT(*) FROM workspaces").fetchone()[0]
        self.assertEqual(n, 2)

    def test_upsert_admin_usage_idempotent(self):
        rows = [
            {"workspace_id": "wrk_1", "api_key_id": "k1", "model": "claude-opus-4-7",
             "service_tier": "standard", "bucket_start": "2026-05-01T00:00:00Z",
             "input_tokens": 100, "output_tokens": 200,
             "cache_read_tokens": 50,
             "cache_create_5m_tokens": 8, "cache_create_1h_tokens": 2,
             "cost_usd": 1.23},
        ]
        with sqlite3.connect(self.db) as c:
            self.assertEqual(upsert_admin_usage(c, rows, sync_ts=time.time()), 1)
            self.assertEqual(upsert_admin_usage(c, rows, sync_ts=time.time()), 1)
            c.commit()
            n = c.execute("SELECT COUNT(*) FROM admin_usage").fetchone()[0]
        self.assertEqual(n, 1)

    def test_workspaces_with_usage_joins_and_zeroes(self):
        with sqlite3.connect(self.db) as c:
            upsert_workspaces(c, [
                {"id": "wrk_1", "name": "A"},
                {"id": "wrk_2", "name": "B"},
            ], sync_ts=time.time())
            upsert_admin_usage(c, [
                {"workspace_id": "wrk_1", "api_key_id": "k", "model": "m",
                 "service_tier": "standard", "bucket_start": "2026-05-01T00:00:00Z",
                 "input_tokens": 10, "output_tokens": 20,
                 "cache_read_tokens": 0,
                 "cache_create_5m_tokens": 0, "cache_create_1h_tokens": 0,
                 "cost_usd": 4.20},
            ], sync_ts=time.time())
            c.commit()
        rows = workspaces_with_usage(self.db)
        by_id = {r["workspace_id"]: r for r in rows}
        self.assertAlmostEqual(by_id["wrk_1"]["cost_usd"], 4.20, places=2)
        self.assertEqual(by_id["wrk_1"]["input_tokens"], 10)
        self.assertEqual(by_id["wrk_2"]["input_tokens"], 0)
        self.assertEqual(by_id["wrk_2"]["cost_usd"], 0.0)

    def test_last_admin_sync_none_when_empty(self):
        self.assertIsNone(last_admin_sync(self.db))

    def test_last_admin_sync_returns_max_ts(self):
        with sqlite3.connect(self.db) as c:
            upsert_workspaces(c, [{"id": "wrk_1", "name": "A"}], sync_ts=100.0)
            upsert_workspaces(c, [{"id": "wrk_1", "name": "A"}], sync_ts=200.0)
            c.commit()
        self.assertEqual(last_admin_sync(self.db), 200.0)


class WorkspacesServerRouteTests(unittest.TestCase):
    """End-to-end route tests with mocked Admin API client."""

    def setUp(self):
        import http.server
        import socket
        import threading
        from token_dashboard.server import build_handler

        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "t.db")
        init_db(self.db)
        s = socket.socket(); s.bind(("127.0.0.1", 0))
        self.port = s.getsockname()[1]; s.close()
        H = build_handler(self.db, projects_dir="/nonexistent")
        self.httpd = http.server.HTTPServer(("127.0.0.1", self.port), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()

    def _post(self, path: str, body: dict):
        import urllib.request
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            return urllib.request.urlopen(req).read(), 200
        except urllib.error.HTTPError as e:
            return e.read(), e.code

    def _get(self, path: str):
        import urllib.request
        return urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}").read()

    def test_workspaces_get_empty_until_refresh(self):
        body = json.loads(self._get("/api/workspaces"))
        self.assertEqual(body["rows"], [])
        self.assertIsNone(body["_meta"]["last_synced_at"])

    def test_refresh_missing_admin_key_returns_400(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            data, code = self._post("/api/workspaces/refresh", {
                "starting_at": "2026-05-01T00:00:00Z",
                "ending_at": "2026-05-08T00:00:00Z",
            })
        self.assertEqual(code, 400)
        self.assertIn("ANTHROPIC_ADMIN_API_KEY", json.loads(data)["error"])

    def test_refresh_missing_dates_returns_400(self):
        data, code = self._post("/api/workspaces/refresh", {})
        self.assertEqual(code, 400)
        self.assertIn("starting_at", json.loads(data)["error"])

    def test_refresh_success_populates_workspaces(self):
        # The refresh route now makes only TWO admin-API calls (workspaces +
        # usage). Cost is computed from pricing.json per row.
        ws_payload = _http_response({
            "data": [{"id": "wrk_1", "name": "Default", "type": "default"}],
            "has_more": False,
        })
        usage_payload = _http_response({
            "data": [{
                "starting_at": "2026-05-01T00:00:00Z",
                "results": [{
                    "workspace_id": "wrk_1", "api_key_id": "k", "model": "claude-opus-4-7",
                    "service_tier": "standard",
                    "uncached_input_tokens": 1_000_000, "output_tokens": 0,
                    "cache_read_input_tokens": 0,
                    "cache_creation": {
                        "ephemeral_5m_input_tokens": 0,
                        "ephemeral_1h_input_tokens": 0,
                    },
                }],
            }],
            "has_more": False,
        })
        with mock.patch.dict(os.environ, {"ANTHROPIC_ADMIN_API_KEY": "sk-ant-admin-test"}):
            with mock.patch.object(
                admin_usage, "_urlopen",
                side_effect=[ws_payload, usage_payload],
            ):
                data, code = self._post("/api/workspaces/refresh", {
                    "starting_at": "2026-05-01T00:00:00Z",
                    "ending_at": "2026-05-08T00:00:00Z",
                })
        self.assertEqual(code, 200)
        body = json.loads(data)
        self.assertEqual(body["workspaces"], 1)
        self.assertEqual(body["usage_rows"], 1)

        # Now /api/workspaces returns the synced row. Opus 4.7 input is $5/M,
        # so 1M uncached input tokens = $5 exactly. This proves cost is
        # computed deterministically from pricing.json at sync time, not from
        # a cost_report split.
        listed = json.loads(self._get("/api/workspaces"))
        self.assertEqual(len(listed["rows"]), 1)
        self.assertEqual(listed["rows"][0]["workspace_id"], "wrk_1")
        self.assertAlmostEqual(listed["rows"][0]["cost_usd"], 5.00, places=2)
        self.assertIsNotNone(listed["_meta"]["last_synced_at"])

    def test_refresh_http_error_does_not_corrupt_db(self):
        ws_payload = _http_response({"data": [{"id": "wrk_1", "name": "A"}], "has_more": False})
        with mock.patch.dict(os.environ, {"ANTHROPIC_ADMIN_API_KEY": "sk-ant-admin-test"}):
            with mock.patch.object(admin_usage, "RETRY_BACKOFF_SECONDS", 0.0):
                with mock.patch.object(
                    admin_usage, "_urlopen",
                    side_effect=[ws_payload, _http_error(500), _http_error(500)],
                ):
                    data, code = self._post("/api/workspaces/refresh", {
                        "starting_at": "2026-05-01T00:00:00Z",
                        "ending_at": "2026-05-08T00:00:00Z",
                    })
        self.assertEqual(code, 502)
        # No partial writes
        with sqlite3.connect(self.db) as c:
            ws_count = c.execute("SELECT COUNT(*) FROM workspaces").fetchone()[0]
            usage_count = c.execute("SELECT COUNT(*) FROM admin_usage").fetchone()[0]
        self.assertEqual(ws_count, 0)
        self.assertEqual(usage_count, 0)


if __name__ == "__main__":
    unittest.main()
