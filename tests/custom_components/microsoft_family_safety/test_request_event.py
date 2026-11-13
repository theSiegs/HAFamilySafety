"""microsoft_family_safety_request fires once for each new pending request."""

from __future__ import annotations

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_capture_events

from custom_components.microsoft_family_safety.const import DOMAIN, EVENT_REQUEST
from custom_components.microsoft_family_safety.coordinator import (
    FamilySafetyDataUpdateCoordinator,
)

CHILD = "1234567890123456"


def _request(rid, **extra):
    return {"id": rid, "puid": int(CHILD), "type": "ScreenTime", "platform": "Windows",
            "requestedTime": 3600000, **extra}


@pytest.fixture
def coord(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={"refresh_token": "x"})
    entry.add_to_hass(hass)
    return FamilySafetyDataUpdateCoordinator(hass, entry)


async def test_new_requests_fire_once_after_the_baseline(hass, coord):
    accounts = {CHILD: {"first_name": "Alex"}}
    events = async_capture_events(hass, EVENT_REQUEST)

    coord._fire_request_events([_request("r1")], accounts)  # first poll: baseline
    coord._fire_request_events(
        [_request("r1"), _request("r2", appId="x:1", displayName="Racing")], accounts
    )
    coord._fire_request_events([_request("r2")], accounts)  # nothing new
    await hass.async_block_till_done()

    assert len(events) == 1
    data = events[0].data
    assert data["request_id"] == "r2"
    assert data["account_id"] == CHILD
    assert data["first_name"] == "Alex"
    assert (data["app_id"], data["app_name"], data["platform"]) == ("x:1", "Racing", "Windows")
    assert data["requested_time"] == 3600000
    assert data["entry_id"] == coord.entry.entry_id


async def test_request_ids_survive_an_empty_poll(hass, coord):
    events = async_capture_events(hass, EVENT_REQUEST)
    request = _request("r1")
    coord._fire_request_events([request], {})  # baseline
    coord._fire_request_events([], {})  # a failed or empty read
    coord._fire_request_events([request], {})
    await hass.async_block_till_done()
    assert events == []

    # Gone for three polls in a row: forgotten, so it is news when it returns.
    for _ in range(3):
        coord._fire_request_events([], {})
    coord._fire_request_events([request], {})
    await hass.async_block_till_done()
    assert len(events) == 1


async def test_requests_without_an_id_are_ignored(hass, coord):
    events = async_capture_events(hass, EVENT_REQUEST)
    coord._fire_request_events([], {})
    coord._fire_request_events([{"puid": 1}, "junk"], {})
    await hass.async_block_till_done()
    assert events == []
