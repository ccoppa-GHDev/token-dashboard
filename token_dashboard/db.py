"""SQLite schema, connection, and shared query helpers."""
from __future__ import annotations

import os
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Optional, Union

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
  path        TEXT PRIMARY KEY,
  mtime       REAL    NOT NULL,
  bytes_read  INTEGER NOT NULL,
  scanned_at  REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
  uuid                    TEXT PRIMARY KEY,
  parent_uuid             TEXT,
  session_id              TEXT NOT NULL,
  project_slug            TEXT NOT NULL,
  cwd                     TEXT,
  git_branch              TEXT,
  cc_version              TEXT,
  entrypoint              TEXT,
  type                    TEXT NOT NULL,
  is_sidechain            INTEGER NOT NULL DEFAULT 0,
  agent_id                TEXT,
  timestamp               TEXT NOT NULL,
  model                   TEXT,
  stop_reason             TEXT,
  prompt_id               TEXT,
  message_id              TEXT,
  input_tokens            INTEGER NOT NULL DEFAULT 0,
  output_tokens           INTEGER NOT NULL DEFAULT 0,
  cache_read_tokens       INTEGER NOT NULL DEFAULT 0,
  cache_create_5m_tokens  INTEGER NOT NULL DEFAULT 0,
  cache_create_1h_tokens  INTEGER NOT NULL DEFAULT 0,
  prompt_text             TEXT,
  prompt_chars            INTEGER,
  tool_calls_json         TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_session   ON messages(session_id);
CREATE INDEX IF NOT EXISTS idx_messages_project   ON messages(project_slug);
CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp);
CREATE INDEX IF NOT EXISTS idx_messages_model     ON messages(model);
CREATE INDEX IF NOT EXISTS idx_messages_msgid     ON messages(session_id, message_id);

CREATE TABLE IF NOT EXISTS tool_calls (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  message_uuid  TEXT    NOT NULL,
  session_id    TEXT    NOT NULL,
  project_slug  TEXT    NOT NULL,
  tool_name     TEXT    NOT NULL,
  target        TEXT,
  result_tokens INTEGER,
  is_error      INTEGER NOT NULL DEFAULT 0,
  timestamp     TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tools_session ON tool_calls(session_id);
CREATE INDEX IF NOT EXISTS idx_tools_name    ON tool_calls(tool_name);
CREATE INDEX IF NOT EXISTS idx_tools_target  ON tool_calls(target);

CREATE TABLE IF NOT EXISTS plan (
  k TEXT PRIMARY KEY,
  v TEXT
);

CREATE TABLE IF NOT EXISTS dismissed_tips (
  tip_key       TEXT PRIMARY KEY,
  dismissed_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS workspaces (
  workspace_id    TEXT PRIMARY KEY,
  name            TEXT,
  display_color   TEXT,
  type            TEXT,
  created_at      TEXT,
  archived_at     TEXT,
  last_synced_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS admin_usage (
  workspace_id            TEXT NOT NULL,
  api_key_id              TEXT NOT NULL,
  model                   TEXT NOT NULL,
  service_tier            TEXT NOT NULL,
  bucket_start            TEXT NOT NULL,
  input_tokens            INTEGER NOT NULL DEFAULT 0,  -- uncached input
  output_tokens           INTEGER NOT NULL DEFAULT 0,
  cache_read_tokens       INTEGER NOT NULL DEFAULT 0,
  cache_create_5m_tokens  INTEGER NOT NULL DEFAULT 0,
  cache_create_1h_tokens  INTEGER NOT NULL DEFAULT 0,
  cost_usd                REAL    NOT NULL DEFAULT 0,  -- computed via pricing.cost_for at sync time
  last_synced_at          REAL    NOT NULL,
  PRIMARY KEY (workspace_id, api_key_id, model, service_tier, bucket_start)
);
CREATE INDEX IF NOT EXISTS idx_admin_usage_workspace ON admin_usage(workspace_id);
CREATE INDEX IF NOT EXISTS idx_admin_usage_bucket    ON admin_usage(bucket_start);
"""


def default_db_path() -> Path:
    return Path.home() / ".claude" / "token-dashboard.db"


def init_db(path: Union[str, Path]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as c:
        # WAL mode lets the scanner write concurrently with API readers.
        # Without it, the default rollback journal serializes writers against
        # readers and the dashboard's six parallel boot queries hang behind
        # the scanner's initial-scan transaction. WAL is a persistent
        # per-database property — setting it once is enough.
        c.execute("PRAGMA journal_mode = WAL")
        c.execute("PRAGMA synchronous = NORMAL")
        _migrate_add_message_id(c)
        c.executescript(SCHEMA)


def _migrate_add_message_id(conn) -> None:
    """Add messages.message_id for streaming-snapshot dedup.

    Why: pre-migration rows were summed from all streaming snapshots (over-count).
    How to apply: if the old table exists without the column, add it and clear
    messages/tool_calls/files so the next scan replays JSONLs cleanly. Source
    of truth is on disk; rescanning is cheap.
    """
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages'"
    ).fetchone()
    if not has_table:
        return
    cols = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
    if "message_id" in cols:
        return
    conn.execute("ALTER TABLE messages ADD COLUMN message_id TEXT")
    conn.execute("DELETE FROM messages")
    conn.execute("DELETE FROM tool_calls")
    conn.execute("DELETE FROM files")
    conn.commit()


@contextmanager
def connect(path: Union[str, Path]):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


def _range_clause(since, until, col: str = "timestamp"):
    where, args = [], []
    if since:
        where.append(f"{col} >= ?"); args.append(since)
    if until:
        where.append(f"{col} < ?"); args.append(until)
    return ((" AND " + " AND ".join(where)) if where else "", args)


def _encode_slug(path: str) -> str:
    """Claude Code's project-slug encoding: each of `:`, `\\`, `/`, space → one `-`."""
    return re.sub(r"[:\\/ ]", "-", path)


def _walk_to_root(cwd: str, slug: str) -> Optional[str]:
    """If any ancestor of cwd encodes to slug, return that ancestor's basename."""
    if not cwd or not slug:
        return None
    trimmed = cwd.rstrip("/\\")
    sep = "\\" if "\\" in trimmed else "/"
    parts = trimmed.split(sep)
    for i in range(len(parts), 0, -1):
        if _encode_slug(sep.join(parts[:i])) == slug:
            name = parts[i - 1]
            if name:
                return name
    return None


def project_name_for(cwd: Optional[str], fallback_slug: str) -> str:
    """Pretty project name from a single cwd + slug (best-effort).

    For the multi-cwd case, prefer `best_project_name`.
    """
    name = _walk_to_root(cwd or "", fallback_slug or "")
    if name:
        return name
    if cwd:
        trimmed = cwd.rstrip("/\\")
        sep = "\\" if "\\" in trimmed else "/"
        tail = trimmed.split(sep)[-1]
        if tail:
            return tail
    if fallback_slug:
        parts = [p for p in re.split(r"-+", fallback_slug) if p]
        if parts:
            return parts[-1]
    return fallback_slug or ""


def best_project_name(cwds, slug: str) -> str:
    """Pick a pretty name from a list of cwds.

    Prefer a cwd whose walk-up matches `slug` (a true descendant of the project
    root). If none match, fall back to `project_name_for` on the first cwd,
    then to the slug's last segment.
    """
    cwds = [c for c in (cwds or []) if c]
    for cwd in cwds:
        name = _walk_to_root(cwd, slug)
        if name:
            return name
    return project_name_for(cwds[0] if cwds else None, slug)


def overview_totals(db_path, since=None, until=None) -> dict:
    rng, args = _range_clause(since, until)
    sql = f"""
      SELECT COUNT(DISTINCT session_id) AS sessions,
             SUM(CASE WHEN type='user' THEN 1 ELSE 0 END) AS turns,
             COALESCE(SUM(input_tokens),0)            AS input_tokens,
             COALESCE(SUM(output_tokens),0)           AS output_tokens,
             COALESCE(SUM(cache_read_tokens),0)       AS cache_read_tokens,
             COALESCE(SUM(cache_create_5m_tokens),0)  AS cache_create_5m_tokens,
             COALESCE(SUM(cache_create_1h_tokens),0)  AS cache_create_1h_tokens
        FROM messages WHERE 1=1 {rng}
    """
    with connect(db_path) as c:
        return dict(c.execute(sql, args).fetchone())


def months_with_activity(db_path, since=None, until=None) -> int:
    """Distinct YYYY-MM months with at least one message in range. Min 1."""
    rng, args = _range_clause(since, until)
    sql = f"""
      SELECT COUNT(DISTINCT strftime('%Y-%m', timestamp)) AS months
        FROM messages
       WHERE timestamp IS NOT NULL {rng}
    """
    with connect(db_path) as c:
        row = c.execute(sql, args).fetchone()
    return max(1, int(row["months"] or 0))


def project_model_costs(db_path, since=None, until=None) -> dict:
    """Per-(project, model) token aggregates for API-cost computation.

    Returns {project_slug: [{model, input_tokens, output_tokens, cache_read_tokens,
                             cache_create_5m_tokens, cache_create_1h_tokens}, ...]}.
    """
    rng, args = _range_clause(since, until)
    sql = f"""
      SELECT project_slug,
             COALESCE(model, 'unknown') AS model,
             COALESCE(SUM(input_tokens),0)            AS input_tokens,
             COALESCE(SUM(output_tokens),0)           AS output_tokens,
             COALESCE(SUM(cache_read_tokens),0)       AS cache_read_tokens,
             COALESCE(SUM(cache_create_5m_tokens),0)  AS cache_create_5m_tokens,
             COALESCE(SUM(cache_create_1h_tokens),0)  AS cache_create_1h_tokens
        FROM messages
       WHERE type = 'assistant' {rng}
       GROUP BY project_slug, model
    """
    result: dict = {}
    with connect(db_path) as c:
        for r in c.execute(sql, args):
            result.setdefault(r["project_slug"], []).append(dict(r))
    return result


def session_model_costs(db_path, session_ids=None) -> dict:
    """Per-(session, model) token aggregates for API-cost computation.

    Optionally restricted to a caller-supplied list of session ids to avoid
    scanning every session when the caller only needs the top N.
    Returns {session_id: [{model, input_tokens, ...}, ...]}.
    """
    args: list = []
    where = "type = 'assistant'"
    if session_ids:
        placeholders = ",".join("?" * len(session_ids))
        where += f" AND session_id IN ({placeholders})"
        args.extend(session_ids)
    sql = f"""
      SELECT session_id,
             COALESCE(model, 'unknown') AS model,
             COALESCE(SUM(input_tokens),0)            AS input_tokens,
             COALESCE(SUM(output_tokens),0)           AS output_tokens,
             COALESCE(SUM(cache_read_tokens),0)       AS cache_read_tokens,
             COALESCE(SUM(cache_create_5m_tokens),0)  AS cache_create_5m_tokens,
             COALESCE(SUM(cache_create_1h_tokens),0)  AS cache_create_1h_tokens
        FROM messages
       WHERE {where}
       GROUP BY session_id, model
    """
    result: dict = {}
    with connect(db_path) as c:
        for r in c.execute(sql, args):
            result.setdefault(r["session_id"], []).append(dict(r))
    return result


def expensive_prompts(db_path, limit: int = 50, sort: str = "tokens") -> list:
    """User prompt joined with the immediately-following assistant turn's tokens.

    sort="tokens" (default) → largest billable first.
    sort="recent"           → newest first.
    """
    order = "u.timestamp DESC" if sort == "recent" else "billable_tokens DESC"
    sql = f"""
      SELECT u.uuid AS user_uuid, u.session_id, u.project_slug, u.timestamp,
             u.prompt_text, u.prompt_chars,
             a.uuid AS assistant_uuid, a.model,
             COALESCE(a.input_tokens,0)+COALESCE(a.output_tokens,0)
               +COALESCE(a.cache_create_5m_tokens,0)+COALESCE(a.cache_create_1h_tokens,0) AS billable_tokens,
             COALESCE(a.cache_read_tokens,0) AS cache_read_tokens
        FROM messages u
        JOIN messages a ON a.parent_uuid = u.uuid AND a.type='assistant'
       WHERE u.type='user' AND u.prompt_text IS NOT NULL
       ORDER BY {order}
       LIMIT ?
    """
    with connect(db_path) as c:
        return [dict(r) for r in c.execute(sql, (limit,))]


def project_summary(db_path, since=None, until=None) -> list:
    rng, args = _range_clause(since, until)
    sql = f"""
      SELECT project_slug,
             COUNT(DISTINCT session_id) AS sessions,
             SUM(CASE WHEN type='user' THEN 1 ELSE 0 END) AS turns,
             COALESCE(SUM(input_tokens), 0)  AS input_tokens,
             COALESCE(SUM(output_tokens), 0) AS output_tokens,
             SUM(input_tokens)+SUM(output_tokens)
               +SUM(cache_create_5m_tokens)+SUM(cache_create_1h_tokens) AS billable_tokens,
             SUM(cache_read_tokens) AS cache_read_tokens
        FROM messages m
       WHERE 1=1 {rng}
       GROUP BY project_slug
       ORDER BY billable_tokens DESC
    """
    with connect(db_path) as c:
        rows = [dict(r) for r in c.execute(sql, args)]
        for r in rows:
            cwds = [row["cwd"] for row in c.execute(
                "SELECT DISTINCT cwd FROM messages WHERE project_slug=? AND cwd IS NOT NULL",
                (r["project_slug"],),
            )]
            r["project_name"] = best_project_name(cwds, r["project_slug"])
    return rows


def tool_token_breakdown(db_path, since=None, until=None) -> list:
    rng, args = _range_clause(since, until)
    sql = f"""
      SELECT tool_name,
             COUNT(*) AS calls,
             COALESCE(SUM(result_tokens),0) AS result_tokens
        FROM tool_calls
       WHERE tool_name != '_tool_result' {rng}
       GROUP BY tool_name
       ORDER BY calls DESC
    """
    with connect(db_path) as c:
        return [dict(r) for r in c.execute(sql, args)]


def recent_sessions(db_path, limit: int = 20, since=None, until=None) -> list:
    rng, args = _range_clause(since, until)
    sql = f"""
      SELECT session_id, project_slug,
             MIN(timestamp) AS started, MAX(timestamp) AS ended,
             SUM(CASE WHEN type='user' THEN 1 ELSE 0 END) AS turns,
             SUM(input_tokens)+SUM(output_tokens) AS tokens
        FROM messages m
       WHERE 1=1 {rng}
       GROUP BY session_id
       ORDER BY ended DESC
       LIMIT ?
    """
    with connect(db_path) as c:
        rows = [dict(r) for r in c.execute(sql, (*args, limit))]
        # Cache per-slug name lookups so we don't query once per session.
        slug_cache = {}
        for r in rows:
            slug = r["project_slug"]
            if slug not in slug_cache:
                cwds = [row["cwd"] for row in c.execute(
                    "SELECT DISTINCT cwd FROM messages WHERE project_slug=? AND cwd IS NOT NULL",
                    (slug,),
                )]
                slug_cache[slug] = best_project_name(cwds, slug)
            r["project_name"] = slug_cache[slug]
    return rows


def session_turns(db_path, session_id: str) -> list:
    sql = """
      SELECT uuid, parent_uuid, type, timestamp, model, is_sidechain, agent_id,
             input_tokens, output_tokens, cache_read_tokens,
             cache_create_5m_tokens, cache_create_1h_tokens,
             prompt_text, prompt_chars, tool_calls_json, project_slug, cwd
        FROM messages
       WHERE session_id = ?
       ORDER BY timestamp ASC
    """
    with connect(db_path) as c:
        return [dict(r) for r in c.execute(sql, (session_id,))]


def daily_token_breakdown(db_path, since=None, until=None) -> list:
    """One row per day: stacked bar data for input/output/cache_read/cache_create."""
    rng, args = _range_clause(since, until)
    sql = f"""
      SELECT substr(timestamp, 1, 10) AS day,
             COALESCE(SUM(input_tokens),0)      AS input_tokens,
             COALESCE(SUM(output_tokens),0)     AS output_tokens,
             COALESCE(SUM(cache_read_tokens),0) AS cache_read_tokens,
             COALESCE(SUM(cache_create_5m_tokens),0)
               + COALESCE(SUM(cache_create_1h_tokens),0) AS cache_create_tokens
        FROM messages
       WHERE timestamp IS NOT NULL {rng}
       GROUP BY day
       ORDER BY day ASC
    """
    with connect(db_path) as c:
        return [dict(r) for r in c.execute(sql, args)]


def skill_breakdown(db_path, since=None, until=None) -> list:
    """Per-skill invocation counts, distinct sessions, last-used timestamp.

    Token attribution per skill is not included: in Claude Code, a Skill's
    content is loaded via a system-reminder on the next turn, not as the
    tool_result body — so `result_tokens` on _tool_result rows reflects the
    activation ack (tiny), not the skill definition (which is what actually
    fills context). A future schema change (storing tool_use_id on the
    invocation row) could enable precise attribution; for now we only expose
    the reliable counts.
    """
    rng, args = _range_clause(since, until)
    sql = f"""
      SELECT target AS skill,
             COUNT(*) AS invocations,
             COUNT(DISTINCT session_id) AS sessions,
             MAX(timestamp) AS last_used
        FROM tool_calls
       WHERE tool_name = 'Skill' AND target IS NOT NULL AND target != '' {rng}
       GROUP BY target
       ORDER BY invocations DESC
    """
    with connect(db_path) as c:
        return [dict(r) for r in c.execute(sql, args)]


def model_breakdown(db_path, since=None, until=None) -> list:
    """Per-model token totals + turn count. Caller computes cost via pricing."""
    rng, args = _range_clause(since, until)
    sql = f"""
      SELECT COALESCE(model, 'unknown') AS model,
             COUNT(*) AS turns,
             COALESCE(SUM(input_tokens),0)            AS input_tokens,
             COALESCE(SUM(output_tokens),0)           AS output_tokens,
             COALESCE(SUM(cache_read_tokens),0)       AS cache_read_tokens,
             COALESCE(SUM(cache_create_5m_tokens),0)  AS cache_create_5m_tokens,
             COALESCE(SUM(cache_create_1h_tokens),0)  AS cache_create_1h_tokens
        FROM messages
       WHERE type = 'assistant' {rng}
       GROUP BY model
       ORDER BY (input_tokens + output_tokens + cache_create_5m_tokens + cache_create_1h_tokens) DESC
    """
    with connect(db_path) as c:
        return [dict(r) for r in c.execute(sql, args)]


def upsert_workspaces(conn, workspaces: list, sync_ts: float) -> int:
    """Idempotent upsert of workspaces from the Admin API. Caller commits."""
    n = 0
    for w in workspaces:
        conn.execute(
            """
            INSERT OR REPLACE INTO workspaces
              (workspace_id, name, display_color, type, created_at, archived_at, last_synced_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                w.get("id") or "",
                w.get("name"),
                w.get("display_color"),
                w.get("type"),
                w.get("created_at"),
                w.get("archived_at"),
                sync_ts,
            ),
        )
        n += 1
    return n


def upsert_admin_usage(conn, rows: list, sync_ts: float) -> int:
    """Idempotent upsert of admin-usage rows. Caller commits.

    Each row is expected to carry: workspace_id, api_key_id, model, service_tier,
    bucket_start, input_tokens (uncached), output_tokens, cache_read_tokens,
    cache_create_5m_tokens, cache_create_1h_tokens, cost_usd. Missing keys default
    to '' or 0. ``cost_usd`` is the caller's responsibility — compute it before
    calling so the row carries authoritative pricing.
    """
    n = 0
    for r in rows:
        conn.execute(
            """
            INSERT OR REPLACE INTO admin_usage
              (workspace_id, api_key_id, model, service_tier, bucket_start,
               input_tokens, output_tokens,
               cache_read_tokens, cache_create_5m_tokens, cache_create_1h_tokens,
               cost_usd, last_synced_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                r.get("workspace_id") or "",
                r.get("api_key_id") or "",
                r.get("model") or "",
                r.get("service_tier") or "",
                r.get("bucket_start") or "",
                int(r.get("input_tokens") or 0),
                int(r.get("output_tokens") or 0),
                int(r.get("cache_read_tokens") or 0),
                int(r.get("cache_create_5m_tokens") or 0),
                int(r.get("cache_create_1h_tokens") or 0),
                float(r.get("cost_usd") or 0.0),
                sync_ts,
            ),
        )
        n += 1
    return n


def workspaces_with_usage(db_path, since=None, until=None) -> list:
    """Workspaces joined with aggregated usage in [since, until).

    Returns one row per workspace, including those with zero activity in range.
    ``cost_usd`` is summed from per-row pricing computed at sync time
    (``pricing.cost_for`` against the Admin API tokens), not from the
    cost_report endpoint — the cost_report rolls up org-level activity that
    cannot be reliably split per-workspace.
    """
    rng, args = _range_clause(since, until, col="bucket_start")
    sql = f"""
      SELECT w.workspace_id,
             w.name,
             w.display_color,
             w.type,
             w.created_at,
             w.archived_at,
             w.last_synced_at,
             COALESCE(SUM(u.input_tokens),0)            AS input_tokens,
             COALESCE(SUM(u.output_tokens),0)           AS output_tokens,
             COALESCE(SUM(u.cache_read_tokens),0)       AS cache_read_tokens,
             COALESCE(SUM(u.cache_create_5m_tokens),0)  AS cache_create_5m_tokens,
             COALESCE(SUM(u.cache_create_1h_tokens),0)  AS cache_create_1h_tokens,
             COALESCE(SUM(u.cost_usd),0)                AS cost_usd,
             MAX(u.bucket_start)                        AS last_activity
        FROM workspaces w
        LEFT JOIN admin_usage u
          ON u.workspace_id = w.workspace_id
         AND u.bucket_start IS NOT NULL {rng}
       GROUP BY w.workspace_id
       ORDER BY cost_usd DESC, w.name
    """
    with connect(db_path) as c:
        return [dict(r) for r in c.execute(sql, args)]


def last_admin_sync(db_path) -> Optional[float]:
    """Most recent successful sync timestamp across both admin tables, or None."""
    sql = """
      SELECT MAX(ts) AS ts FROM (
        SELECT MAX(last_synced_at) AS ts FROM workspaces
        UNION ALL
        SELECT MAX(last_synced_at) AS ts FROM admin_usage
      )
    """
    with connect(db_path) as c:
        row = c.execute(sql).fetchone()
    return row["ts"] if row and row["ts"] is not None else None
