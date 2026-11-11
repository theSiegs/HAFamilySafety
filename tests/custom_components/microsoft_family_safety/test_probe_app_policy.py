"""probe_app_policy: every candidate read of one app's policy, GETs only (diagnostic)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import Context
from homeassistant.exceptions import Unauthorized

from custom_components.microsoft_family_safety import _register_services
from custom_components.microsoft_family_safety.app_policy_probe import (
    BODY_LIMIT,
    async_probe_app_policy,
)
from custom_components.microsoft_family_safety.const import DAY_KEYS, DOMAIN
from custom_components.microsoft_family_safety.coordinator import (
    FamilySafetyDataUpdateCoordinator,
)

CHILD = "1234567890123456"
APP = "appx:windowsplt:example_1234abcd"


def _policy(app_id: str = APP) -> dict:
    day = {"allowance": 3_600_000, "intervalsEnabled": False, "intervals": []}
    return {
        "appId": app_id, "displayName": "Example", "enabled": True,
        "blockState": "NotBlocked", "appTimeEnforcementPolicy": "Custom",
        **{d: day for d in DAY_KEYS},
    }


async def test_probe_sends_only_gets_and_reports_each_answer():
    async def raw(method, path, body=None, headers=None, *, params=None, encoded=False):
        if path == f"/v3/AppLimits/policies/{CHILD}/{APP}" and headers == {"Plat-Info": "Windows"}:
            return {"status": 404, "body": {"error": "not found"}}
        if path == f"/v2/AppLimits/policies/{CHILD}/{APP}":
            return {"status": 200, "body": _policy()}
        if path == f"/v4/activityReport/appUsage/{CHILD}":
            return {"status": 200, "body": {"appActivity": [
                {"appId": "x:other", "policy": "Unlimited", "blockState": "NotBlocked"},
                {"appId": APP, "policy": _policy(), "blockState": "NotBlocked",
                 "isLegacyBlocked": False, "usage": 3},
            ]}}
        if path == f"/v3/AppLimits/policies/{CHILD}" and params is None:
            return {"status": 200, "body": {"policies": [_policy("x:other"), _policy()]}}
        if path.startswith("/v1/"):
            return {"status": None, "error": "ClientError: boom"}
        return {"status": 200, "body": "x" * (BODY_LIMIT + 10)}

    api = MagicMock()
    api.mobile_raw_request = AsyncMock(side_effect=raw)
    result = await async_probe_app_policy(api, CHILD, APP)

    calls = api.mobile_raw_request.await_args_list
    assert calls and all(c.args[0] == "GET" and c.args[2] is None for c in calls)
    by_name = {r["name"]: r for r in result["requests"]}
    assert len(by_name) == len(calls)

    assert by_name["v3_policy"]["status"] == 404
    assert by_name["v3_policy"]["pause_can_use"] is False
    # A bare policy is not what pause_app reads: the activity report entry is.
    assert by_name["v2_policy"]["pause_can_use"] is False
    assert by_name["v3_policies_list"]["app_entry"]["appId"] == APP
    assert by_name["v3_policies_list"]["pause_can_use"] is False
    usage = by_name["v4_app_usage_today"]
    assert usage["app_entry"]["policy"] == _policy()
    assert usage["pause_can_use"] is True
    assert usage["params"]["beginTime"].endswith("Z")
    assert by_name["v1_policies_list"]["error"] == "ClientError: boom"
    clipped = by_name["v4_policy"]["body"]
    assert clipped.startswith("x" * BODY_LIMIT) and clipped.endswith("characters]")

    encoded = by_name["v3_policy_encoded_app_id"]
    assert encoded["path"] == f"/v3/AppLimits/policies/{CHILD}/appx%3Awindowsplt%3Aexample_1234abcd"
    assert any(c.kwargs.get("encoded") for c in calls)
    assert by_name["v3_policy_no_plat_info"]["headers"] is None
    assert result["usable"] == [
        r["name"] for r in result["requests"]
        if r["path"] == f"/v4/activityReport/appUsage/{CHILD}"
    ]
    assert "v4_app_usage_today" in result["usable"]
    assert result["platform"] == "windows"


async def test_probe_uses_the_xbox_plat_info_for_an_xbox_app():
    api = MagicMock()
    api.mobile_raw_request = AsyncMock(return_value={"status": 200, "body": None})
    result = await async_probe_app_policy(api, CHILD, "x:1234567890")
    first = result["requests"][0]
    assert first["headers"] == {"Plat-Info": "Xbox"}
    assert result["usable"] == []


async def test_coordinator_probe_needs_the_mobile_api(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={"refresh_token": "x"})
    entry.add_to_hass(hass)
    coord = FamilySafetyDataUpdateCoordinator(hass, entry)
    coord.web_api = None
    with pytest.raises(Exception, match="Mobile API not initialized"):
        await coord.async_probe_app_policy(CHILD, APP)


async def test_probe_service_is_admin_only(hass, hass_admin_user, hass_read_only_user):
    coord = MagicMock()
    coord.async_probe_app_policy = AsyncMock(return_value={"requests": []})
    with patch("custom_components.microsoft_family_safety._get_coordinator", return_value=coord):
        _register_services(hass)
        with pytest.raises(Unauthorized):
            await hass.services.async_call(
                DOMAIN, "probe_app_policy", {"account_id": CHILD, "app_id": APP},
                blocking=True, return_response=True,
                context=Context(user_id=hass_read_only_user.id),
            )
        response = await hass.services.async_call(
            DOMAIN, "probe_app_policy", {"account_id": CHILD, "app_id": APP},
            blocking=True, return_response=True,
            context=Context(user_id=hass_admin_user.id),
        )
    assert response == {"requests": []}
    coord.async_probe_app_policy.assert_awaited_once_with(CHILD, APP)
