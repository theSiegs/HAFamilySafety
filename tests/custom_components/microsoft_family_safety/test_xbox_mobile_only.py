"""Xbox daily limits run on the mobile API, with or without the web session.

An entry without the Family web session runs on the mobile token only. The
Xbox schedule, its on/off switch and its daily limits all go through the
mobile /v4 schedules endpoint. The Xbox lock comes with the Xbox Family
Settings sign-in (test_xbox_xapprove.py); Xbox play time (web only) is gone.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.microsoft_family_safety import number, sensor, switch
from custom_components.microsoft_family_safety.api_client import FamilySafetyWebAPI
from custom_components.microsoft_family_safety.const import (
    CONF_PLATFORMS,
    CONF_XBOX_FEATURES,
    DAY_KEYS,
    DOMAIN,
)
from custom_components.microsoft_family_safety.coordinator import (
    FamilySafetyDataUpdateCoordinator,
)

CHILD = "1234567890123456"


def _xbox_policy(*, enabled: bool = True) -> dict:
    return {
        "isEnabled": enabled,
        "appliesTo": "Xbox",
        "dailyRestrictions": {
            key: {"allowance": "02:00:00", "allottedIntervals": []} for key in DAY_KEYS
        },
    }


class FakeWebAPI:
    """The mobile reads the per-account fetch makes; records Xbox calls."""

    def __init__(self) -> None:
        self.xbox_reads: list[str] = []
        self.xbox_allowances: list[tuple[str, int, int, int]] = []
        self.enabled_calls: list[tuple[str, bool, str]] = []
        self.last_web_error_code = None
        self.web_session_state = "unknown"

    async def get_web_browsing_settings(self, child_id):
        return None

    async def get_content_settings(self, child_id):
        return None

    async def get_app_limits_enabled(self, child_id, platform="Windows"):
        return None

    async def get_mobile_xbox_screentime_policy(self, child_id):
        self.xbox_reads.append(child_id)
        return _xbox_policy()

    async def set_mobile_xbox_screentime_allowance(self, child_id, day, hours, minutes):
        self.xbox_allowances.append((child_id, day, hours, minutes))
        return True

    async def set_mobile_schedule_enabled(self, child_id, enabled, platform="Windows"):
        self.enabled_calls.append((child_id, enabled, platform))
        return True


def _entry(hass, *, xbox_features: bool) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"refresh_token": "x"},
        options={
            CONF_XBOX_FEATURES: xbox_features,
            CONF_PLATFORMS: ["Windows", "Xbox"],
        },
    )
    entry.add_to_hass(hass)
    return entry


def _coordinator(hass, entry, account: dict[str, Any]) -> FamilySafetyDataUpdateCoordinator:
    coord = FamilySafetyDataUpdateCoordinator(hass, entry)
    coord.web_api = FakeWebAPI()
    coord._fetch_screentime_policy = AsyncMock(return_value=None)
    coord.async_request_refresh = AsyncMock()
    # The platforms add listeners; no polling timer in these tests.
    coord.update_interval = None
    coord.data = {
        "accounts": {CHILD: {"first_name": "Alex", "applications": [], **account}},
        "devices": {},
    }
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coord
    return coord


async def _entities(hass, entry, platform) -> list:
    added: list = []
    await platform.async_setup_entry(hass, entry, added.extend)
    return added


@pytest.mark.parametrize("xbox_features", [False, True])
async def test_xbox_schedule_is_read_from_the_mobile_api_in_both_modes(
    hass, xbox_features: bool
) -> None:
    coord = _coordinator(hass, _entry(hass, xbox_features=xbox_features), {})
    assert coord.web_session_enabled is xbox_features

    result = await coord._fetch_web_api_data(CHILD)

    assert coord.web_api.xbox_reads == [CHILD]
    assert result["xbox_screentime_policy"] == _xbox_policy()


async def test_mobile_only_creates_the_xbox_schedule_switch_but_no_xbox_lock(hass) -> None:
    entry = _entry(hass, xbox_features=False)
    _coordinator(hass, entry, {"xbox_screentime_policy": _xbox_policy()})

    entities = await _entities(hass, entry, switch)

    assert sum(isinstance(e, switch.FamilySafetyXboxScheduleSwitch) for e in entities) == 1
    locks = [e for e in entities if isinstance(e, switch.FamilySafetyPlatformLockSwitch)]
    assert [e._platform.lower() for e in locks] == ["windows"]


async def test_mobile_only_creates_the_xbox_limits_once_the_schedule_is_on(hass) -> None:
    entry = _entry(hass, xbox_features=False)
    coord = _coordinator(
        hass, entry, {"xbox_screentime_policy": _xbox_policy(enabled=False)}
    )
    added: list = []
    await number.async_setup_entry(hass, entry, added.extend)
    assert not [e for e in added if isinstance(e, number.FamilySafetyXboxDailyLimitNumber)]

    coord.data["accounts"][CHILD]["xbox_screentime_policy"] = _xbox_policy()
    coord.async_update_listeners()

    xbox = [e for e in added if isinstance(e, number.FamilySafetyXboxDailyLimitNumber)]
    assert sorted(e._day_key for e in xbox) == sorted(DAY_KEYS)
    assert all(e.native_value == 120 for e in xbox)


async def test_without_the_xbox_sign_in_there_is_no_xbox_lock_or_override(hass) -> None:
    entry = _entry(hass, xbox_features=True)
    _coordinator(hass, entry, {"xbox_screentime_policy": _xbox_policy()})

    sensors = await _entities(hass, entry, sensor)
    switches = await _entities(hass, entry, switch)

    assert not [e for e in sensors if isinstance(e, sensor.FamilySafetyXboxOverrideSensor)]
    locks = [e for e in switches if isinstance(e, switch.FamilySafetyPlatformLockSwitch)]
    assert [e._platform.lower() for e in locks] == ["windows"]


async def test_mobile_only_writes_the_xbox_schedule_through_the_mobile_api(hass) -> None:
    coord = _coordinator(
        hass,
        _entry(hass, xbox_features=False),
        {"xbox_screentime_policy": _xbox_policy()},
    )

    await coord.async_set_xbox_screentime_limit(CHILD, 1, 1, 30)
    await coord.async_set_xbox_schedule_enabled(CHILD, False)

    assert coord.web_api.xbox_allowances == [(CHILD, 1, 1, 30)]
    assert coord.web_api.enabled_calls == [(CHILD, False, "Xbox")]
    policy = coord.data["accounts"][CHILD]["xbox_screentime_policy"]
    assert policy["dailyRestrictions"]["monday"]["allowance"] == "01:30:00"
    assert policy["isEnabled"] is False


async def test_xbox_schedule_calls_never_touch_the_web_session() -> None:
    """Read, allowance and on/off all go through the mobile _request only."""
    api = FamilySafetyWebAPI(authenticator=None)
    payload = {
        "mode": "PerDeviceType",
        "schedules": [
            {
                "appliesTo": "Xbox",
                "enabled": True,
                "dailyRestrictions": {
                    key: {"allowance": 7_200_000, "allottedIntervals": []}
                    for key in DAY_KEYS
                },
            }
        ],
    }
    calls: list[tuple[str, str]] = []

    async def mobile_request(method, path, json_data=None, params=None, extra_headers=None):
        calls.append((method, path))
        return payload if method == "GET" else None

    async def web_request(*args, **kwargs):
        raise AssertionError("the Xbox schedule must not use the web session")

    api._request = mobile_request
    api._web_request = web_request

    policy = await api.get_mobile_xbox_screentime_policy(CHILD)
    await api.set_mobile_xbox_screentime_allowance(CHILD, 1, 1, 30)
    await api.set_mobile_schedule_enabled(CHILD, False, platform="Xbox")

    assert policy["isEnabled"] is True
    assert policy["dailyRestrictions"]["monday"]["allowance"] == "02:00:00"
    path = f"/v4/devicelimits/schedules/{CHILD}"
    assert calls == [
        ("GET", path),
        ("GET", path),
        ("PATCH", path),
        ("GET", path),
        ("PATCH", path),
    ]
