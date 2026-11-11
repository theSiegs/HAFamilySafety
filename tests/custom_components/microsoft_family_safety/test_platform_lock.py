"""lock_platform saves what was in force, unlock_platform puts it back.

The first lock of a platform reads Microsoft's override (a parent's own lock,
extra time, or nothing) and keeps it in a Store with the fact that this
integration holds a lock there. A later lock, after a failed call or a
restart, never takes its own lock for that prior state; unlock restores it
while it has not run out, else cancels, and only undoes a lock that is still
ours.

Xbox is read and written through the Xbox Family Settings service
(xapprove), Windows and Mobile are read from the mobile API. No test here
gives the coordinator a Family web session.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.util import dt as dt_util

from custom_components.microsoft_family_safety import _register_services
from custom_components.microsoft_family_safety.const import DOMAIN
from custom_components.microsoft_family_safety.coordinator import (
    PLATFORM_HOLD_STORAGE_KEY,
    FamilySafetyDataUpdateCoordinator,
)
from custom_components.microsoft_family_safety.screen_time import (
    iso_utc,
    parse_mobile_override,
    parse_utc,
    parse_xbox_override,
)
from custom_components.microsoft_family_safety.switch import (
    FamilySafetyPlatformLockSwitch,
)
from custom_components.microsoft_family_safety.xbox_family_api import (
    OVERRIDE_ALLOW,
    OVERRIDE_BLOCK,
    OVERRIDE_REVOKE,
    XboxFamilyError,
)

CHILD = "1234567890123456"
XUID = "2535400000000001"


def _hours(hours: float) -> datetime:
    return dt_util.utcnow() + timedelta(hours=hours)


def _xbox(kind: int, until: datetime) -> dict:
    return {"overrideType": kind, "validUntil": iso_utc(until)}


class FakeXbox:
    """The Xbox Family Settings service: its overrides and the writes it gets."""

    def __init__(self) -> None:
        self.linked = True
        self.ready = True
        self.usable = True
        self.overrides: list[dict] | None = []
        self.writes: list[tuple[int, str]] = []
        self.fail = False
        #: Microsoft applies the write but the call still fails.
        self.apply_then_fail = False

    async def async_child_xuid(self, first_name, surname):
        return XUID

    async def async_get_overrides(self, xuid):
        if self.overrides is None:
            raise XboxFamilyError("read failed")
        return {"overrides": list(self.overrides)}

    async def async_create_override(self, xuid, override_type, valid_until):
        if self.fail and not self.apply_then_fail:
            raise XboxFamilyError("HTTP 403")
        self.writes.append((override_type, iso_utc(valid_until)))
        self.overrides = (
            [] if override_type == OVERRIDE_REVOKE else [_xbox(override_type, valid_until)]
        )
        if self.fail:
            raise XboxFamilyError("HTTP 403")
        return {"overrides": list(self.overrides)}


class FakeMobile:
    """The mobile API's /v1/devicelimits/{id}/overrides for one child."""

    def __init__(self) -> None:
        self.platforms: dict[str, list[dict]] = {"Windows": [], "Mobile": [], "Xbox": []}
        self.writes: list[tuple[str, str]] = []
        self.reads = 0

    def state(self) -> dict:
        return {
            "lockablePlatforms": [
                {"appliesTo": name, "overrides": list(entries), "devices": []}
                for name, entries in self.platforms.items()
            ]
        }

    async def send_request(self, endpoint, body=None, headers=None, platform=None, **kwargs):
        assert endpoint == "get_override_device_restrictions"
        assert kwargs["USER_ID"] == CHILD
        self.reads += 1
        return {"status": 200, "json": self.state()}

    async def override_device(self, target, override, valid_until=None):
        """pyfamilysafety Account.override_device."""
        name = str(target)
        if str(override) == "Cancel":
            self.platforms[name] = []
            self.writes.append((name, "Cancel"))
        else:
            until = iso_utc(valid_until.replace(tzinfo=dt_util.UTC))
            self.platforms[name] = [{"overrideType": "BlockUntil", "validUntil": until}]
            self.writes.append((name, until))


def _make(hass, entry, xbox: FakeXbox, mobile: FakeMobile) -> FamilySafetyDataUpdateCoordinator:
    coord = FamilySafetyDataUpdateCoordinator(hass, entry)
    coord.xbox_family = xbox
    coord.api = SimpleNamespace(api=mobile)
    coord._accounts[CHILD] = SimpleNamespace(
        user_id=CHILD, first_name="Alex", surname="Rivera", blocked_platforms=[],
        override_device=mobile.override_device,
    )
    coord.async_request_refresh = AsyncMock()
    coord.data = {"accounts": {CHILD: {"blocked_platforms": []}}, "devices": {}}
    return coord


@pytest.fixture
def entry(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={"refresh_token": "x"})
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
def xbox():
    return FakeXbox()


@pytest.fixture
def mobile():
    return FakeMobile()


@pytest.fixture
def coordinator(hass, entry, xbox, mobile):
    return _make(hass, entry, xbox, mobile)


# --- Xbox through xapprove -------------------------------------------------------


async def test_xbox_lock_writes_a_block_and_saves_nothing_in_force(coordinator, xbox):
    result = await coordinator.async_lock_platform(CHILD, "Xbox", _hours(2))

    assert [kind for kind, _ in xbox.writes] == [OVERRIDE_BLOCK]
    assert result["prior"]["type"] == "none"
    assert result["applied"]["type"] == "blockUntil"
    assert parse_utc(result["applied"]["valid_until"]) == parse_utc(xbox.writes[0][1])
    hold = coordinator.platform_hold(CHILD, "Xbox")
    assert hold["confirmed"] is True
    assert "Xbox" in coordinator._merge_blocked_platforms(coordinator._accounts[CHILD])


async def test_unlock_restores_the_parent_lock_in_force_before(coordinator, xbox):
    parent_until = _hours(1)
    xbox.overrides = [_xbox(OVERRIDE_BLOCK, parent_until)]

    await coordinator.async_lock_platform(CHILD, "Xbox", _hours(3))
    result = await coordinator.async_unlock_platform(CHILD, "Xbox")

    assert xbox.writes[-1] == (OVERRIDE_BLOCK, iso_utc(parent_until))
    assert result["prior"]["type"] == "blockUntil"
    assert result["applied"]["type"] == "blockUntil"
    assert coordinator.platform_hold(CHILD, "Xbox") is None


async def test_unlock_with_nothing_saved_cancels(coordinator, xbox):
    result = await coordinator.async_unlock_platform(CHILD, "Xbox")

    assert [kind for kind, _ in xbox.writes] == [OVERRIDE_REVOKE]
    assert result == {"prior": None, "applied": {"type": "none", "valid_until": None}}


async def test_unlock_discards_a_prior_lock_that_has_run_out(coordinator, xbox):
    xbox.overrides = [_xbox(OVERRIDE_BLOCK, _hours(1))]
    await coordinator.async_lock_platform(CHILD, "Xbox", _hours(3))
    # The parent's lock ended while ours was held.
    key = coordinator._hold_key(CHILD, "Xbox")
    coordinator._platform_holds[key]["prior"]["valid_until"] = iso_utc(_hours(-0.5))

    result = await coordinator.async_unlock_platform(CHILD, "Xbox")

    assert xbox.writes[-1][0] == OVERRIDE_REVOKE
    assert result["applied"]["type"] == "none"


async def test_extra_time_before_the_lock_is_not_written_back(coordinator, xbox):
    xbox.overrides = [_xbox(OVERRIDE_ALLOW, _hours(1))]
    await coordinator.async_lock_platform(CHILD, "Xbox", _hours(3))

    result = await coordinator.async_unlock_platform(CHILD, "Xbox")

    assert xbox.writes[-1][0] == OVERRIDE_REVOKE
    assert result["applied"]["not_restored"]["type"] == "allowUntil"


async def test_a_failed_write_keeps_the_saved_prior_for_the_retry(coordinator, xbox):
    xbox.fail = True
    xbox.apply_then_fail = True  # Microsoft stored it anyway
    with pytest.raises(HomeAssistantError):
        await coordinator.async_lock_platform(CHILD, "Xbox", _hours(2))
    hold = coordinator.platform_hold(CHILD, "Xbox")
    assert hold["prior"]["type"] == "none"
    assert hold["confirmed"] is False
    # The read-back now shows our own block; the retry must not save it.
    assert parse_xbox_override({"overrides": xbox.overrides})["type"] == "blockUntil"

    xbox.fail = False
    result = await coordinator.async_lock_platform(CHILD, "Xbox", _hours(2))

    assert result["prior"]["type"] == "none"
    await coordinator.async_unlock_platform(CHILD, "Xbox")
    assert xbox.writes[-1][0] == OVERRIDE_REVOKE


async def test_a_refused_write_reports_the_failure(coordinator, xbox):
    xbox.fail = True
    with pytest.raises(HomeAssistantError):
        await coordinator.async_lock_platform(CHILD, "Xbox", _hours(2))
    assert xbox.writes == []


async def test_repeat_lock_keeps_the_prior_and_the_later_end(coordinator, xbox):
    xbox.overrides = [_xbox(OVERRIDE_BLOCK, _hours(1))]
    first = await coordinator.async_lock_platform(CHILD, "Xbox", _hours(5))
    second = await coordinator.async_lock_platform(CHILD, "Xbox", _hours(2))

    assert second["prior"] == first["prior"]
    assert second["prior"]["type"] == "blockUntil"
    assert second["applied"]["valid_until"] == first["applied"]["valid_until"]


async def test_lock_keeps_a_parent_lock_ending_later(coordinator, xbox):
    parent_until = _hours(10)
    xbox.overrides = [_xbox(OVERRIDE_BLOCK, parent_until)]

    result = await coordinator.async_lock_platform(CHILD, "Xbox", _hours(2))

    assert parse_utc(result["applied"]["valid_until"]) == parse_utc(iso_utc(parent_until))


async def test_prior_survives_a_restart(hass, entry, coordinator, xbox, mobile, hass_storage):
    xbox.overrides = [_xbox(OVERRIDE_BLOCK, _hours(1))]
    await coordinator.async_lock_platform(CHILD, "Xbox", _hours(3))
    assert f"{PLATFORM_HOLD_STORAGE_KEY}.{entry.entry_id}" in hass_storage

    # A new coordinator after a restart; Microsoft now shows our own lock.
    restarted = _make(hass, entry, xbox, mobile)
    await restarted.async_load_saved_screentime()
    result = await restarted.async_lock_platform(CHILD, "Xbox", _hours(3))
    assert result["prior"]["type"] == "blockUntil"
    assert parse_utc(result["prior"]["valid_until"]) < _hours(1.1)

    unlocked = await restarted.async_unlock_platform(CHILD, "Xbox")
    assert unlocked["applied"]["type"] == "blockUntil"


async def test_a_hold_whose_lock_ran_out_starts_over(coordinator, xbox):
    await coordinator.async_lock_platform(CHILD, "Xbox", _hours(2))
    key = coordinator._hold_key(CHILD, "Xbox")
    coordinator._platform_holds[key]["until"] = iso_utc(_hours(-1))
    xbox.overrides = [_xbox(OVERRIDE_BLOCK, _hours(4))]  # a parent lock since

    result = await coordinator.async_lock_platform(CHILD, "Xbox", _hours(1))

    assert result["prior"]["type"] == "blockUntil"


async def test_int_roster_ids_and_str_service_ids_meet(coordinator, xbox):
    # The roster keys accounts by int user_id; services pass a str.
    coordinator._accounts = {int(CHILD): coordinator._accounts.pop(CHILD)}
    await coordinator.async_lock_platform(CHILD, "Xbox", _hours(2))
    assert coordinator.platform_hold(int(CHILD), "Xbox")["confirmed"] is True
    await coordinator.async_unlock_platform(int(CHILD), "Xbox")
    assert coordinator.platform_hold(CHILD, "Xbox") is None


async def test_xbox_without_the_xapprove_link_saves_an_unknown_prior(coordinator, xbox):
    """Only the web session could read the Xbox override then: nothing is read."""
    xbox.linked = False
    web = MagicMock(has_web_cookies=True)
    web.set_platform_time_override = AsyncMock(return_value=True)
    web.get_xbox_screentime_policy = AsyncMock()
    web.get_device_overrides = AsyncMock()
    coordinator.web_api = web
    coordinator._has_web_credentials = True

    result = await coordinator.async_lock_platform(CHILD, "Xbox", _hours(2))
    assert result["prior"]["type"] == "unknown"
    unlocked = await coordinator.async_unlock_platform(CHILD, "Xbox")

    assert unlocked["applied"]["type"] == "none"
    web.get_xbox_screentime_policy.assert_not_called()
    web.get_device_overrides.assert_not_called()
    assert xbox.writes == []


# --- Windows and Mobile: read from the mobile API -------------------------------


async def test_windows_prior_comes_from_the_mobile_api(coordinator, mobile):
    parent_until = _hours(1)
    mobile.platforms["Windows"] = [
        {"overrideType": "BlockUntil", "validUntil": iso_utc(parent_until)}
    ]
    result = await coordinator.async_lock_platform(CHILD, "Windows", _hours(3))
    assert result["prior"]["type"] == "blockUntil"
    assert mobile.writes[-1][0] == "Windows"

    unlocked = await coordinator.async_unlock_platform(CHILD, "Windows")

    assert mobile.writes[-1] == ("Windows", iso_utc(parent_until))
    assert unlocked["applied"]["type"] == "blockUntil"
    assert coordinator.platform_hold(CHILD, "Windows") is None


async def test_reads_never_touch_the_web_session(coordinator, mobile):
    web = MagicMock(has_web_cookies=False)
    coordinator.web_api = web
    await coordinator.async_lock_platform(CHILD, "Mobile", _hours(2))
    await coordinator.async_unlock_platform(CHILD, "Mobile")
    assert mobile.reads == 2
    assert [name for name, _ in mobile.writes] == ["Mobile", "Mobile"]
    web.get_device_overrides.assert_not_called()
    web.get_xbox_screentime_policy.assert_not_called()


def test_override_parsers():
    now = dt_util.utcnow()
    later = iso_utc(now + timedelta(hours=1))
    earlier = iso_utc(now - timedelta(minutes=1))
    mobile = {"lockablePlatforms": [
        {"appliesTo": "Xbox", "overrides": [{"overrideType": "BlockUntil", "validUntil": later}]},
        {"appliesTo": "Windows", "overrides": []},
    ]}
    assert parse_mobile_override(mobile, "Windows")["type"] == "none"
    assert parse_mobile_override(mobile, "Xbox")["type"] == "blockUntil"
    assert parse_mobile_override(
        {"lockablePlatforms": [{"appliesTo": "Windows", "overrides": [{"overrideType": "Mystery"}]}]},
        "Windows",
    ) is None
    assert parse_mobile_override({"something": 1}, "Windows") is None

    assert parse_xbox_override({"overrides": []})["type"] == "none"
    assert parse_xbox_override({})["type"] == "none"
    assert parse_xbox_override({"overrides": [{"overrideType": 2, "validUntil": later}]})["type"] == "blockUntil"
    assert parse_xbox_override({"overrides": [{"overrideType": 1, "validUntil": later}]})["type"] == "allowUntil"
    # A release, an expired lock, an unknown type, no end.
    assert parse_xbox_override({"overrides": [{"overrideType": 100, "validUntil": later}]})["type"] == "none"
    assert parse_xbox_override({"overrides": [{"overrideType": 2, "validUntil": earlier}]})["type"] == "none"
    assert parse_xbox_override({"overrides": [{"overrideType": 7, "validUntil": later}]}) is None
    assert parse_xbox_override({"overrides": [{"overrideType": 2}]}) is None
    assert parse_xbox_override(None) is None


# --- unlock only touches a lock that is still ours ------------------------------


async def test_unlock_after_our_hold_ran_out_writes_nothing(coordinator, xbox):
    xbox.overrides = [_xbox(OVERRIDE_BLOCK, _hours(1))]  # a parent lock before ours
    await coordinator.async_lock_platform(CHILD, "Xbox", _hours(3))
    key = coordinator._hold_key(CHILD, "Xbox")
    coordinator._platform_holds[key]["until"] = iso_utc(_hours(-0.1))
    xbox.writes.clear()

    result = await coordinator.async_unlock_platform(CHILD, "Xbox")

    assert result["applied"] == "hold_expired"
    assert xbox.writes == []
    assert coordinator.platform_hold(CHILD, "Xbox") is None


@pytest.mark.parametrize(
    ("parent", "kind"),
    [
        (lambda: [_xbox(OVERRIDE_BLOCK, _hours(8))], "blockUntil"),  # a longer lock
        (lambda: [_xbox(OVERRIDE_ALLOW, _hours(1))], "allowUntil"),  # extra time
        (lambda: [], "none"),  # released from the app
    ],
)
async def test_unlock_leaves_a_parent_change_alone(coordinator, xbox, parent, kind):
    await coordinator.async_lock_platform(CHILD, "Xbox", _hours(3))
    xbox.overrides = parent()
    xbox.writes.clear()

    result = await coordinator.async_unlock_platform(CHILD, "Xbox")

    assert result["applied"] == "left_parent_override"
    assert result["current"]["type"] == kind
    assert xbox.writes == []
    assert coordinator.platform_hold(CHILD, "Xbox") is None


async def test_unlock_when_the_state_cannot_be_read_still_unlocks(coordinator, xbox):
    await coordinator.async_lock_platform(CHILD, "Xbox", _hours(3))
    xbox.overrides = None  # the read fails
    xbox.writes.clear()

    result = await coordinator.async_unlock_platform(CHILD, "Xbox")

    assert xbox.writes[-1][0] == OVERRIDE_REVOKE
    assert result["applied"]["type"] == "none"


async def test_the_lock_switch_off_cancels_without_restoring(coordinator, entry, xbox):
    xbox.overrides = [_xbox(OVERRIDE_BLOCK, _hours(1))]
    await coordinator.async_lock_platform(CHILD, "Xbox", _hours(3))
    switch = FamilySafetyPlatformLockSwitch(coordinator, entry, CHILD, "Alex", "Xbox")
    switch.hass = coordinator.hass

    await switch.async_turn_off()

    assert xbox.writes[-1][0] == OVERRIDE_REVOKE
    assert coordinator.platform_hold(CHILD, "Xbox") is None


# --- the service: until, duration_hours, response -------------------------------


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
