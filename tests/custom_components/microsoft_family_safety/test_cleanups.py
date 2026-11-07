"""Small fixes: shared HTTP session, saved-schedule accessor, sensor attributes."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import aiohttp
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.microsoft_family_safety import sensor
from custom_components.microsoft_family_safety.api_client import FamilySafetyWebAPI
from custom_components.microsoft_family_safety.const import DOMAIN
from custom_components.microsoft_family_safety.coordinator import (
    FamilySafetyDataUpdateCoordinator,
)

CHILD = "1234567890123456"


async def test_mobile_api_uses_the_shared_session_and_never_closes_it() -> None:
    shared = Mock(spec=aiohttp.ClientSession)
    shared.closed = False
    shared.close = AsyncMock()
    api = FamilySafetyWebAPI(Mock(), session=shared)

    await api._ensure_session()
    assert api._session is shared

    await api.close()
    shared.close.assert_not_awaited()


def test_dead_schedule_write_is_gone() -> None:
    # It bypassed the schedule write lock and sent a body Microsoft rejects.
    assert not hasattr(FamilySafetyWebAPI, "set_screentime_daily_allowance")


async def test_saved_schedule_accessor_normalises_the_id(hass) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={"refresh_token": "x"})
    entry.add_to_hass(hass)
    coord = FamilySafetyDataUpdateCoordinator(hass, entry)
    coord._saved_screentime = {CHILD: {"dailyRestrictions": {}}}

    assert coord.has_saved_screentime(int(CHILD))
    assert coord.has_saved_screentime(CHILD)
    assert not coord.has_saved_screentime("42")


async def test_screen_time_sensors_carry_no_state_class_attribute(hass) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={"refresh_token": "x"})
    entry.add_to_hass(hass)
    coord = FamilySafetyDataUpdateCoordinator(hass, entry)
    coord.data = {
        "accounts": {CHILD: {"first_name": "Alex", "today_screentime_usage": 5}},
        "devices": {"dev1": {"device_name": "PC", "today_time_used": 3}},
    }
    account = sensor.FamilySafetyScreenTimeSensor(coord, entry, CHILD)
    device = sensor.FamilySafetyDeviceScreenTimeSensor(coord, entry, "dev1")

    for entity in (account, device):
        attrs = entity.extra_state_attributes
        assert "state_class" not in attrs
        assert attrs["date"]
