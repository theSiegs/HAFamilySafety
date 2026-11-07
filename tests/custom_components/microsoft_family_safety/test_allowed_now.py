"""Allowed now / time remaining: one pure rule, used by both entity kinds."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.util import dt as dt_util

from custom_components.microsoft_family_safety import binary_sensor, sensor
from custom_components.microsoft_family_safety.const import DAY_KEYS, DOMAIN
from custom_components.microsoft_family_safety.coordinator import (
    FamilySafetyDataUpdateCoordinator,
)
from custom_components.microsoft_family_safety.screen_time import (
    iso_utc,
    platform_allowance,
)

CHILD = "1234567890123456"
# A Wednesday, 16:10 local.
NOW = datetime(2026, 11, 4, 16, 10, tzinfo=dt_util.get_default_time_zone())


def _policy(allowance="02:00:00", begin="07:00:00", end="20:00:00", enabled=True):
    return {
        "isEnabled": enabled,
        "dailyRestrictions": {
            key: {"allowance": allowance, "allottedIntervals": [{"begin": begin, "end": end}]}
            for key in DAY_KEYS
        },
    }


def _at(hour, minute=0):
    return NOW.replace(hour=hour, minute=minute)


@pytest.mark.parametrize(
    ("policy", "used", "now", "override", "allowed", "remaining", "reason"),
    [
        # Inside the window, allowance left: the smaller of the two.
        (_policy(), 30, NOW, None, True, 90, "within_limits"),
        (_policy(), 30, _at(19, 30), None, True, 30, "within_limits"),
        # Allowance used up, or past it.
        (_policy(), 120, NOW, None, False, 0, "allowance_used"),
        (_policy(), 150, NOW, None, False, 0, "allowance_used"),
        # Outside the allowed hours, whatever the allowance.
        (_policy(), 0, _at(6, 59), None, False, 0, "outside_allowed_hours"),
        (_policy(), 0, _at(20, 0), None, False, 0, "outside_allowed_hours"),
        # Window ending at midnight in .NET notation.
        (_policy("24:00:00", "00:00:00", "1.00:00:00"), 600, _at(23, 0), None, True, 60, "within_limits"),
        # Limits off: allowed until midnight.
        (_policy(enabled=False), 999, _at(22, 0), None, True, 120, "no_limits"),
        # Unknown usage or schedule.
        (_policy(), None, NOW, None, None, None, "unknown"),
        (None, 10, NOW, None, None, None, "unknown"),
        # Zero allowance (the account lock).
        (_policy("00:00:00"), 0, NOW, None, False, 0, "allowance_used"),
    ],
)
def test_platform_allowance(policy, used, now, override, allowed, remaining, reason):
    status = platform_allowance(policy, used, now, override)
    assert (status["allowed"], status["remaining_minutes"], status["reason"]) == (
        allowed, remaining, reason
    )


def test_an_active_lock_wins_over_everything():
    lock = {"type": "blockUntil", "valid_until": iso_utc(NOW + timedelta(hours=1))}
    status = platform_allowance(_policy(enabled=False), 0, NOW, lock)
    assert (status["allowed"], status["remaining_minutes"], status["reason"]) == (False, 0, "locked")
    # A lock known only from the blocked platforms (no end) counts too.
    status = platform_allowance(_policy(), 0, NOW, {"type": "blockUntil", "valid_until": None})
    assert status["allowed"] is False


def test_an_expired_lock_is_ignored():
    lock = {"type": "blockUntil", "valid_until": iso_utc(NOW - timedelta(minutes=1))}
    assert platform_allowance(_policy(), 30, NOW, lock)["reason"] == "within_limits"


def test_extra_time_allows_until_it_ends():
    extra = {"type": "allowUntil", "valid_until": iso_utc(_at(21, 0) + timedelta(minutes=45))}
    status = platform_allowance(_policy(), 500, _at(21, 0), extra)
    assert (status["allowed"], status["remaining_minutes"], status["reason"]) == (
        True, 45, "extra_time"
    )


@pytest.fixture
def coordinator(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={"refresh_token": "x"})
    entry.add_to_hass(hass)
    coord = FamilySafetyDataUpdateCoordinator(hass, entry)
    coord.update_interval = None
    coord.data = {
        "accounts": {
            CHILD: {
                "first_name": "Alex",
                "devices": ["pc", "box"],
                "blocked_platforms": [],
                "screentime_policy": _policy(),
                "xbox_screentime_policy": _policy("01:00:00"),
                "xbox_today_usage": 20,
            }
        },
        "devices": {
            "pc": {"os_name": "Windows", "today_time_used": 25, "account_id": CHILD},
            "box": {"os_name": "Xbox", "device_class": "Xbox", "today_time_used": 20, "account_id": CHILD},
        },
    }
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coord
    return coord, entry


async def test_coordinator_feeds_each_platform_its_own_usage(coordinator):
    coord, _ = coordinator
    windows = coord.platform_status(CHILD, "Windows", NOW)
    xbox = coord.platform_status(CHILD, "Xbox", NOW)
    assert (windows["used_minutes"], windows["remaining_minutes"]) == (25, 95)
    assert (xbox["used_minutes"], xbox["remaining_minutes"]) == (20, 40)


async def test_xbox_lock_in_the_policy_makes_it_zero(coordinator):
    coord, _ = coordinator
    coord.data["accounts"][CHILD]["xbox_policy"] = {
        "data": {"screenTimeOverride": {
            "screenTimeOverrideType": "blockUntil",
            "validUntil": iso_utc(dt_util.utcnow() + timedelta(hours=1)),
        }}
    }
    status = coord.platform_status(CHILD, "Xbox")
    assert (status["allowed"], status["remaining_minutes"]) == (False, 0)


async def test_windows_lock_from_blocked_platforms(coordinator):
    coord, _ = coordinator
    coord.data["accounts"][CHILD]["blocked_platforms"] = ["Windows"]
    assert coord.platform_status(CHILD, "Windows")["allowed"] is False


async def test_entities_for_both_platforms(hass, coordinator):
    coord, entry = coordinator
    binaries: list = []
    await binary_sensor.async_setup_entry(hass, entry, binaries.extend)
    sensors: list = []
    await sensor.async_setup_entry(hass, entry, sensors.extend)

    assert sorted(e.unique_id.split("_", 2)[-1] for e in binaries) == [
        "windows_allowed_now", "xbox_allowed_now"
    ]
    remaining = [e for e in sensors if isinstance(e, sensor.FamilySafetyTimeRemainingSensor)]
    assert sorted(e.unique_id.split("_", 2)[-1] for e in remaining) == [
        "windows_time_remaining", "xbox_time_remaining"
    ]
    xbox = next(e for e in remaining if e._platform == "Xbox")
    # Unique id unchanged from before, so the existing entity keeps its id.
    assert xbox.unique_id == f"{entry.entry_id}_{CHILD}_xbox_time_remaining"
    assert xbox.extra_state_attributes["reason"] in {
        "within_limits", "outside_allowed_hours", "allowance_used"
    }


async def test_minute_tick_writes_only_when_something_changed(hass, coordinator):
    coord, entry = coordinator
    entity = binary_sensor.FamilySafetyAllowedNowSensor(coord, entry, CHILD, "Windows")
    writes: list = []
    entity.hass = hass
    entity.entity_id = "binary_sensor.alex_windows_allowed_now"
    # Count writes that reach Home Assistant.
    import homeassistant.helpers.entity as ha_entity

    original = ha_entity.Entity.async_write_ha_state
    ha_entity.Entity.async_write_ha_state = lambda self: writes.append(1)
    try:
        entity.async_write_ha_state()
        entity._async_minute_tick(dt_util.now())
        entity._async_minute_tick(dt_util.now())
        assert len(writes) == 1
        coord.data["accounts"][CHILD]["blocked_platforms"] = ["Windows"]
        entity._async_minute_tick(dt_util.now())
        assert len(writes) == 2
    finally:
        ha_entity.Entity.async_write_ha_state = original


async def test_volatile_attributes_are_not_recorded_and_user_id_is_there(hass, coordinator):
    coord, entry = coordinator
    allowed = binary_sensor.FamilySafetyAllowedNowSensor(coord, entry, CHILD, "Xbox")
    remaining = sensor.FamilySafetyXboxTimeRemainingSensor(coord, entry, CHILD)
    assert {"remaining_minutes", "used_minutes"} <= allowed._unrecorded_attributes
    assert "used_minutes" in remaining._unrecorded_attributes
    assert allowed.extra_state_attributes["user_id"] == CHILD
    assert remaining.extra_state_attributes["user_id"] == CHILD
