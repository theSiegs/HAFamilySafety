"""Fork-only diagnostic: every candidate read of one app's policy (no writes).

pause_app needs an app's current policy (blockState and the per-day
allowance and intervals) to put it back exactly. GET v3/AppLimits/policies/
{child}/{app} did not give a usable policy for a Windows app on a live
account, so this probe asks every read the mobile API might answer and
returns each one raw. Only GET requests are sent.

Sources for the candidates: pyfamilysafety's endpoint list (v3 PATCH policy,
v4 appUsage activity report with ``policy`` / ``blockState`` per app) and its
docs/endpoints.xlsx (older v2 appLimits/policies and v2 appUsage), the
integration's own v2/AppLimits/settings read, and plain variations (no app
id, other versions, Plat-Info casing, an encoded app id).
"""
from __future__ import annotations

import json
from datetime import timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from homeassistant.util import dt as dt_util

from .app_pause import app_platform, read_entry, saved_policy

if TYPE_CHECKING:
    from .api_client import FamilySafetyWebAPI

#: Characters of each raw body kept in the answer.
BODY_LIMIT = 4096

_PLAT_INFO = {"windows": "Windows", "xbox": "Xbox"}


def _clip(value: Any) -> Any:
    """The body as Microsoft sent it, as text cut to BODY_LIMIT when long."""
    if value is None:
        return None
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    if len(text) <= BODY_LIMIT:
        return value
    return f"{text[:BODY_LIMIT]}... [{len(text)} characters]"


def _find_app(value: Any, app_id: str) -> dict | None:
    """The first dict anywhere in ``value`` whose appId is ``app_id``."""
    if isinstance(value, dict):
        if str(value.get("appId", "")).lower() == app_id.lower():
            return value
        children = value.values()
    elif isinstance(value, list):
        children = value
    else:
        return None
    for child in children:
        found = _find_app(child, app_id)
        if found is not None:
            return found
    return None


def candidates(child: str, app_id: str, platform: str) -> list[dict[str, Any]]:
    """The reads to try: name, path, query params, headers, encoded path."""
    plat = _PLAT_INFO.get(platform, "Windows")
    header = {"Plat-Info": plat}
    encoded_app = quote(app_id, safe="")
    now = dt_util.now()
    start = dt_util.start_of_local_day(now)
    report = {
        "beginTime": dt_util.as_utc(start).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "endTime": dt_util.as_utc(now).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    week = {
        **report,
        "beginTime": dt_util.as_utc(start - timedelta(days=6)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    def c(name: str, path: str, headers: dict | None = header, params: dict | None = None,
          encoded: bool = False) -> dict[str, Any]:
        return {"name": name, "path": path, "headers": headers, "params": params,
                "encoded": encoded}

    one = f"/v3/AppLimits/policies/{child}/{app_id}"
    return [
        c("v3_policy", one),
        c("v3_policy_encoded_app_id", f"/v3/AppLimits/policies/{child}/{encoded_app}",
          encoded=True),
        c("v3_policy_lowercase_path", f"/v3/appLimits/policies/{child}/{app_id}"),
        c("v3_policy_upper_plat_info", one, headers={"Plat-Info": plat.upper()}),
        c("v3_policy_no_plat_info", one, headers=None),
        c("v3_policy_query_app_id", f"/v3/AppLimits/policies/{child}",
          params={"appId": app_id}),
        c("v3_policies_list", f"/v3/AppLimits/policies/{child}"),
        c("v3_policies_list_platform_param", f"/v3/AppLimits/policies/{child}",
          params={"platform": plat}),
        c("v3_applimits_child", f"/v3/AppLimits/{child}"),
        c("v2_policy", f"/v2/AppLimits/policies/{child}/{app_id}"),
        c("v2_policies_list", f"/v2/AppLimits/policies/{child}"),
        c("v4_policy", f"/v4/AppLimits/policies/{child}/{app_id}"),
        c("v4_policies_list", f"/v4/AppLimits/policies/{child}"),
        c("v1_policies_list", f"/v1/AppLimits/policies/{child}"),
        c("v2_settings", f"/v2/AppLimits/settings/{child}"),
        c("v4_app_usage_today", f"/v4/activityReport/appUsage/{child}", headers=None,
          params=report),
        c("v4_app_usage_today_plat_info", f"/v4/activityReport/appUsage/{child}",
          params=report),
        c("v4_app_usage_week", f"/v4/activityReport/appUsage/{child}", headers=None,
          params=week),
        c("v2_app_usage_today", f"/v2/activityReport/appUsage/{child}", headers=None,
          params=report),
        c("v4_schedules", f"/v4/devicelimits/schedules/{child}",
          params={"culture": "en-US", "time": now.isoformat(timespec="seconds")}),
    ]


def _pause_can_use(entry: Any) -> bool:
    """pause_app would accept this as an app's activity report entry."""
    if not isinstance(entry, dict) or "policy" not in entry:
        return False
    now = read_entry(entry)
    if now is None:
        return False
    try:
        saved_policy(now["policy"])
    except ValueError:
        return False
    return True


async def async_probe_app_policy(
    web_api: FamilySafetyWebAPI, child: str, app_id: str
) -> dict[str, Any]:
    """Send every candidate GET and return each request with the raw answer.

    For each answer, ``app_entry`` is the part naming this app (the whole body
    when it is the app's policy itself) and ``pause_can_use`` says whether
    pause_app accepts it as the app's activity report entry.
    """
    platform = app_platform(app_id) or "windows"
    results: list[dict[str, Any]] = []
    for cand in candidates(str(child), app_id, platform):
        answer = await web_api.mobile_raw_request(
            "GET", cand["path"], None, cand["headers"],
            params=cand["params"], encoded=cand["encoded"],
        )
        body = answer.get("body")
        entry = _find_app(body, app_id)
        if entry is None and isinstance(body, dict) and "blockState" in body:
            entry = body
        results.append({
            "name": cand["name"],
            "method": "GET",
            "path": cand["path"],
            "params": cand["params"],
            "headers": cand["headers"],
            "status": answer.get("status"),
            **({"error": answer["error"]} if "error" in answer else {}),
            "body": _clip(body),
            "app_entry": _clip(entry),
            "pause_can_use": _pause_can_use(entry),
        })
    return {
        "account_id": str(child),
        "app_id": app_id,
        "platform": platform,
        "usable": [r["name"] for r in results if r["pause_can_use"]],
        "requests": results,
    }
