"""pause_app / resume_app: save the state once, put it back on resume."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.microsoft_family_safety import _register_services
from custom_components.microsoft_family_safety.const import DOMAIN
from custom_components.microsoft_family_safety.coordinator import (
    FamilySafetyDataUpdateCoordinator,
)

CHILD = "1234567890123456"


@pytest.fixture
def rig(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={"refresh_token": "x"})
    entry.add_to_hass(hass)
    coord = FamilySafetyDataUpdateCoordinator(hass, entry)
    coord.update_interval = None
    apps = {
        "x:racing": SimpleNamespace(blocked=False),
        "appx:paint": SimpleNamespace(blocked=False),
        "win32:chat": SimpleNamespace(blocked=True),  # blocked by a parent
    }
    writes: list[tuple[str, str]] = []

    async def block(account_id, app_id):
        writes.append(("block", app_id))
        apps[app_id].blocked = True

    async def unblock(account_id, app_id):
        writes.append(("unblock", app_id))
        apps[app_id].blocked = False

    coord.get_application = lambda account_id, app_id: apps.get(app_id)
    coord.async_block_app = AsyncMock(side_effect=block)
    coord.async_unblock_app = AsyncMock(side_effect=unblock)
    coord.data = {"accounts": {CHILD: {}}, "devices": {}}
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coord
    _register_services(hass)
    return coord, apps, writes, entry


async def _call(hass, name, **data):
    return await hass.services.async_call(
        DOMAIN, name, {"account_id": CHILD, **data}, blocking=True, return_response=True
    )


async def test_pause_then_resume(hass, rig):
    coord, apps, writes, _ = rig
    paused = await _call(hass, "pause_app", apps=["x:racing", "win32:chat", "nope"])
    assert paused["paused"] == ["x:racing"]
    assert paused["skipped"] == {"win32:chat": "already blocked", "nope": "unknown app"}
    assert apps["x:racing"].blocked is True

    resumed = await _call(hass, "resume_app")
    assert resumed["resumed"] == ["x:racing"]
    assert apps["x:racing"].blocked is False
    assert apps["win32:chat"].blocked is True  # never touched
    assert writes == [("block", "x:racing"), ("unblock", "x:racing")]


async def test_repeat_pause_keeps_the_first_state_also_after_a_restart(hass, rig):
    coord, apps, writes, entry = rig
    await _call(hass, "pause_app", packages=["appx:paint"])  # FL's field name
    # The app now reads as blocked; a second pause must not save that.
    await _call(hass, "pause_app", apps=["appx:paint"])
    restarted = FamilySafetyDataUpdateCoordinator(hass, entry)
    assert await restarted.app_pauser.async_saved(CHILD) == {"appx:paint": {"blocked": False}}

    await _call(hass, "resume_app", apps=["appx:paint"])
    assert apps["appx:paint"].blocked is False
    assert await coord.app_pauser.async_saved(CHILD) == {}


async def test_resume_of_an_app_never_paused_is_skipped(hass, rig):
    _, _, writes, _ = rig
    result = await _call(hass, "resume_app", apps=["appx:paint"])
    assert result["skipped"] == {"appx:paint": "not paused"}
    assert writes == []


async def test_a_failed_unblock_keeps_the_saved_state(hass, rig):
    coord, apps, _, _ = rig
    await _call(hass, "pause_app", apps=["x:racing"])
    coord.async_unblock_app.side_effect = RuntimeError("HTTP 500")
    result = await _call(hass, "resume_app")
    assert "x:racing" in result["skipped"]
    assert "x:racing" in await coord.app_pauser.async_saved(CHILD)
