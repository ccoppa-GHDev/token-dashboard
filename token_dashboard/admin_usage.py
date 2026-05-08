"""Anthropic Admin Usage/Cost API client.

Stdlib only (project rule). Three endpoints, all GET, all behind an Admin
API key (prefix ``sk-ant-admin-``). Verified against:

* https://docs.anthropic.com/en/api/admin-api/workspaces/list-workspaces
* https://docs.anthropic.com/en/api/admin-api/usage-cost/get-messages-usage-report
* https://docs.anthropic.com/en/api/admin-api/usage-cost/get-cost-report

The client returns flat lists of dicts ready for ``db.upsert_*`` so the route
handler can drive the whole sync inside one SQLite transaction.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional
from urllib.request import urlopen as _urlopen
from urllib.request import Request as _Request

ADMIN_BASE = "https://api.anthropic.com/v1/organizations"
API_VERSION = "2023-06-01"
HTTP_TIMEOUT_SECONDS = 30
RETRY_BACKOFF_SECONDS = 2.0


class MissingAdminKey(Exception):
    """No admin key in the env. Surface the actionable fix to the UI."""


def _admin_key() -> str:
    key = os.environ.get("ANTHROPIC_ADMIN_API_KEY")
    if not key:
        raise MissingAdminKey(
            "Set ANTHROPIC_ADMIN_API_KEY and retry. Create one at "
            "console.anthropic.com → Settings → Admin Keys."
        )
    return key


def _request(path: str, params: Optional[dict], key: str) -> dict:
    """Single GET. One short backoff on 429, then surface the error."""
    qs = urllib.parse.urlencode(params or {}, doseq=True)
    url = f"{ADMIN_BASE}{path}"
    if qs:
        url = f"{url}?{qs}"
    req = _Request(
        url,
        headers={"x-api-key": key, "anthropic-version": API_VERSION},
    )
    for attempt in range(2):
        try:
            with _urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
                return json.loads(resp.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt == 0:
                time.sleep(RETRY_BACKOFF_SECONDS)
                continue
            raise
    return {}  # unreachable; loop above either returns or raises


def _paginate(path: str, params: dict, key: str) -> list:
    """Walk has_more / next_page until exhausted. Returns concatenated ``data``."""
    out: list = []
    cursor: Optional[str] = None
    while True:
        page_params = dict(params)
        if cursor:
            page_params["page"] = cursor
        body = _request(path, page_params, key)
        out.extend(body.get("data") or [])
        if not body.get("has_more"):
            return out
        cursor = body.get("next_page")
        if not cursor:
            return out


def list_workspaces(key: Optional[str] = None, *, include_archived: bool = True) -> list:
    """All workspaces in the organization. Returns the API's row shape unchanged."""
    return _paginate(
        "/workspaces",
        {"limit": 100, "include_archived": str(bool(include_archived)).lower()},
        key or _admin_key(),
    )


def fetch_usage(
    starting_at: str,
    ending_at: str,
    *,
    bucket_width: str = "1d",
    key: Optional[str] = None,
) -> list:
    """Token usage rows flattened across pages and buckets.

    Returns a list of dicts with: ``workspace_id``, ``api_key_id``, ``model``,
    ``service_tier``, ``bucket_start``, ``input_tokens``, ``output_tokens``,
    ``cache_read_input_tokens``, ``cache_creation_input_tokens``.
    """
    if bucket_width not in ("1m", "1h", "1d"):
        raise ValueError(f"bucket_width must be 1m|1h|1d, got {bucket_width!r}")
    page_limit = {"1d": 31, "1h": 168, "1m": 1440}[bucket_width]
    raw = _paginate(
        "/usage_report/messages",
        {
            "starting_at": starting_at,
            "ending_at": ending_at,
            "bucket_width": bucket_width,
            "group_by[]": ["workspace_id", "api_key_id", "model", "service_tier"],
            "limit": page_limit,
        },
        key or _admin_key(),
    )
    return _flatten_usage(raw)


def _flatten_usage(buckets: list) -> list:
    """Each bucket has ``starting_at`` and a ``results`` array of grouped rows.

    Field names are normalized to match the rest of the dashboard (the
    ``messages`` table convention) so ``pricing.cost_for`` works against these
    rows unchanged: ``input_tokens`` (uncached input), ``cache_read_tokens``,
    and the two TTL-specific cache-creation buckets.
    """
    flat: list = []
    for bucket in buckets:
        bucket_start = bucket.get("starting_at") or ""
        for r in bucket.get("results") or []:
            cc = r.get("cache_creation") or {}
            flat.append({
                "workspace_id":           r.get("workspace_id") or "",
                "api_key_id":             r.get("api_key_id") or "",
                "model":                  r.get("model") or "",
                "service_tier":           r.get("service_tier") or "",
                "bucket_start":           bucket_start,
                "input_tokens":           int(r.get("uncached_input_tokens") or 0),
                "output_tokens":          int(r.get("output_tokens") or 0),
                "cache_read_tokens":      int(r.get("cache_read_input_tokens") or 0),
                "cache_create_5m_tokens": int(cc.get("ephemeral_5m_input_tokens") or 0),
                "cache_create_1h_tokens": int(cc.get("ephemeral_1h_input_tokens") or 0),
            })
    return flat
