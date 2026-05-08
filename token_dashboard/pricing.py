"""Pricing table + plan-aware cost formatting."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional, Union

from .db import connect


def load_pricing(path: Union[str, Path]) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _tier_from_name(model: str) -> Optional[str]:
    m = (model or "").lower()
    for tier in ("opus", "sonnet", "haiku"):
        if tier in m:
            return tier
    return None


def cost_for(model: str, usage: dict, pricing: dict) -> dict:
    """Return {usd, estimated, breakdown}. usd=None when no tier match."""
    rates = pricing["models"].get(model)
    estimated = False
    if rates is None:
        tier = _tier_from_name(model or "")
        if tier and tier in pricing["tier_fallback"]:
            rates = pricing["tier_fallback"][tier]
            estimated = True
        else:
            return {"usd": None, "estimated": True, "breakdown": {}}
    bd = {
        "input":           usage["input_tokens"]            * rates["input"]           / 1_000_000,
        "output":          usage["output_tokens"]           * rates["output"]          / 1_000_000,
        "cache_read":      usage["cache_read_tokens"]       * rates["cache_read"]      / 1_000_000,
        "cache_create_5m": usage["cache_create_5m_tokens"]  * rates["cache_create_5m"] / 1_000_000,
        "cache_create_1h": usage["cache_create_1h_tokens"]  * rates["cache_create_1h"] / 1_000_000,
    }
    return {"usd": round(sum(bd.values()), 6), "estimated": estimated, "breakdown": bd}


def get_plan(db_path: Union[str, Path], default: str = "api") -> str:
    with connect(db_path) as c:
        row = c.execute("SELECT v FROM plan WHERE k='plan'").fetchone()
    return row["v"] if row else default


def set_plan(db_path: Union[str, Path], plan: str) -> None:
    with connect(db_path) as c:
        c.execute("INSERT OR REPLACE INTO plan (k, v) VALUES ('plan', ?)", (plan,))
        c.commit()


def format_allocation(
    row_api_cost: float,
    total_api_cost: float,
    plan: str,
    pricing: dict,
    months_in_range: int,
) -> dict:
    """Per-row plan-aware cost display (project, session, etc.).

    For API plan: display the row's own API-equivalent cost.
    For subscription plans: display the row's allocated share of what the user
    actually paid in the range, weighted by the row's share of total API cost.
    """
    p = pricing["plans"].get(plan, pricing["plans"]["api"])
    monthly = p.get("monthly") or 0
    label = p.get("label", plan)
    if plan == "api" or monthly == 0:
        return {
            "api_cost_usd":    row_api_cost,
            "display_usd":     row_api_cost,
            "display_suffix":  None,
            "share_of_plan":   None,
            "is_subscription": False,
            "plan_label":      label,
        }
    total_paid = float(monthly) * max(1, int(months_in_range))
    share = (row_api_cost / total_api_cost) if total_api_cost > 0 else 0.0
    return {
        "api_cost_usd":    row_api_cost,
        "display_usd":     round(total_paid * share, 4),
        "display_suffix":  None,
        "share_of_plan":   round(share, 6),
        "is_subscription": True,
        "plan_label":      label,
    }


def format_for_user(api_cost_usd: float, plan: str, pricing: dict) -> dict:
    """Translate a period's API-equivalent cost into a plan-aware display shape.

    For the API plan, the headline $ is the API cost itself.
    For subscription plans, the headline $ is the flat monthly fee and the
    API-equivalent cost is surfaced in a subtitle so the user can compare.
    """
    p = pricing["plans"].get(plan, pricing["plans"]["api"])
    monthly = p.get("monthly") or 0
    label = p.get("label", plan)
    if plan == "api" or monthly == 0:
        return {
            "api_cost_usd":    api_cost_usd,
            "display_usd":     api_cost_usd,
            "display_suffix":  None,
            "subtitle":        None,
            "plan_label":      label,
            "is_subscription": False,
            "monthly_fee":     0,
        }
    return {
        "api_cost_usd":    api_cost_usd,
        "display_usd":     float(monthly),
        "display_suffix":  "/mo",
        "subtitle":        f"${api_cost_usd:,.2f} API-equivalent this period",
        "plan_label":      label,
        "is_subscription": True,
        "monthly_fee":     monthly,
    }
