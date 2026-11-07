"""Web filter (Microsoft Edge): on/off switch, site lists, several sites per call."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.microsoft_family_safety import _register_services, sensor, switch
from custom_components.microsoft_family_safety.const import DOMAIN
from custom_components.microsoft_family_safety.coordinator import (
    FamilySafetyDataUpdateCoordinator,
)

CHILD = "1234567890123456"


class FakeWebAPI:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, object]] = []

    async def block_website(self, child_id, website):
        self.calls.append(("block", child_id, website))

    async def allow_website(self, child_id, website):
        self.calls.append(("allow", child_id, website))

    async def remove_website(self, child_id, website):
        self.calls.append(("remove", child_id, website))

    async def toggle_web_filter(self, child_id, enabled):
        self.calls.append(("toggle", child_id, enabled))


@pytest.fixture
def rig(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={"refresh_token": "x"})
    entry.add_to_hass(hass)
    coord = FamilySafetyDataUpdateCoordinator(hass, entry)
    coord.update_interval = None
    coord.web_api = FakeWebAPI()
    coord.async_request_refresh = AsyncMock()
    coord.data = {
        "accounts": {CHILD: {
            "first_name": "Alex",
            "web_browsing": {
                "enabled": True,
                "useAllowedListOnly": False,
                "exceptions": [
                    {"website": "https://www.Example.com/", "allowed": False},
                    {"website": "https://school.example.org", "allowed": True},
                ],
            },
        }},
        "devices": {},
    }
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coord
    return coord, entry


async def test_sensor_lists_blocked_and_allowed_sites(rig):
    coord, entry = rig
    attrs = sensor.FamilySafetyWebFilterSensor(coord, entry, CHILD).extra_state_attributes
    assert attrs["blocked_sites"] == ["www.example.com"]
    assert attrs["allowed_sites"] == ["school.example.org"]
    assert (attrs["blocked_site_count"], attrs["allowed_site_count"]) == (1, 1)
    assert attrs["allowed_sites_only"] is False


def test_site_lists_fall_back_to_the_derived_lists():
    lists = sensor.website_lists({"blockedSites": ["https://a.example/x"], "allowedSites": []})
    assert lists["blocked_sites"] == ["a.example"]
    assert "allowed_sites_only" not in lists


async def test_web_filter_switch_reads_and_writes(hass, rig):
    coord, entry = rig
    added: list = []
    await switch.async_setup_entry(hass, entry, added.extend)
    web_switch = next(e for e in added if isinstance(e, switch.FamilySafetyWebFilterSwitch))
    web_switch.hass = hass
    web_switch.entity_id = "switch.alex_web_filtering"
    assert web_switch.is_on is True

    await web_switch.async_turn_off()

    assert coord.web_api.calls == [("toggle", CHILD, False)]
    assert web_switch.is_on is False


async def test_website_services_take_several_sites(hass, rig):
    coord, _ = rig
    _register_services(hass)
    await hass.services.async_call(
        DOMAIN, "block_website",
        {"account_id": CHILD, "website": ["a.example", "b.example", "a.example"]},
        blocking=True,
    )
    await hass.services.async_call(
        DOMAIN, "remove_website", {"account_id": CHILD, "website": "c.example"}, blocking=True,
    )
    assert coord.web_api.calls == [
        ("block", CHILD, "a.example"),
        ("block", CHILD, "b.example"),
        ("remove", CHILD, "c.example"),
    ]
