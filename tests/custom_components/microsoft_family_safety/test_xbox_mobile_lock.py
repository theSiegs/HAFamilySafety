"""The Xbox lock's fallback: today's Xbox schedule day.

When the Xbox Family Settings API cannot be used (here: no Xbox sign-in),
lock_platform on Xbox saves today's Xbox day and sets its allowance to 0
through the mobile schedule API; unlock_platform, the lock's end and
midnight put the saved day back, unless a parent changed it in the app
meanwhile. The Family web session is never used for the Xbox.
"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from custom_components.microsoft_family_safety import binary_sensor, switch
from custom_components.microsoft_family_safety.api_client import (
    FamilySafetyWebAPI,
    FamilySafetyWebAPIError,
)
from custom_components.microsoft_family_safety.const import (
    DAY_KEYS,
    DOMAIN,
)
from custom_components.microsoft_family_safety.coordinator import (
    PLATFORM_HOLD_STORAGE_KEY,
    FamilySafetyDataUpdateCoordinator,
)
from custom_components.microsoft_family_safety.screen_time import iso_utc, parse_utc
from custom_components.microsoft_family_safety.xbox_mobile_lock import (
    next_local_midnight,
    weekday_index,
)

CHILD = "1234567890123456"
TWO_HOURS = 2 * 3600 * 1000
EVENING = [{"begin": "07:00:00", "end": "21:00:00"}]


class FakeWeb:
    """The Xbox web override (down by default) and the mobile Xbox schedule."""

    def __init__(self) -> None:
        self.has_web_cookies = True
        self.family_context_state = "auth_required"
        self.last_isolated_error_code: str | None = None
        self.last_web_error_code: str | None = None
        self.web_fail: str | None = None
        self.web_writes: list[tuple[str, str]] = []
        self.days = {
            day: {"allowance": TWO_HOURS, "allottedIntervals": copy.deepcopy(EVENING)}
            for day in range(7)
        }
        self.enabled = True
        self.day_writes: list[tuple[int, int, list]] = []
        self.enable_writes: list[bool] = []
        self.read_fail = False
        self.write_fail = False

    # Web override ---------------------------------------------------------
    async def get_xbox_screentime_policy(self, child_id):
        if self.family_context_state != "ready":
            return None
        return {"data": {"dailyRestrictions": [], "screenTimeOverride": None}}

    async def set_platform_time_override(self, child_id, platform, kind, date_time):
        if self.web_fail:
            self.last_isolated_error_code = self.web_fail
            if self.web_fail == "HTTP_401":
                self.family_context_state = "auth_required"
            raise FamilySafetyWebAPIError(f"{platform} override web request failed ({self.web_fail})")
        self.web_writes.append((platform.lower(), kind))
        return True

    # Mobile schedule ------------------------------------------------------
    async def get_mobile_xbox_day(self, child_id, day_of_week):
        if self.read_fail:
            raise FamilySafetyWebAPIError("Mobile API request failed")
        return {**copy.deepcopy(self.days[day_of_week]), "enabled": self.enabled}

    async def set_mobile_xbox_day(self, child_id, day_of_week, allowance_ms, intervals):
        if self.write_fail:
            raise FamilySafetyWebAPIError("Mobile API request failed: status 500")
        self.days[day_of_week] = {
            "allowance": allowance_ms, "allottedIntervals": copy.deepcopy(intervals),
        }
        self.day_writes.append((day_of_week, allowance_ms, copy.deepcopy(intervals)))

    async def set_mobile_schedule_enabled(self, child_id, enabled, platform="Windows"):
        assert platform == "Xbox"
        self.enabled = enabled
        self.enable_writes.append(enabled)

    def policy(self) -> dict:
        """The Xbox policy as a poll stores it (get_mobile_xbox_screentime_policy)."""
        return FamilySafetyWebAPI._convert_mobile_schedule({
            "enabled": self.enabled,
            "appliesTo": "Xbox",
            "dailyRestrictions": {DAY_KEYS[d]: v for d, v in self.days.items()},
        })


_MADE: list[FamilySafetyDataUpdateCoordinator] = []


@pytest.fixture(autouse=True)
def _stop_timers():
    """Home Assistant stopping: async_cleanup cancels the restore timers."""
    yield
    while _MADE:
        _MADE.pop()._cancel_all_mobile_restores()


def _make(hass, entry, web) -> FamilySafetyDataUpdateCoordinator:
    coord = FamilySafetyDataUpdateCoordinator(hass, entry)
    _MADE.append(coord)
    coord._has_web_credentials = True
    coord._accounts[CHILD] = MagicMock(user_id=CHILD)
    coord.async_request_refresh = AsyncMock()
    coord.web_api = web
    coord.data = {
        "accounts": {
            CHILD: {
                "blocked_platforms": [],
                "first_name": "Alex",
                "xbox_screentime_policy": web.policy(),
                "xbox_today_usage": 30,
            }
        },
        "devices": {},
    }
    return coord


def _poll(coord) -> None:
    """What a poll does with the Xbox schedule and our holds."""
    account = coord.data["accounts"][CHILD]
    account["xbox_screentime_policy"] = coord.web_api.policy()
    account["blocked_platforms"] = []
    coord._apply_xbox_mobile_lock_state(CHILD, account)


@pytest.fixture
def entry(hass):
    entry = MockConfigEntry(
        domain=DOMAIN, data={"refresh_token": "x"},
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
def web():
    return FakeWeb()


@pytest.fixture
def coord(hass, entry, web):
    return _make(hass, entry, web)


@pytest.fixture
def noon(freezer):
    """Local noon, so a lock of a few hours ends before midnight."""
    moment = dt_util.start_of_local_day() + timedelta(hours=12)
    freezer.move_to(moment)
    return moment


def _today() -> int:
    return weekday_index(dt_util.now())


def _in(hours: float) -> datetime:
    return dt_util.utcnow() + timedelta(hours=hours)


# Lock ------------------------------------------------------------------------


async def test_web_down_locks_today_through_the_schedule(coord, web, noon, hass_storage, entry):
    result = await coord.async_lock_platform(CHILD, "Xbox", _in(2))

    assert result["lock_path"] == "mobile_schedule"
    assert result["applied"]["type"] == "blockUntil"
    assert parse_utc(result["applied"]["valid_until"]) == parse_utc(iso_utc(_in(2)))
    assert result["saved_day"]["allowance_minutes"] == 120
    # Allowance 0, today's intervals kept as they were; no web write.
    assert web.day_writes == [(_today(), 0, EVENING)]
    assert web.web_writes == []
    assert web.enable_writes == []
    hold = coord.platform_hold(CHILD, "Xbox")
    assert hold["path"] == "mobile_schedule"
    assert hold["saved"] == {"allowance": TWO_HOURS, "allottedIntervals": EVENING}
    assert hold["confirmed"] is True
    assert f"{PLATFORM_HOLD_STORAGE_KEY}.{entry.entry_id}" in hass_storage


async def test_a_working_web_session_is_never_used_for_the_xbox(coord, web, noon):
    web.family_context_state = "ready"

    result = await coord.async_lock_platform(CHILD, "Xbox", _in(2))

    assert result["lock_path"] == "mobile_schedule"
    assert web.web_writes == []
    unlocked = await coord.async_unlock_platform(CHILD, "Xbox")
    assert unlocked["lock_path"] == "mobile_schedule"
    await coord.async_release_platform(CHILD, "Xbox")
    assert web.web_writes == []


async def test_unreadable_schedule_refuses_and_writes_nothing(coord, web, noon):
    web.read_fail = True

    with pytest.raises(HomeAssistantError, match="could not be read"):
        await coord.async_lock_platform(CHILD, "Xbox", _in(2))
    assert web.day_writes == []
    assert coord.platform_hold(CHILD, "Xbox") is None


async def test_lock_never_outlives_midnight(coord, web, noon):
    result = await coord.async_lock_platform(CHILD, "Xbox", _in(30))

    midnight = next_local_midnight(dt_util.now())
    assert parse_utc(result["applied"]["valid_until"]) == parse_utc(iso_utc(midnight))


async def test_schedule_turned_off_is_turned_on_and_back_off(coord, web, noon):
    web.enabled = False

    await coord.async_lock_platform(CHILD, "Xbox", _in(2))
    assert web.enable_writes == [True]

    result = await coord.async_unlock_platform(CHILD, "Xbox")
    assert result["applied"]["type"] == "none"
    assert web.enable_writes == [True, False]
    assert web.days[_today()]["allowance"] == TWO_HOURS


async def test_relock_keeps_the_saved_day_and_the_later_end(coord, web, noon):
    await coord.async_lock_platform(CHILD, "Xbox", _in(3))
    second = await coord.async_lock_platform(CHILD, "Xbox", _in(1))

    assert parse_utc(second["applied"]["valid_until"]) == parse_utc(iso_utc(_in(3)))
    assert coord.platform_hold(CHILD, "Xbox")["saved"]["allowance"] == TWO_HOURS
    assert len(web.day_writes) == 1  # still ours: nothing written again


async def test_relock_after_a_parent_change_leaves_the_parents_day(coord, web, noon):
    await coord.async_lock_platform(CHILD, "Xbox", _in(2))
    web.days[_today()] = {"allowance": 3600 * 1000, "allottedIntervals": EVENING}
    writes = len(web.day_writes)

    result = await coord.async_lock_platform(CHILD, "Xbox", _in(2))
    assert result["applied"] == "left_parent_override"
    assert result["lock_path"] == "mobile_schedule"
    assert result["current"]["allowance_minutes"] == 60
    assert len(web.day_writes) == writes
    assert web.days[_today()]["allowance"] == 3600 * 1000
    assert coord.platform_hold(CHILD, "Xbox") is None


# Unlock ----------------------------------------------------------------------


async def test_unlock_restores_the_saved_day_exactly(coord, web, noon):
    web.days[_today()] = {
        "allowance": 5400 * 1000,
        "allottedIntervals": [
            {"begin": "07:00:00", "end": "12:00:00"},
            {"begin": "15:30:00", "end": "1.00:00:00"},
        ],
    }
    before = copy.deepcopy(web.days[_today()])
    await coord.async_lock_platform(CHILD, "Xbox", _in(2))

    result = await coord.async_unlock_platform(CHILD, "Xbox")

    assert result["applied"] == {"type": "none", "valid_until": None}
    assert result["lock_path"] == "mobile_schedule"
    assert web.days[_today()] == before
    assert web.day_writes[-1] == (_today(), before["allowance"], before["allottedIntervals"])
    assert coord.platform_hold(CHILD, "Xbox") is None


async def test_unlock_keeps_a_parents_change(coord, web, noon):
    await coord.async_lock_platform(CHILD, "Xbox", _in(2))
    parent = {"allowance": 3 * 3600 * 1000, "allottedIntervals": EVENING}
    web.days[_today()] = copy.deepcopy(parent)
    writes = len(web.day_writes)

    result = await coord.async_unlock_platform(CHILD, "Xbox")

    assert result["applied"] == "left_parent_override"
    assert result["current"]["allowance_minutes"] == 180
    assert web.days[_today()] == parent
    assert len(web.day_writes) == writes
    assert coord.platform_hold(CHILD, "Xbox") is None


async def test_unlock_after_a_failed_write_is_not_locked(coord, web, noon):
    web.write_fail = True
    with pytest.raises(HomeAssistantError, match="refused"):
        await coord.async_lock_platform(CHILD, "Xbox", _in(2))
    web.write_fail = False

    result = await coord.async_unlock_platform(CHILD, "Xbox")

    assert result["applied"] == "not_locked"
    assert result["current"]["allowance_minutes"] == 120
    assert web.day_writes == []


async def test_unlock_with_nothing_held_and_web_down_is_not_locked(coord, web, noon):
    result = await coord.async_unlock_platform(CHILD, "Xbox")

    assert result["applied"] == "not_locked"
    assert result["current"]["allowance_minutes"] == 120
    assert web.day_writes == [] and web.web_writes == []


async def test_unreadable_day_at_unlock_keeps_the_hold(coord, web, noon):
    await coord.async_lock_platform(CHILD, "Xbox", _in(2))
    web.read_fail = True

    with pytest.raises(HomeAssistantError, match="lock is kept"):
        await coord.async_unlock_platform(CHILD, "Xbox")
    assert coord.platform_hold(CHILD, "Xbox") is not None
    assert web.days[_today()]["allowance"] == 0


async def test_switch_off_restores(hass, coord, web, noon, entry):
    await coord.async_lock_platform(CHILD, "Xbox", _in(2))

    await coord.async_release_platform(CHILD, "Xbox")

    assert web.days[_today()]["allowance"] == TWO_HOURS
    assert coord.platform_hold(CHILD, "Xbox") is None


# The lock's end, midnight and restarts ------------------------------------------


async def test_day_is_put_back_at_the_lock_end(hass, coord, web, noon, freezer):
    await coord.async_lock_platform(CHILD, "Xbox", _in(1))
    assert web.days[_today()]["allowance"] == 0

    later = dt_util.utcnow() + timedelta(hours=1, seconds=1)
    freezer.move_to(later)
    async_fire_time_changed(hass, later)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert web.days[weekday_index(dt_util.as_local(later))]["allowance"] == TWO_HOURS
    assert coord.platform_hold(CHILD, "Xbox") is None


async def test_day_is_put_back_at_midnight_and_tomorrow_is_untouched(
    hass, coord, web, freezer
):
    late = dt_util.start_of_local_day() + timedelta(hours=23)
    freezer.move_to(late)
    locked_day = _today()
    await coord.async_lock_platform(CHILD, "Xbox", _in(10))

    after = next_local_midnight(late) + timedelta(seconds=1)
    freezer.move_to(after)
    async_fire_time_changed(hass, after)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert web.days[locked_day]["allowance"] == TWO_HOURS
    assert all(day["allowance"] == TWO_HOURS for day in web.days.values())
    assert [w[0] for w in web.day_writes] == [locked_day, locked_day]
    assert coord.platform_hold(CHILD, "Xbox") is None


async def test_restart_past_the_end_restores_on_the_first_poll(
    hass, entry, web, noon, freezer
):
    first = _make(hass, entry, web)
    await first.async_lock_platform(CHILD, "Xbox", _in(1))
    first._cancel_all_mobile_restores()  # Home Assistant stops

    later = dt_util.utcnow() + timedelta(hours=3)
    freezer.move_to(later)
    restarted = _make(hass, entry, web)
    await restarted._async_check_mobile_holds()

    assert web.days[_today()]["allowance"] == TWO_HOURS
    assert restarted.platform_hold(CHILD, "Xbox") is None


async def test_restart_before_the_end_arms_the_timer(hass, entry, web, noon):
    first = _make(hass, entry, web)
    await first.async_lock_platform(CHILD, "Xbox", _in(1))
    first._cancel_all_mobile_restores()

    restarted = _make(hass, entry, web)
    await restarted._async_check_mobile_holds()

    assert web.days[_today()]["allowance"] == 0
    assert list(restarted._mobile_restore_timers) == [restarted._hold_key(CHILD, "Xbox")]
    restarted._cancel_all_mobile_restores()


async def test_failed_restore_is_retried_on_the_next_poll(hass, coord, web, noon, freezer):
    await coord.async_lock_platform(CHILD, "Xbox", _in(1))
    coord._cancel_all_mobile_restores()
    freezer.move_to(dt_util.utcnow() + timedelta(hours=2))
    web.read_fail = True
    await coord._async_check_mobile_holds()
    assert coord.platform_hold(CHILD, "Xbox") is not None
    issue_id = coord._not_restored_issue_id(coord._hold_key(CHILD, "Xbox"))
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert issue is not None and issue.severity == ir.IssueSeverity.ERROR

    web.read_fail = False
    await coord._async_check_mobile_holds()
    assert web.days[_today()]["allowance"] == TWO_HOURS
    assert coord.platform_hold(CHILD, "Xbox") is None
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def test_unlock_after_the_end_reports_hold_expired_and_restores(coord, web, noon, freezer):
    await coord.async_lock_platform(CHILD, "Xbox", _in(1))
    coord._cancel_all_mobile_restores()
    freezer.move_to(dt_util.utcnow() + timedelta(hours=2))

    result = await coord.async_unlock_platform(CHILD, "Xbox")

    assert result["applied"] == "hold_expired"
    assert result["restored"] is True
    assert web.days[_today()]["allowance"] == TWO_HOURS


# What the entities show ------------------------------------------------------


async def test_entities_while_the_schedule_lock_holds(hass, coord, web, noon, entry):
    await coord.async_lock_platform(CHILD, "Xbox", _in(2))
    _poll(coord)

    state = coord.platform_hold_state(CHILD, "Xbox")
    until = parse_utc(coord.platform_hold(CHILD, "Xbox")["until"])
    assert state == {
        "hold": "ours",
        "override": "blockUntil",
        "override_until": dt_util.as_local(until).isoformat(),
        "allow_until": None,
        "lock_path": "mobile_schedule",
    }
    status = coord.platform_status(CHILD, "Xbox")
    assert status["allowed"] is False
    assert status["reason"] == "locked"
    # The saved allowance, not the 0 written to lock.
    assert status["allowance_minutes"] == 120

    lock = switch.FamilySafetyPlatformLockSwitch(coord, entry, CHILD, "Alex", "Xbox")
    assert lock.is_on is True
    attrs = lock.extra_state_attributes
    assert attrs["hold"] == "ours" and attrs["lock_path"] == "mobile_schedule"
    assert attrs["user_id"] == CHILD and attrs["platform"] == "Xbox"

    allowed = binary_sensor.FamilySafetyAllowedNowSensor(coord, entry, CHILD, "Xbox")
    assert allowed.is_on is False
    attrs = allowed.extra_state_attributes
    assert attrs["hold"] == "ours"
    assert attrs["allowance_minutes"] == 120
    assert attrs["override"] == "blockUntil"


async def test_entities_after_a_parent_change(hass, coord, web, noon, entry):
    await coord.async_lock_platform(CHILD, "Xbox", _in(2))
    web.days[_today()] = {"allowance": 3 * 3600 * 1000, "allottedIntervals": EVENING}
    _poll(coord)

    state = coord.platform_hold_state(CHILD, "Xbox")
    assert state["hold"] == "none" and state["override"] == "none"
    status = coord.platform_status(CHILD, "Xbox")
    # Not locked; whether the allowance is used up is unknown without Xbox
    # play time (it came from the Family web session only).
    assert status["allowed"] is None and status["reason"] != "locked"
    assert status["allowance_minutes"] == 180
    lock = switch.FamilySafetyPlatformLockSwitch(coord, entry, CHILD, "Alex", "Xbox")
    assert "Xbox" not in coord.data["accounts"][CHILD]["blocked_platforms"]


async def test_state_right_after_the_lock_before_a_poll(coord, web, noon):
    await coord.async_lock_platform(CHILD, "Xbox", _in(2))
    # No poll yet: the policy was updated locally with the write.
    assert coord.platform_hold_state(CHILD, "Xbox")["hold"] == "ours"
    assert coord.platform_status(CHILD, "Xbox")["allowance_minutes"] == 120


async def test_strict_mode_leaves_a_schedule_lock_alone(hass, web, noon):
    entry = MockConfigEntry(
        domain=DOMAIN, data={"refresh_token": "x"},
        options={"strict_mode": True},
    )
    entry.add_to_hass(hass)
    coord = _make(hass, entry, web)
    await coord.async_lock_platform(CHILD, "Xbox", _in(2))

    await coord._async_enforce_holds({CHILD: {"type": "none", "valid_until": None}})
    await hass.async_block_till_done()
    assert web.web_writes == []


# Extra time needs the Xbox Family Settings sign-in ------------------------------


async def test_extra_time_on_xbox_fails_clearly_without_the_xbox_sign_in(coord, web, noon):
    with pytest.raises(HomeAssistantError, match="Xbox Family Settings sign-in"):
        await coord.async_add_time_bonus(CHILD, "Xbox", _in(1))
    with pytest.raises(HomeAssistantError, match="Xbox Family Settings sign-in"):
        await coord.async_cancel_time_bonus(CHILD, "Xbox")
    assert web.web_writes == [] and web.day_writes == []
