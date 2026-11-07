"""microsoft_family_safety_request fires once for each new pending request."""

from __future__ import annotations

from pytest_homeassistant_custom_component.common import MockConfigEntry, async_capture_events

from custom_components.microsoft_family_safety.const import DOMAIN, EVENT_REQUEST
from custom_components.microsoft_family_safety.coordinator import (
    FamilySafetyDataUpdateCoordinator,
)

CHILD = "1234567890123456"


def _request(rid, **extra):
    return {"id": rid, "puid": int(CHILD), "type": "ScreenTime", "platform": "Windows",
            "requestedTime": 3600000, **extra}


async def test_new_requests_fire_once_after_the_baseline(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={"refresh_token": "x"})
    entry.add_to_hass(hass)
    coord = FamilySafetyDataUpdateCoordinator(hass, entry)
    accounts = {CHILD: {"first_name": "Alex"}}
    events = async_capture_events(hass, EVENT_REQUEST)

    coord._fire_request_events([_request("r1")], accounts)  # first poll: baseline
    coord._fire_request_events([_request("r1"), _request("r2", appId="x:1", displayName="Racing")], accounts)
    coord._fire_request_events([_request("r2")], accounts)  # nothing new
    await hass.async_block_till_done()

    assert len(events) == 1
    data = events[0].data
    assert data["request_id"] == "r2"
    assert data["account_id"] == CHILD
    assert data["first_name"] == "Alex"
    assert (data["app_id"], data["app_name"], data["platform"]) == ("x:1", "Racing", "Windows")
    assert data["requested_time"] == 3600000
