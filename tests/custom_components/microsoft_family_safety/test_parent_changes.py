"""Telling a parent's change in the Family Safety app from our own holds."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_capture_events
import yaml

from custom_components.microsoft_family_safety import sensor
from custom_components.microsoft_family_safety.const import DOMAIN, EVENT_REQUEST_RESOLVED
from custom_components.microsoft_family_safety.coordinator import (
    FamilySafetyDataUpdateCoordinator,
)

from .test_platform_lock import CHILD, FakeWebAPI, _hours, _make


@pytest.fixture
def rig(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={"refresh_token": "x"})
    entry.add_to_hass(hass)
    coord = _make(hass, entry)
    coord.web_api = FakeWebAPI()
    coord.data["accounts"][CHILD].update(first_name="Alex", applications=[])
    return coord, entry


# 1. hold: ours / parent / none ------------------------------------------------
# (The Xbox's hold state comes from xapprove: test_xbox_xapprove.py.)


async def test_windows_uses_the_polled_lock_flag(rig):
    coord, _ = rig
    account = coord.data["accounts"][CHILD]
    account["blocked_platforms"] = ["Windows"]
    assert coord.platform_hold_state(CHILD, "Windows")["hold"] == "parent"
    await coord.async_lock_platform(CHILD, "Windows", _hours(2))
    assert coord.platform_hold_state(CHILD, "Windows")["hold"] == "ours"
    account["blocked_platforms"] = []  # cancelled in the app
    assert coord.platform_hold_state(CHILD, "Windows")["hold"] == "parent"


# 2. paused apps -----------------------------------------------------------------


async def test_paused_apps_are_marked_ours_then_parent(hass, rig):
    coord, entry = rig
    apps = [{"app_id": "x:racing", "app_name": "Racing", "blocked": False, "usage_minutes": 5}]
    coord.data["accounts"][CHILD]["applications"] = apps
    target = SimpleNamespace(blocked=False)
    coord.get_application = lambda account_id, app_id: target

    async def block(account_id, app_id):
        apps[0]["blocked"] = True

    coord.async_block_app = AsyncMock(side_effect=block)
    await coord.async_pause_app(CHILD, ["x:racing"])

    count = sensor.FamilySafetyApplicationCountSensor(coord, entry, CHILD)
    screen = sensor.FamilySafetyScreenTimeSensor(coord, entry, CHILD)
    assert count.extra_state_attributes["paused_apps"] == [
        {"package": "x:racing", "name": "Racing", "paused": "ours"}
    ]
    assert screen.extra_state_attributes["apps"][0]["paused"] == "ours"

    apps[0]["blocked"] = False  # a parent unblocked it in the app
    assert count.extra_state_attributes["paused_apps"][0]["paused"] == "parent"
    assert count.extra_state_attributes["apps"][0]["paused"] == "parent"


async def test_paused_state_is_read_back_after_a_restart(hass, rig):
    coord, entry = rig
    await coord.app_pauser._async_save({CHILD: {"x:racing": {"blocked": False}}})
    restarted = FamilySafetyDataUpdateCoordinator(hass, entry)
    await restarted.async_load_saved_screentime()
    assert restarted.app_pauser.saved(CHILD) == {"x:racing": {"blocked": False}}


# 3. request resolved ------------------------------------------------------------


async def test_request_resolved_via_service_and_via_app(hass, rig):
    coord, _ = rig
    events = async_capture_events(hass, EVENT_REQUEST_RESOLVED)
    coord.api = SimpleNamespace(
        approve_pending_request=AsyncMock(return_value=True),
        deny_pending_request=AsyncMock(return_value=True),
    )
    r1, r2, r3 = ({"id": rid, "puid": int(CHILD)} for rid in ("r1", "r2", "r3"))
    coord._fire_request_events([r1, r2, r3], {})  # baseline
    await coord.async_approve_request("r1", 1800)
    await coord.async_deny_request("r2")

    coord._fire_request_events([r3], {})  # r1, r2 gone
    coord._fire_request_events([], {})  # r3 gone, resolved in the app
    await hass.async_block_till_done()

    by_id = {e.data["request_id"]: e.data for e in events}
    assert by_id["r1"] | {"entry_id": None} == {
        "entry_id": None, "account_id": CHILD, "request_id": "r1",
        "outcome": "approved", "minutes": 30, "via": "service",
    }
    assert (by_id["r2"]["outcome"], by_id["r2"]["via"]) == ("denied", "service")
    assert (by_id["r3"]["outcome"], by_id["r3"]["minutes"], by_id["r3"]["via"]) == (
        "unknown", None, "app"
    )
    assert len(events) == 3


async def test_request_resolved_fires_once_even_if_it_flickers(hass, rig):
    coord, _ = rig
    events = async_capture_events(hass, EVENT_REQUEST_RESOLVED)
    request = {"id": "r1", "puid": CHILD}
    coord._fire_request_events([request], {})
    for pending in ([], [request], [], [], [], [], [request], []):
        coord._fire_request_events(pending, {})
    await hass.async_block_till_done()
    assert len(events) == 1


# 4. documentation ---------------------------------------------------------------


def test_strict_mode_warning_is_documented():
    from pathlib import Path

    root = Path(sensor.__file__).parent
    services = yaml.safe_load((root / "services.yaml").read_text(encoding="utf-8"))
    for name in ("lock_platform", "unlock_platform"):
        assert "strict mode off" in services[name]["description"]
    readme = (root.parent.parent / "README.md").read_text(encoding="utf-8")
    assert "Keep strict mode off when another integration" in readme
