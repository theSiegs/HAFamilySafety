"""microsoft_family_safety_request_resolved fires once when a request leaves the list."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_capture_events

from custom_components.microsoft_family_safety.const import DOMAIN, EVENT_REQUEST_RESOLVED
from custom_components.microsoft_family_safety.coordinator import (
    FamilySafetyDataUpdateCoordinator,
)

CHILD = "1234567890123456"


def _request(rid):
    return {"id": rid, "puid": int(CHILD), "type": "ScreenTime", "platform": "Xbox"}


@pytest.fixture
def coord(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={"refresh_token": "x"})
    entry.add_to_hass(hass)
    coordinator = FamilySafetyDataUpdateCoordinator(hass, entry)
    coordinator.api = SimpleNamespace(
        approve_pending_request=AsyncMock(return_value=True),
        deny_pending_request=AsyncMock(return_value=True),
    )
    return coordinator


async def test_answered_here_resolves_on_the_next_poll(hass, coord):
    events = async_capture_events(hass, EVENT_REQUEST_RESOLVED)
    coord._fire_request_events([_request("r1"), _request("r2")], {})
    assert await coord.async_approve_request("r1", 1800)
    assert await coord.async_deny_request("r2")
    coord._fire_request_events([], {})
    await hass.async_block_till_done()

    by_id = {e.data["request_id"]: e.data for e in events}
    assert by_id["r1"]["outcome"] == "approved"
    assert by_id["r1"]["minutes"] == 30
    assert by_id["r1"]["via"] == "service"
    assert by_id["r1"]["account_id"] == CHILD
    assert by_id["r2"]["outcome"] == "denied"
    assert by_id["r2"]["minutes"] is None
    assert len(events) == 2


async def test_answered_in_the_app_resolves_once_it_stays_gone(hass, coord):
    events = async_capture_events(hass, EVENT_REQUEST_RESOLVED)
    coord._fire_request_events([_request("r1")], {})
    # One empty read is not an answer.
    coord._fire_request_events([], {})
    coord._fire_request_events([_request("r1")], {})
    await hass.async_block_till_done()
    assert events == []

    for _ in range(3):
        coord._fire_request_events([], {})
    await hass.async_block_till_done()
    assert len(events) == 1
    data = events[0].data
    assert (data["request_id"], data["outcome"], data["via"]) == ("r1", "unknown", "app")
    assert data["entry_id"] == coord.entry.entry_id


async def test_the_first_poll_resolves_nothing(hass, coord):
    events = async_capture_events(hass, EVENT_REQUEST_RESOLVED)
    coord._fire_request_events([], {})
    coord._fire_request_events([], {})
    await hass.async_block_till_done()
    assert events == []
