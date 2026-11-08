"""Strict mode puts back a lock of ours that Microsoft reports as cancelled.

On Windows here; the Xbox (through xapprove) is in test_xbox_xapprove.py.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_capture_events

from homeassistant.util import dt as dt_util

from custom_components.microsoft_family_safety.const import (
    CONF_STRICT_MODE,
    DOMAIN,
    EVENT_LOCK_REASSERTED,
)
from custom_components.microsoft_family_safety.screen_time import iso_utc

from .test_platform_lock import CHILD, FakeWebAPI, _make, _windows


@pytest.fixture
def web():
    return FakeWebAPI()


def _coordinator(hass, web, strict: bool):
    entry = MockConfigEntry(
        domain=DOMAIN, data={"refresh_token": "x"}, options={CONF_STRICT_MODE: strict}
    )
    entry.add_to_hass(hass)
    coord = _make(hass, entry)
    coord.web_api = web
    return coord


async def _locked(coord, platform="Windows"):
    await coord.async_lock_platform(CHILD, platform, dt_util.utcnow() + timedelta(hours=2))
    coord.web_api.writes.clear()


def _cancelled(web):
    web.windows = _windows("cancel")


async def test_cancelled_lock_is_placed_again_with_an_event(hass, web):
    coord = _coordinator(hass, web, strict=True)
    await _locked(coord)
    events = async_capture_events(hass, EVENT_LOCK_REASSERTED)
    until = coord.platform_hold(CHILD, "Windows")["until"]
    _cancelled(web)

    await coord._async_enforce_holds({})
    await hass.async_block_till_done()

    assert web.writes == [("windows", "blockUntil", until)]
    assert len(events) == 1
    assert events[0].data["account_id"] == CHILD
    assert events[0].data["platform"] == "Windows"
    assert events[0].data["until"] == until

    # Within the cooldown nothing is sent again.
    _cancelled(web)
    await coord._async_enforce_holds({})
    await hass.async_block_till_done()
    assert len(web.writes) == 1


async def test_strict_mode_off_changes_nothing(hass, web):
    coord = _coordinator(hass, web, strict=False)
    await _locked(coord)
    _cancelled(web)
    await coord._async_enforce_holds({})
    await hass.async_block_till_done()
    assert web.writes == []


@pytest.mark.parametrize("unreadable", [True, False])
async def test_unknown_or_still_blocked_is_left_alone(hass, web, unreadable):
    coord = _coordinator(hass, web, strict=True)
    await _locked(coord)
    if unreadable:
        web.windows = None
    await coord._async_enforce_holds({})
    await hass.async_block_till_done()
    assert web.writes == []


async def test_unconfirmed_or_expired_holds_are_left_alone(hass, web):
    coord = _coordinator(hass, web, strict=True)
    await _locked(coord)
    _cancelled(web)
    key = coord._hold_key(CHILD, "Windows")
    coord._platform_holds[key]["confirmed"] = False
    await coord._async_enforce_holds({})
    coord._platform_holds[key]["confirmed"] = True
    coord._platform_holds[key]["until"] = iso_utc(dt_util.utcnow() - timedelta(minutes=1))
    await coord._async_enforce_holds({})
    await hass.async_block_till_done()
    assert web.writes == []


async def test_unlocked_lock_is_not_placed_again(hass, web):
    coord = _coordinator(hass, web, strict=True)
    await _locked(coord)
    await coord.async_unlock_platform(CHILD, "Windows")
    web.writes.clear()
    _cancelled(web)
    await coord._async_enforce_holds({})
    await hass.async_block_till_done()
    assert web.writes == []


async def test_windows_lock_is_read_back_from_its_route(hass, web):
    coord = _coordinator(hass, web, strict=True)
    await _locked(coord, "Windows")
    web.windows = {"data": {"screenTimeOverrides": []}}  # cancelled elsewhere

    await coord._async_enforce_holds({})
    await hass.async_block_till_done()

    assert [(p, k) for p, k, _ in web.writes] == [("windows", "blockUntil")]
