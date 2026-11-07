"""lock_platform saves what was in force, unlock_platform puts it back.

The first lock of a platform reads Microsoft's override (a parent's own lock,
extra time, or nothing) and keeps it in a Store with the fact that this
integration holds a lock there. A later lock, after a failed call or a
restart, never takes its own lock for that prior state; unlock restores it
while it has not run out, else cancels.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from custom_components.microsoft_family_safety import _register_services
from custom_components.microsoft_family_safety.api_client import (
    FamilySafetyWebAPIError,
)
from custom_components.microsoft_family_safety.const import CONF_XBOX_FEATURES, DOMAIN
from custom_components.microsoft_family_safety.coordinator import (
    PLATFORM_HOLD_STORAGE_KEY,
    XBOX_LOCK_ISSUE,
    FamilySafetyDataUpdateCoordinator,
)
from custom_components.microsoft_family_safety.screen_time import (
    iso_utc,
    parse_platform_override,
    parse_utc,
)

CHILD = "1234567890123456"


def _xbox_policy(kind: str = "cancel", until: datetime | None = None) -> dict:
    until = until or dt_util.utcnow()
    return {
        "data": {
            "isEnabled": True,
            "dailyRestrictions": [],
            "screenTimeOverride": {
                "screenTimeOverrideType": kind,
                "validUntil": iso_utc(until),
            },
        }
    }


class FakeWebAPI:
    """Microsoft's Xbox and Windows overrides, as the web routes return them."""

    def __init__(self) -> None:
        self.has_web_cookies = True
        self.last_web_error_code = None
        self.xbox_policy: dict | None = _xbox_policy()
        self.windows: dict | None = {"data": {"screenTimeOverrides": []}}
        self.writes: list[tuple[str, str, str]] = []
        self.fail: str | None = None
        #: Microsoft applies the write but the call still fails.
        self.apply_then_fail = False

    async def get_xbox_screentime_policy(self, child_id):
        return self.xbox_policy if self.has_web_cookies else None

    async def get_device_overrides(self, child_id):
        return self.windows

    async def set_platform_time_override(self, child_id, platform, kind, date_time):
        if self.fail and not self.apply_then_fail:
            raise FamilySafetyWebAPIError(f"{platform} override web request failed ({self.fail})")
        self.writes.append((platform.lower(), kind, date_time))
        if platform.lower() == "xbox":
            self.xbox_policy = _xbox_policy(kind, parse_utc(date_time))
        else:
            entries = [] if kind == "cancel" else [
                {"screenTimeOverrideType": kind, "validUntil": date_time}
            ]
            self.windows = {"data": {"screenTimeOverrides": entries}}
        if self.fail:
            raise FamilySafetyWebAPIError(f"{platform} override web request failed ({self.fail})")
        return True


def _make(hass, entry) -> FamilySafetyDataUpdateCoordinator:
    coord = FamilySafetyDataUpdateCoordinator(hass, entry)
    coord._has_web_credentials = True
    coord._accounts[CHILD] = MagicMock(user_id=CHILD)
    coord.async_request_refresh = AsyncMock()
    coord.data = {"accounts": {CHILD: {"blocked_platforms": []}}, "devices": {}}
    return coord


@pytest.fixture
def entry(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={"refresh_token": "x"})
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
def web():
    return FakeWebAPI()


@pytest.fixture
def coordinator(hass, entry, web):
    coord = _make(hass, entry)
    coord.web_api = web
    return coord


def _hours(hours: float) -> datetime:
    return dt_util.utcnow() + timedelta(hours=hours)


async def test_xbox_lock_writes_block_until_and_saves_nothing_in_force(coordinator, web):
    result = await coordinator.async_lock_platform(CHILD, "Xbox", _hours(2))

    assert [(p, k) for p, k, _ in web.writes] == [("xbox", "blockUntil")]
    assert result["prior"]["type"] == "none"
    assert result["applied"]["type"] == "blockUntil"
    assert result["applied"]["valid_until"] == web.writes[0][2]
    hold = coordinator.platform_hold(CHILD, "Xbox")
    assert hold["confirmed"] is True
    assert "Xbox" in coordinator._merge_blocked_platforms(coordinator._accounts[CHILD])


async def test_unlock_restores_the_parent_lock_in_force_before(coordinator, web):
    parent_until = _hours(1)
    web.xbox_policy = _xbox_policy("blockUntil", parent_until)

    await coordinator.async_lock_platform(CHILD, "Xbox", _hours(3))
    result = await coordinator.async_unlock_platform(CHILD, "Xbox")

    assert web.writes[-1][:2] == ("xbox", "blockUntil")
    assert parse_utc(web.writes[-1][2]) == parse_utc(iso_utc(parent_until))
    assert result["prior"]["type"] == "blockUntil"
    assert result["applied"]["type"] == "blockUntil"
    assert coordinator.platform_hold(CHILD, "Xbox") is None


async def test_unlock_with_nothing_saved_cancels(coordinator, web):
    result = await coordinator.async_unlock_platform(CHILD, "Xbox")

    assert [(p, k) for p, k, _ in web.writes] == [("xbox", "cancel")]
    assert result == {"prior": None, "applied": {"type": "none", "valid_until": None}}


async def test_unlock_discards_a_prior_lock_that_has_run_out(coordinator, web):
    web.xbox_policy = _xbox_policy("blockUntil", _hours(1))
    await coordinator.async_lock_platform(CHILD, "Xbox", _hours(3))
    # The parent's lock ended while ours was held.
    key = coordinator._hold_key(CHILD, "Xbox")
    coordinator._platform_holds[key]["prior"]["valid_until"] = iso_utc(_hours(-0.5))

    result = await coordinator.async_unlock_platform(CHILD, "Xbox")

    assert web.writes[-1][1] == "cancel"
    assert result["applied"]["type"] == "none"


async def test_extra_time_before_the_lock_cannot_be_written_back(coordinator, web):
    web.xbox_policy = _xbox_policy("allowUntil", _hours(1))
    await coordinator.async_lock_platform(CHILD, "Xbox", _hours(3))

    result = await coordinator.async_unlock_platform(CHILD, "Xbox")

    assert web.writes[-1][1] == "cancel"
    assert result["applied"]["not_restored"]["type"] == "allowUntil"


async def test_403_fails_and_a_retry_keeps_the_saved_prior(coordinator, web):
    web.fail = "HTTP_403"
    web.apply_then_fail = True  # Microsoft stored it anyway
    with pytest.raises(HomeAssistantError):
        await coordinator.async_lock_platform(CHILD, "Xbox", _hours(2))
    hold = coordinator.platform_hold(CHILD, "Xbox")
    assert hold["prior"]["type"] == "none"
    assert hold["confirmed"] is False
    # The read-back now shows our own blockUntil; the retry must not save it.
    assert parse_platform_override(web.xbox_policy, "Xbox")["type"] == "blockUntil"

    web.fail = None
    result = await coordinator.async_lock_platform(CHILD, "Xbox", _hours(2))

    assert result["prior"]["type"] == "none"
    await coordinator.async_unlock_platform(CHILD, "Xbox")
    assert web.writes[-1][1] == "cancel"


async def test_403_without_any_write_reports_the_failure(coordinator, web):
    web.fail = "HTTP_403"
    with pytest.raises(HomeAssistantError, match="HTTP_403"):
        await coordinator.async_lock_platform(CHILD, "Xbox", _hours(2))
    assert web.writes == []


async def test_no_web_session_refuses_the_xbox_lock(coordinator, web):
    web.has_web_cookies = False
    with pytest.raises(HomeAssistantError, match="web session"):
        await coordinator.async_lock_platform(CHILD, "Xbox", _hours(2))
    with pytest.raises(HomeAssistantError, match="web session"):
        await coordinator.async_unlock_platform(CHILD, "Xbox")
    assert web.writes == []


async def test_repeat_lock_keeps_the_prior_and_the_later_end(coordinator, web):
    web.xbox_policy = _xbox_policy("blockUntil", _hours(1))
    first = await coordinator.async_lock_platform(CHILD, "Xbox", _hours(5))
    second = await coordinator.async_lock_platform(CHILD, "Xbox", _hours(2))

    assert second["prior"] == first["prior"]
    assert second["prior"]["type"] == "blockUntil"
    assert second["applied"]["valid_until"] == first["applied"]["valid_until"]


async def test_lock_keeps_a_parent_lock_ending_later(coordinator, web):
    parent_until = _hours(10)
    web.xbox_policy = _xbox_policy("blockUntil", parent_until)

    result = await coordinator.async_lock_platform(CHILD, "Xbox", _hours(2))

    assert parse_utc(result["applied"]["valid_until"]) == parse_utc(iso_utc(parent_until))


async def test_prior_survives_a_restart(hass, entry, coordinator, web, hass_storage):
    web.xbox_policy = _xbox_policy("blockUntil", _hours(1))
    await coordinator.async_lock_platform(CHILD, "Xbox", _hours(3))
    assert f"{PLATFORM_HOLD_STORAGE_KEY}.{entry.entry_id}" in hass_storage

    # A new coordinator after a restart; Microsoft now shows our own lock.
    restarted = _make(hass, entry)
    restarted.web_api = web
    await restarted.async_load_saved_screentime()
    restarted._has_web_credentials = True  # the entry in this test has no cookies
    result = await restarted.async_lock_platform(CHILD, "Xbox", _hours(3))
    assert result["prior"]["type"] == "blockUntil"
    assert parse_utc(result["prior"]["valid_until"]) < _hours(1.1)

    unlocked = await restarted.async_unlock_platform(CHILD, "Xbox")
    assert unlocked["applied"]["type"] == "blockUntil"


async def test_a_hold_whose_lock_ran_out_starts_over(coordinator, web):
    await coordinator.async_lock_platform(CHILD, "Xbox", _hours(2))
    key = coordinator._hold_key(CHILD, "Xbox")
    coordinator._platform_holds[key]["until"] = iso_utc(_hours(-1))
    web.xbox_policy = _xbox_policy("blockUntil", _hours(4))  # a parent lock since

    result = await coordinator.async_lock_platform(CHILD, "Xbox", _hours(1))

    assert result["prior"]["type"] == "blockUntil"


async def test_lock_state_is_read_back_from_the_xbox_policy(coordinator):
    account = coordinator.data["accounts"][CHILD]
    coordinator._apply_xbox_lock_state(CHILD, account, _xbox_policy("blockUntil", _hours(1)))
    assert "Xbox" in account["blocked_platforms"]
    assert account["xbox_lock_known"] is True

    coordinator._apply_xbox_lock_state(CHILD, account, _xbox_policy("cancel"))
    assert "Xbox" not in account["blocked_platforms"]


async def test_windows_lock_and_restore_use_the_web_route(coordinator, web):
    web.windows = {"data": {"screenTimeOverrides": [
        {"screenTimeOverrideType": "blockUntil", "validUntil": iso_utc(_hours(1))}
    ]}}
    result = await coordinator.async_lock_platform(CHILD, "Windows", _hours(3))
    assert result["prior"]["type"] == "blockUntil"

    unlocked = await coordinator.async_unlock_platform(CHILD, "Windows")
    assert web.writes[-1][:2] == ("windows", "blockUntil")
    assert unlocked["applied"]["type"] == "blockUntil"


async def test_mobile_only_xbox_lock_is_refused_with_a_repair(hass, web):
    entry = MockConfigEntry(
        domain=DOMAIN, data={"refresh_token": "x"}, options={CONF_XBOX_FEATURES: False}
    )
    entry.add_to_hass(hass)
    coord = _make(hass, entry)
    coord.web_api = web

    with pytest.raises(ServiceValidationError, match="Xbox features"):
        await coord.async_lock_platform(CHILD, "Xbox", _hours(2))

    assert web.writes == []
    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"{XBOX_LOCK_ISSUE}_{entry.entry_id}")
    assert issue is not None and issue.translation_key == XBOX_LOCK_ISSUE


def test_override_parser_reads_the_three_shapes():
    now = dt_util.utcnow()
    later = iso_utc(now + timedelta(hours=1))
    assert parse_platform_override(_xbox_policy("blockUntil", now + timedelta(hours=1)), "Xbox")["type"] == "blockUntil"
    assert parse_platform_override({"data": {"screenTimeOverride": None}}, "Xbox")["type"] == "none"
    assert parse_platform_override(
        {"lockablePlatforms": [
            {"appliesTo": "Xbox", "overrides": [{"overrideType": "BlockUntil", "validUntil": later}]},
            {"appliesTo": "Windows", "overrides": []},
        ]},
        "Windows",
    )["type"] == "none"
    # Expired, unreadable, unknown.
    assert parse_platform_override(_xbox_policy("blockUntil", now - timedelta(minutes=1)), "Xbox")["type"] == "none"
    assert parse_platform_override({"data": {"screenTimeOverride": {"screenTimeOverrideType": "blockUntil"}}}, "Xbox") is None
    assert parse_platform_override(None, "Xbox") is None
    assert parse_platform_override({"something": 1}, "Xbox") is None


# --- the service: until, duration_hours, response, account_id or entity ----


@pytest.fixture
def service(hass):
    coord = MagicMock()
    coord.async_lock_platform = AsyncMock(
        return_value={"prior": {"type": "none", "valid_until": None},
                      "applied": {"type": "blockUntil", "valid_until": "x"}}
    )
    coord.async_unlock_platform = AsyncMock(return_value={"prior": None, "applied": {}})
    with patch(
        "custom_components.microsoft_family_safety._get_coordinator", return_value=coord
    ):
        _register_services(hass)
        yield coord


async def _call(hass, name="lock_platform", response=False, **data):
    return await hass.services.async_call(
        DOMAIN, name, {"account_id": CHILD, "platform": "Xbox", **data},
        blocking=True, return_response=response,
    )


async def test_service_defaults_to_24_hours(hass, service):
    before = dt_util.now()
    await _call(hass)
    until = service.async_lock_platform.await_args.args[2]
    assert timedelta(hours=23, minutes=59) < until - before < timedelta(hours=24, minutes=1)


async def test_service_takes_until_instead_of_hours(hass, service):
    wanted = dt_util.now().replace(microsecond=0) + timedelta(hours=3, minutes=15)
    await _call(hass, until=wanted.isoformat())
    assert service.async_lock_platform.await_args.args[2] == wanted


async def test_service_until_without_offset_is_local_time(hass, service):
    wanted = (dt_util.now() + timedelta(hours=2)).replace(microsecond=0, tzinfo=None)
    await _call(hass, until=wanted.isoformat())
    until = service.async_lock_platform.await_args.args[2]
    assert until.tzinfo is not None
    assert until.replace(tzinfo=None) == wanted


async def test_service_refuses_both_and_the_past(hass, service):
    with pytest.raises(ServiceValidationError, match="not both"):
        await _call(hass, duration_hours=2, until=(dt_util.now() + timedelta(hours=1)).isoformat())
    with pytest.raises(ServiceValidationError, match="future"):
        await _call(hass, until=(dt_util.now() - timedelta(hours=1)).isoformat())
    service.async_lock_platform.assert_not_awaited()


async def test_service_answers_with_prior_and_applied(hass, service):
    response = await _call(hass, response=True, duration_hours=2)
    assert response["prior"]["type"] == "none"
    assert response["applied"]["type"] == "blockUntil"
    assert await _call(hass, name="unlock_platform", response=True) == {
        "prior": None, "applied": {}
    }
    # Without asking for it, as existing callers do, nothing changes.
    assert await _call(hass, duration_hours=2) is None


# --- unlock only touches a lock that is still ours --------------------------


async def test_unlock_after_our_hold_ran_out_writes_nothing(coordinator, web):
    web.xbox_policy = _xbox_policy("blockUntil", _hours(1))  # a parent lock before ours
    await coordinator.async_lock_platform(CHILD, "Xbox", _hours(3))
    key = coordinator._hold_key(CHILD, "Xbox")
    coordinator._platform_holds[key]["until"] = iso_utc(_hours(-0.1))
    web.writes.clear()

    result = await coordinator.async_unlock_platform(CHILD, "Xbox")

    assert result["applied"] == "hold_expired"
    assert web.writes == []
    assert coordinator.platform_hold(CHILD, "Xbox") is None


@pytest.mark.parametrize(
    "parent",
    [
        lambda: _xbox_policy("blockUntil", _hours(8)),  # a longer lock
        lambda: _xbox_policy("allowUntil", _hours(1)),  # extra time
        lambda: _xbox_policy("cancel"),  # released from the app
    ],
)
async def test_unlock_leaves_a_parent_change_alone(coordinator, web, parent):
    await coordinator.async_lock_platform(CHILD, "Xbox", _hours(3))
    web.xbox_policy = parent()
    web.writes.clear()

    result = await coordinator.async_unlock_platform(CHILD, "Xbox")

    assert result["applied"] == "left_parent_override"
    assert web.writes == []
    assert coordinator.platform_hold(CHILD, "Xbox") is None


async def test_unlock_when_the_state_cannot_be_read_still_unlocks(coordinator, web):
    await coordinator.async_lock_platform(CHILD, "Xbox", _hours(3))
    web.xbox_policy = None  # read failed or backed off
    web.writes.clear()

    result = await coordinator.async_unlock_platform(CHILD, "Xbox")

    assert web.writes[-1][1] == "cancel"
    assert result["applied"]["type"] == "none"
