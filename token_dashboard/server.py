"""HTTP server: static frontend + JSON endpoints + SSE diff stream."""
from __future__ import annotations

import http.server
import json
import mimetypes
import queue
import threading
import time
import urllib.error
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from .db import (
    overview_totals, expensive_prompts, project_summary,
    tool_token_breakdown, recent_sessions, session_turns,
    daily_token_breakdown, model_breakdown, skill_breakdown,
    months_with_activity, project_model_costs, session_model_costs,
    workspaces_with_usage, last_admin_sync,
    upsert_workspaces, upsert_admin_usage, connect,
)
from .pricing import load_pricing, cost_for, format_allocation, format_for_user, get_plan, set_plan
from .tips import all_tips, dismiss_tip
from .scanner import scan_dir
from .skills import cached_catalog
from . import admin_usage as admin_usage_client


WEB_ROOT = Path(__file__).resolve().parent.parent / "web"
PRICING_JSON = Path(__file__).resolve().parent.parent / "pricing.json"

EVENTS: "queue.Queue[dict]" = queue.Queue()

MAX_POST_BYTES = 1_000_000  # 1 MB — we only accept tiny JSON bodies (plan, tip key)
MAX_LIMIT = 1000


def _send_json(handler, obj, status: int = 200) -> None:
    body = json.dumps(obj, default=str).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def _send_error(handler, status: int, msg: str) -> None:
    _send_json(handler, {"error": msg}, status=status)


def _clamp_limit(raw, default: int) -> int:
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return default
    return max(1, min(v, MAX_LIMIT))


def _serve_static(handler, rel: str) -> None:
    rel = rel.lstrip("/")
    p = (WEB_ROOT / rel).resolve()
    if not str(p).startswith(str(WEB_ROOT.resolve())) or not p.is_file():
        handler.send_response(404)
        handler.end_headers()
        return
    body = p.read_bytes()
    ctype, _ = mimetypes.guess_type(str(p))
    handler.send_response(200)
    handler.send_header("Content-Type", ctype or "application/octet-stream")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def build_handler(db_path: str, projects_dir: str):
    pricing = load_pricing(PRICING_JSON)

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def do_HEAD(self):
            return self.do_GET()

        def do_GET(self):
            url = urlparse(self.path)
            qs = parse_qs(url.query or "")
            path = url.path
            since = qs.get("since", [None])[0]
            until = qs.get("until", [None])[0]
            if path in ("/", "/index.html"):
                return _serve_static(self, "index.html")
            if path.startswith("/web/"):
                return _serve_static(self, path[5:])
            if path == "/api/overview":
                totals = overview_totals(db_path, since, until)
                cost_usd = 0.0
                for m in model_breakdown(db_path, since, until):
                    c = cost_for(m["model"], m, pricing)
                    if c["usd"] is not None:
                        cost_usd += c["usd"]
                cost_usd = round(cost_usd, 4)
                totals["cost_usd"] = cost_usd
                totals["cost_display"] = format_for_user(cost_usd, get_plan(db_path), pricing)
                return _send_json(self, totals)
            if path == "/api/prompts":
                limit = _clamp_limit(qs.get("limit", ["50"])[0], 50)
                sort = qs.get("sort", ["tokens"])[0]
                rows = expensive_prompts(db_path, limit=limit, sort=sort)
                for r in rows:
                    c = cost_for(r["model"], {
                        "input_tokens": 0, "output_tokens": 0,
                        "cache_read_tokens": r["cache_read_tokens"],
                        "cache_create_5m_tokens": 0, "cache_create_1h_tokens": 0,
                    }, pricing)
                    r["estimated_cost_usd"] = c["usd"]
                return _send_json(self, rows)
            if path == "/api/projects":
                rows = project_summary(db_path, since, until)
                costs_by_slug = project_model_costs(db_path, since, until)
                total_api_cost = 0.0
                for r in rows:
                    row_cost = 0.0
                    for m in costs_by_slug.get(r["project_slug"], []):
                        c = cost_for(m["model"], m, pricing)
                        if c["usd"] is not None:
                            row_cost += c["usd"]
                    r["api_cost_usd"] = row_cost
                    total_api_cost += row_cost
                plan = get_plan(db_path)
                months = months_with_activity(db_path, since, until)
                for r in rows:
                    r["cost_display"] = format_allocation(
                        r["api_cost_usd"], total_api_cost, plan, pricing, months,
                    )
                plan_info = pricing["plans"].get(plan, pricing["plans"]["api"])
                monthly = plan_info.get("monthly") or 0
                meta = {
                    "plan": plan,
                    "plan_label": plan_info.get("label", plan),
                    "monthly_fee": monthly,
                    "months_in_range": months,
                    "total_api_cost_usd": total_api_cost,
                    "total_paid_usd": float(monthly) * months,
                    "is_subscription": plan != "api" and monthly > 0,
                }
                return _send_json(self, {"rows": rows, "_meta": meta})
            if path == "/api/tools":
                return _send_json(self, tool_token_breakdown(db_path, since, until))
            if path == "/api/sessions":
                rows = recent_sessions(
                    db_path, limit=_clamp_limit(qs.get("limit", ["20"])[0], 20),
                    since=since, until=until,
                )
                ids = [r["session_id"] for r in rows]
                costs_by_sid = session_model_costs(db_path, session_ids=ids) if ids else {}
                # Denominator is the total across the full range, not just the
                # sessions in this page — otherwise small pages would over-
                # allocate per session.
                total_api_cost = 0.0
                for models in project_model_costs(db_path, since, until).values():
                    for m in models:
                        c = cost_for(m["model"], m, pricing)
                        if c["usd"] is not None:
                            total_api_cost += c["usd"]
                for r in rows:
                    row_cost = 0.0
                    for m in costs_by_sid.get(r["session_id"], []):
                        c = cost_for(m["model"], m, pricing)
                        if c["usd"] is not None:
                            row_cost += c["usd"]
                    r["api_cost_usd"] = row_cost
                plan = get_plan(db_path)
                months = months_with_activity(db_path, since, until)
                for r in rows:
                    r["cost_display"] = format_allocation(
                        r["api_cost_usd"], total_api_cost, plan, pricing, months,
                    )
                plan_info = pricing["plans"].get(plan, pricing["plans"]["api"])
                monthly = plan_info.get("monthly") or 0
                meta = {
                    "plan": plan,
                    "plan_label": plan_info.get("label", plan),
                    "monthly_fee": monthly,
                    "months_in_range": months,
                    "total_api_cost_usd": total_api_cost,
                    "total_paid_usd": float(monthly) * months,
                    "is_subscription": plan != "api" and monthly > 0,
                }
                return _send_json(self, {"rows": rows, "_meta": meta})
            if path == "/api/daily":
                return _send_json(self, daily_token_breakdown(db_path, since, until))
            if path == "/api/skills":
                rows = skill_breakdown(db_path, since, until)
                catalog = cached_catalog()
                for r in rows:
                    info = catalog.get(r["skill"])
                    r["tokens_per_call"] = info["tokens"] if info else None
                return _send_json(self, rows)
            if path == "/api/by-model":
                rows = model_breakdown(db_path, since, until)
                for r in rows:
                    c = cost_for(r["model"], r, pricing)
                    r["cost_usd"] = c["usd"]
                    r["cost_estimated"] = c["estimated"]
                return _send_json(self, rows)
            if path.startswith("/api/sessions/"):
                sid = path.rsplit("/", 1)[1]
                return _send_json(self, session_turns(db_path, sid))
            if path == "/api/tips":
                return _send_json(self, all_tips(db_path))
            if path == "/api/plan":
                return _send_json(self, {"plan": get_plan(db_path), "pricing": pricing})
            if path == "/api/workspaces":
                rows = workspaces_with_usage(db_path, since, until)
                return _send_json(self, {
                    "rows": rows,
                    "_meta": {"last_synced_at": last_admin_sync(db_path)},
                })
            if path == "/api/scan":
                n = scan_dir(projects_dir, db_path)
                return _send_json(self, n)
            if path == "/api/stream":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                while True:
                    try:
                        evt = EVENTS.get(timeout=15)
                        chunk = f"data: {json.dumps(evt, default=str)}\n\n".encode()
                    except queue.Empty:
                        chunk = b": ping\n\n"
                    try:
                        self.wfile.write(chunk)
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        return
            self.send_response(404)
            self.end_headers()

        def do_POST(self):
            url = urlparse(self.path)
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return _send_error(self, 400, "invalid Content-Length")
            if length < 0 or length > MAX_POST_BYTES:
                return _send_error(self, 413, f"body too large (max {MAX_POST_BYTES} bytes)")
            try:
                body = json.loads(self.rfile.read(length) or b"{}") if length else {}
            except json.JSONDecodeError:
                return _send_error(self, 400, "invalid JSON")
            if not isinstance(body, dict):
                return _send_error(self, 400, "body must be a JSON object")
            if url.path == "/api/plan":
                set_plan(db_path, body.get("plan", "api"))
                return _send_json(self, {"ok": True})
            if url.path == "/api/tips/dismiss":
                dismiss_tip(db_path, body.get("key", ""))
                return _send_json(self, {"ok": True})
            if url.path == "/api/workspaces/refresh":
                starting_at = body.get("starting_at")
                ending_at = body.get("ending_at")
                if not starting_at or not ending_at:
                    return _send_error(self, 400, "starting_at and ending_at are required (RFC 3339)")
                try:
                    workspaces = admin_usage_client.list_workspaces()
                    usage = admin_usage_client.fetch_usage(starting_at, ending_at)
                except admin_usage_client.MissingAdminKey as e:
                    return _send_error(self, 400, str(e))
                except urllib.error.HTTPError as e:
                    return _send_error(self, 502, f"Admin API error {e.code}: {e.reason}")
                except urllib.error.URLError as e:
                    return _send_error(self, 502, f"Admin API unreachable: {e.reason}")
                # Compute cost deterministically per usage row from pricing.json
                # rather than relying on cost_report. cost_report aggregates
                # org-wide and frequently returns workspace_id=null even when
                # usage is per-workspace, which makes any redistribution wrong.
                merged = []
                for u in usage:
                    c = cost_for(u.get("model") or "", u, pricing)
                    merged.append(dict(u, cost_usd=float(c["usd"] or 0.0)))
                # Anthropic returns workspace_id=null for org-level / default-
                # workspace activity (e.g., keys not scoped to a workspace).
                # Surface that as a synthetic row so its spend isn't hidden.
                if any((u.get("workspace_id") or "") == "" for u in merged):
                    if not any((w.get("id") or "") == "" for w in workspaces):
                        workspaces.append({
                            "id": "",
                            "name": "(Default / unattributed)",
                            "type": "synthetic",
                        })
                ts = time.time()
                with connect(db_path) as conn:
                    try:
                        n_ws = upsert_workspaces(conn, workspaces, ts)
                        n_rows = upsert_admin_usage(conn, merged, ts)
                        conn.commit()
                    except Exception:
                        conn.rollback()
                        raise
                EVENTS.put({
                    "type": "workspaces-refresh",
                    "n": {"workspaces": n_ws, "usage_rows": n_rows},
                    "ts": ts,
                })
                return _send_json(self, {
                    "ok": True,
                    "workspaces": n_ws,
                    "usage_rows": n_rows,
                    "last_synced_at": ts,
                })
            self.send_response(404)
            self.end_headers()

    return H


def _scan_loop(db_path: str, projects_dir: str, interval: float = 30.0):
    while True:
        try:
            n = scan_dir(projects_dir, db_path)
            if n["messages"] > 0:
                EVENTS.put({"type": "scan", "n": n, "ts": time.time()})
        except Exception as e:
            EVENTS.put({"type": "error", "message": str(e)})
        time.sleep(interval)


def run(host: str, port: int, db_path: str, projects_dir: str):
    threading.Thread(target=_scan_loop, args=(db_path, projects_dir), daemon=True).start()
    H = build_handler(db_path, projects_dir)
    httpd = http.server.ThreadingHTTPServer((host, port), H)
    httpd.serve_forever()
