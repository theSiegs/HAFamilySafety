"""add_time_bonus / cancel_time_bonus (experimental): believed only when read back."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.util import dt as dt_util

from custom_components.microsoft_family_safety import _register_services
from custom_components.microsoft_family_safety.const import DOMAIN
from custom_components.microsoft_family_safety.screen_time import parse_utc

from .test_platform_lock import CHILD, FakeWebAPI, _hours, _make, _xbox_policy


@pytest.fixture
def rig(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={"refresh_token": "x"})
    entry.add_to_hass(hass)
    coord = _make(hass, entry)
    coord.web_api = FakeWebAPI()
    return coord


def _poll(coord):
    coord.data["accounts"][CHILD]["xbox_policy"] = coord.web_api.xbox_policy


async def test_bonus_applied_and_read_back(rig):
    until = _hours(0.5)
    result = await rig.async_add_time_bonus(CHILD, "Xbox", until)

    assert [(p, k) for p, k, _ in rig.web_api.writes] == [("xbox", "allowUntil")]
    assert result["platform"] == "Xbox"
    assert result["released_hold"] is False
    assert result["read_back"]["type"] == "allowUntil"
    assert parse_utc(result["applied_until"]) == parse_utc(until.isoformat())
    _poll(rig)
    assert rig.platform_hold_state(CHILD, "Xbox")["hold"] == "bonus_ours"


async def test_a_read_back_that_does_not_match_fails(rig):
    original = rig.web_api.set_platform_time_override

    async def ignored(child_id, platform, kind, date_time):
        rig.web_api.writes.append((platform.lower(), kind, date_time))  # accepted, not applied

    rig.web_api.set_platform_time_override = ignored
    with pytest.raises(HomeAssistantError, match="did not apply the extra time") as err:
        await rig.async_add_time_bonus(CHILD, "Xbox", _hours(0.5))
    assert "read back" in str(err.value) and "'none'" in str(err.value)
    rig.web_api.set_platform_time_override = original
    assert rig._bonuses == {}


async def test_an_http_error_fails_without_a_retry(rig):
    calls = []
    original = rig.web_api.set_platform_time_override

    async def failing(*args):
        calls.append(args)
        rig.web_api.fail = "HTTP_400"
        return await original(*args)

    rig.web_api.set_platform_time_override = failing
    with pytest.raises(HomeAssistantError, match="refused the extra time"):
        await rig.async_add_time_bonus(CHILD, "Xbox", _hours(0.5))
    assert len(calls) == 1


async def test_a_bonus_releases_our_lock(rig):
    await rig.async_lock_platform(CHILD, "Xbox", _hours(3))
    result = await rig.async_add_time_bonus(CHILD, "Xbox", _hours(0.5))

    assert result["released_hold"] is True
    assert rig.platform_hold(CHILD, "Xbox") is None
    _poll(rig)
    assert rig.platform_hold_state(CHILD, "Xbox")["hold"] == "bonus_ours"


async def test_cancel_never_touches_a_lock(rig):
    rig.web_api.xbox_policy = _xbox_policy("blockUntil", _hours(2))  # a parent's lock
    result = await rig.async_cancel_time_bonus(CHILD, "Xbox")
    assert result["cancelled"] is False
    assert rig.web_api.writes == []


async def test_cancel_ends_extra_time(rig):
    await rig.async_add_time_bonus(CHILD, "Xbox", _hours(0.5))
    rig.web_api.writes.clear()
    result = await rig.async_cancel_time_bonus(CHILD, "Xbox")
    assert result["cancelled"] is True
    assert [(p, k) for p, k, _ in rig.web_api.writes] == [("xbox", "cancel")]
    assert rig._bonuses == {}


async def test_extra_time_we_did_not_give_is_parent(rig):
    rig.web_api.xbox_policy = _xbox_policy("allowUntil", _hours(1))
    _poll(rig)
    assert rig.platform_hold_state(CHILD, "Xbox")["hold"] == "parent"


async def test_service_takes_minutes_or_until(hass):
    coord = MagicMock()
    coord.async_add_time_bonus = AsyncMock(return_value={"platform": "Xbox"})
    with patch("custom_components.microsoft_family_safety._get_coordinator", return_value=coord):
        _register_services(hass)
        before = dt_util.now()
        response = await hass.services.async_call(
            DOMAIN, "add_time_bonus", {"account_id": CHILD, "platform": "Xbox", "minutes": 30},
            blocking=True, return_response=True,
        )
        assert response == {"platform": "Xbox"}
        until = coord.async_add_time_bonus.await_args.args[2]
        assert timedelta(minutes=29) < until - before < timedelta(minutes=31)
        for data in ({}, {"minutes": 5, "until": (dt_util.now() + timedelta(hours=1)).isoformat()}):
            with pytest.raises(ServiceValidationError):
                await hass.services.async_call(
                    DOMAIN, "add_time_bonus", {"account_id": CHILD, "platform": "Xbox", **data},
                    blocking=True, return_response=True,
                )
