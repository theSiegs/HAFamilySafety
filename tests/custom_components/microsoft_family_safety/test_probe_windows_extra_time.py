"""probe_windows_extra_time: a bounded, self-cancelling live check (diagnostic)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import voluptuous as vol
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import Context
from homeassistant.exceptions import Unauthorized
from homeassistant.util import dt as dt_util

from custom_components.microsoft_family_safety import _register_services
from custom_components.microsoft_family_safety.const import DOMAIN
from custom_components.microsoft_family_safety.coordinator import (
    FamilySafetyDataUpdateCoordinator,
)

CHILD = "1234567890123456"


def _state(*overrides):
    return {"lockablePlatforms": [{"appliesTo": "Windows", "overrides": list(overrides), "devices": []}]}


@pytest.fixture
async def coordinator(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={"refresh_token": "x"})
    entry.add_to_hass(hass)
    coord = FamilySafetyDataUpdateCoordinator(hass, entry)
    coord.async_request_refresh = AsyncMock()
    coord._accounts = {int(CHILD): SimpleNamespace(user_id=int(CHILD))}
    coord.web_api = MagicMock()
    return coord


async def test_probe_writes_reads_cancels_and_reads(coordinator):
    sent: list[dict] = []

    async def raw(method, path, body=None, headers=None):
        sent.append(body)
        if method == "POST":
            return {"status": 200, "body": {"ok": True}}
        writes = [b for b in sent if b]
        if len(writes) == 1:
            return {"status": 200, "body": _state(
                {"overrideType": "AllowUntil", "validUntil": writes[0]["validUntil"]}
            )}
        return {"status": 200, "body": _state()}

    coordinator.web_api.mobile_raw_request = AsyncMock(side_effect=raw)
    result = await coordinator.async_probe_windows_extra_time(CHILD, 2)

    assert [s["step"] for s in result["steps"]] == [
        "read_v1", "read_before", "write_allow", "read_after_write", "cancel", "read_after_cancel",
    ]
    assert result["steps"][2]["sent"]["overrideType"] == "AllowUntil"
    assert result["steps"][4]["sent"]["overrideType"] == "Cancel"
    assert result["confirmed"] is True and result["left_after_cancel"] == []
    assert result["target"] == "Windows"


async def test_probe_cancels_even_when_the_write_is_refused(coordinator):
    async def raw(method, path, body=None, headers=None):
        if method == "POST" and body["overrideType"] == "AllowUntil":
            return {"status": 400, "body": {"error": "bad overrideType"}}
        return {"status": 200, "body": _state()}

    coordinator.web_api.mobile_raw_request = AsyncMock(side_effect=raw)
    result = await coordinator.async_probe_windows_extra_time(CHILD, 2)
    assert result["confirmed"] is False
    assert result["steps"][2]["status"] == 400
    assert result["steps"][4]["step"] == "cancel"


async def test_probe_writes_nothing_while_an_override_is_in_force(coordinator):
    coordinator.web_api.mobile_raw_request = AsyncMock(return_value={
        "status": 200,
        "body": _state({"overrideType": "BlockUntil", "validUntil": "2099-01-01T00:00:00Z"}),
    })
    result = await coordinator.async_probe_windows_extra_time(CHILD, 2)
    assert "aborted" in result
    assert all(c.args[0] == "GET" for c in coordinator.web_api.mobile_raw_request.await_args_list)


async def test_probe_service_is_admin_only_and_needs_confirm_risk(hass, hass_admin_user, hass_read_only_user):
    coord = MagicMock()
    coord.async_probe_windows_extra_time = AsyncMock(return_value={"steps": []})
    with patch("custom_components.microsoft_family_safety._get_coordinator", return_value=coord):
        _register_services(hass)
        with pytest.raises(vol.Invalid):
            await hass.services.async_call(
                DOMAIN, "probe_windows_extra_time",
                {"account_id": CHILD, "confirm_risk": False},
                blocking=True, return_response=True,
                context=Context(user_id=hass_admin_user.id),
            )
        with pytest.raises(Unauthorized):
            await hass.services.async_call(
                DOMAIN, "probe_windows_extra_time",
                {"account_id": CHILD, "confirm_risk": True},
                blocking=True, return_response=True,
                context=Context(user_id=hass_read_only_user.id),
            )
        response = await hass.services.async_call(
            DOMAIN, "probe_windows_extra_time",
            {"account_id": CHILD, "confirm_risk": True},
            blocking=True, return_response=True,
            context=Context(user_id=hass_admin_user.id),
        )
    assert response == {"steps": []}
    coord.async_probe_windows_extra_time.assert_awaited_once_with(CHILD, 2)
