"""The Xbox through the Xbox Family Settings API (xapprove), with mocked HTTP.

Lock = an override of type 2 (block) until the lock's end; unlock = type 100
(cancel), only for our own block; extra time = type 1 (allow). An allow and a
block never stand together: ours is cancelled first. A parent's override is
never replaced. Every request is signed with the Xbox Family Settings
sign-in's ProofKey. When xapprove cannot be used, a lock falls back to
today's Xbox schedule; the Family web session is never used for the Xbox.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from homeassistant.core import Context
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError, Unauthorized
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from custom_components.microsoft_family_safety import (
    _register_services,
    binary_sensor,
    sensor,
    switch,
    xapprove,
)
from custom_components.microsoft_family_safety.const import CONF_XBOX_XUIDS, DOMAIN
from custom_components.microsoft_family_safety.coordinator import (
    FamilySafetyDataUpdateCoordinator,
)
from custom_components.microsoft_family_safety.screen_time import iso_utc, parse_utc
from custom_components.microsoft_family_safety.sensor import FamilySafetyXboxOverrideSensor
from custom_components.microsoft_family_safety.xbox_xapprove import parse_xuid_option

from .test_platform_lock import CHILD
from .xbox_fakes import (
    CHILD_XUID,
    PATCH_SESSION,
    SECRETS,
    SISU_ACCESS,
    XboxServer,
    hours as _hours,
    make_rig,
    no_secrets as _no_secrets,
    stop_timers,
)


@pytest.fixture(autouse=True)
def _timers():
    yield
    stop_timers()


@pytest.fixture
def server():
    srv = XboxServer()
    with patch(PATCH_SESSION, return_value=srv):
        yield srv


@pytest.fixture
async def rig(hass, server):
    coord, _ = await make_rig(hass, server)
    return coord


def _ours(caplog) -> str:
    return " | ".join(
        r.getMessage() for r in caplog.records if r.name.startswith("custom_components.")
    )


# Lock and unlock ---------------------------------------------------------------


async def test_lock_sends_a_signed_block_until_the_end(rig, server):
    end = _hours(3)
    result = await rig.async_lock_platform(CHILD, "Xbox", end)

    assert result["lock_path"] == "xapprove"
    assert result["applied"]["type"] == "blockUntil"
    assert server.create_types() == [2]
    assert parse_utc(server.creates[0]["validUntil"]) == parse_utc(iso_utc(end))
    assert rig.web_api.web_calls == [] and rig.web_api.day_writes == []
    hold = rig.platform_hold(CHILD, "Xbox")
    assert hold["path"] == "xapprove" and hold["confirmed"] is True
    state = rig.platform_hold_state(CHILD, "Xbox")
    assert state["hold"] == "ours" and state["override"] == "blockUntil"
    assert state["lock_path"] == "xapprove"
    assert "Xbox" in rig.data["accounts"][CHILD]["blocked_platforms"]
    _no_secrets(result)


async def test_a_lock_already_in_force_long_enough_is_not_written_again(rig, server):
    end = _hours(3)
    await rig.async_lock_platform(CHILD, "Xbox", end)
    result = await rig.async_lock_platform(CHILD, "Xbox", _hours(2))
    assert server.create_types() == [2]
    assert parse_utc(result["applied"]["valid_until"]) == parse_utc(iso_utc(end))


async def test_a_longer_lock_cancels_ours_first(rig, server):
    await rig.async_lock_platform(CHILD, "Xbox", _hours(1))
    later = _hours(4)
    await rig.async_lock_platform(CHILD, "Xbox", later)
    assert server.create_types() == [2, 100, 2]
    assert parse_utc(server.override[1]) == parse_utc(iso_utc(later))
    assert rig.platform_hold_state(CHILD, "Xbox")["hold"] == "ours"


@pytest.mark.parametrize("code", [1, 2])
async def test_a_parents_override_is_left_alone(rig, server, code):
    server.override = (code, iso_utc(_hours(1)))
    result = await rig.async_lock_platform(CHILD, "Xbox", _hours(3))

    assert result["applied"] == "left_parent_override"
    assert result["current"]["type"] == ("allowUntil" if code == 1 else "blockUntil")
    assert server.creates == []
    assert rig.platform_hold(CHILD, "Xbox") is None
    assert rig.platform_hold_state(CHILD, "Xbox")["hold"] == "parent"


async def test_unlock_cancels_our_block(rig, server):
    await rig.async_lock_platform(CHILD, "Xbox", _hours(3))
    result = await rig.async_unlock_platform(CHILD, "Xbox")

    assert result["applied"]["type"] == "none"
    assert result["lock_path"] == "xapprove"
    assert server.create_types() == [2, 100]
    assert server.override is None
    assert rig.platform_hold(CHILD, "Xbox") is None
    assert "Xbox" not in rig.data["accounts"][CHILD]["blocked_platforms"]


async def test_unlock_leaves_a_block_that_replaced_ours(rig, server):
    await rig.async_lock_platform(CHILD, "Xbox", _hours(3))
    server.override = (2, iso_utc(_hours(6)))  # a parent paused for longer in the app
    result = await rig.async_unlock_platform(CHILD, "Xbox")

    assert result["applied"] == "left_parent_override"
    assert result["current"]["type"] == "blockUntil"
    assert server.create_types() == [2]
    assert rig.platform_hold_state(CHILD, "Xbox")["hold"] == "parent"


async def test_unlock_after_a_parent_resumed_writes_nothing(rig, server):
    await rig.async_lock_platform(CHILD, "Xbox", _hours(3))
    server.override = None  # "Resume" in the app
    result = await rig.async_unlock_platform(CHILD, "Xbox")
    assert result["applied"] == "left_parent_override"
    assert server.create_types() == [2]


async def test_unlock_without_a_hold_cancels_nothing(rig, server):
    server.override = (2, iso_utc(_hours(2)))
    result = await rig.async_unlock_platform(CHILD, "Xbox")
    assert result["applied"] == "not_locked"
    assert server.creates == []


async def test_the_switch_off_cancels_what_is_in_force(rig, server):
    server.override = (2, iso_utc(_hours(2)))
    await rig.async_release_platform(CHILD, "Xbox")
    assert server.create_types() == [100]


# Falling back to today's schedule ------------------------------------------------


async def test_a_write_that_does_not_read_back_falls_back_to_the_schedule(rig, server):
    server.create_ignored = True
    result = await rig.async_lock_platform(CHILD, "Xbox", _hours(3))
    assert result["lock_path"] == "mobile_schedule"
    assert rig.web_api.day_writes and rig.web_api.web_calls == []


@pytest.mark.parametrize("status", [401, 403])
async def test_a_refused_token_falls_back_and_raises_a_repair(hass, rig, server, status, caplog):
    caplog.set_level("DEBUG")
    server.xapprove_status = status
    result = await rig.async_lock_platform(CHILD, "Xbox", _hours(3))

    assert result["lock_path"] == "mobile_schedule"
    assert rig.web_api.web_calls == []
    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"xapprove_refused_{rig.entry.entry_id}")
    assert issue is not None
    assert not rig._xapprove_usable()
    for secret in SECRETS:
        assert secret not in _ours(caplog)

    # Within the 6 hours, xapprove is not asked again; the schedule lock ends.
    calls = len(server.calls)
    unlocked = await rig.async_unlock_platform(CHILD, "Xbox")
    assert unlocked["lock_path"] == "mobile_schedule"
    assert len(server.calls) == calls


async def test_success_clears_the_repair(hass, rig, server):
    server.xapprove_status = 403
    await rig.async_lock_platform(CHILD, "Xbox", _hours(3))
    server.xapprove_status = 200
    rig._xapprove_refused_until = 0
    await rig._async_poll_xapprove(rig.data["accounts"], {}, {})
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"xapprove_refused_{rig.entry.entry_id}") is None


async def test_no_xuid_falls_back(hass, server):
    server.family_status = 404
    coord, _ = await make_rig(hass, server)
    result = await coord.async_lock_platform(CHILD, "Xbox", _hours(3))
    assert result["lock_path"] == "mobile_schedule"
    assert coord._xapprove_usable()  # not a refusal


async def test_our_xapprove_lock_out_of_reach_is_reported_not_doubled(rig, server):
    end = _hours(3)
    await rig.async_lock_platform(CHILD, "Xbox", end)
    server.xapprove_status = 503
    again = await rig.async_lock_platform(CHILD, "Xbox", _hours(2))
    assert again["lock_path"] == "xapprove"
    assert parse_utc(again["applied"]["valid_until"]) == parse_utc(iso_utc(end))
    assert rig.web_api.day_writes == []
    with pytest.raises(HomeAssistantError, match="cannot be reached"):
        await rig.async_unlock_platform(CHILD, "Xbox")


# Extra time --------------------------------------------------------------------


async def test_extra_time_is_an_allow(rig, server):
    until = _hours(0.5)
    result = await rig.async_add_time_bonus(CHILD, "Xbox", until)

    assert server.create_types() == [1]
    assert parse_utc(server.creates[0]["validUntil"]) == parse_utc(iso_utc(until))
    assert result["read_back"]["type"] == "allowUntil"
    assert result["released_hold"] is False
    assert result["lock_path"] == "xapprove"
    assert rig.platform_hold_state(CHILD, "Xbox")["hold"] == "bonus_ours"


async def test_extra_time_over_our_lock_cancels_it_first(rig, server):
    await rig.async_lock_platform(CHILD, "Xbox", _hours(3))
    result = await rig.async_add_time_bonus(CHILD, "Xbox", _hours(0.5))

    assert server.create_types() == [2, 100, 1]
    assert result["released_hold"] is True
    assert rig.platform_hold(CHILD, "Xbox") is None


async def test_a_lock_over_our_extra_time_cancels_it_first(rig, server):
    await rig.async_add_time_bonus(CHILD, "Xbox", _hours(0.5))
    await rig.async_lock_platform(CHILD, "Xbox", _hours(3))
    assert server.create_types() == [1, 100, 2]
    assert rig._bonuses == {}


async def test_extra_time_over_a_parents_pause_is_refused(rig, server):
    server.override = (2, iso_utc(_hours(2)))
    with pytest.raises(HomeAssistantError, match="parent's Xbox override"):
        await rig.async_add_time_bonus(CHILD, "Xbox", _hours(0.5))
    assert server.creates == []
    assert rig.web_api.web_calls == []


async def test_extra_time_when_xapprove_refuses_fails_clearly(rig, server):
    server.xapprove_status = 401
    with pytest.raises(HomeAssistantError, match="Xbox Family Settings sign-in"):
        await rig.async_add_time_bonus(CHILD, "Xbox", _hours(0.5))
    assert rig.web_api.web_calls == []


async def test_cancel_extra_time(rig, server):
    await rig.async_add_time_bonus(CHILD, "Xbox", _hours(0.5))
    result = await rig.async_cancel_time_bonus(CHILD, "Xbox")
    assert result["cancelled"] is True and result["ours"] is True
    assert server.create_types() == [1, 100]
    assert rig._bonuses == {}


async def test_cancel_never_touches_a_block(rig, server):
    server.override = (2, iso_utc(_hours(2)))
    result = await rig.async_cancel_time_bonus(CHILD, "Xbox")
    assert result["cancelled"] is False
    assert server.creates == []


# Strict mode -------------------------------------------------------------------


async def test_strict_mode_places_our_xbox_lock_again(hass, server):
    coord, _ = await make_rig(hass, server, {"strict_mode": True})
    read_before = dt_util.utcnow()
    await coord.async_lock_platform(CHILD, "Xbox", _hours(2))
    server.override = None  # cancelled elsewhere
    none = {"type": "none", "valid_until": None}

    # A read taken before the lock does not count.
    await coord._async_enforce_holds({CHILD: none}, {CHILD: read_before})
    await hass.async_block_till_done()
    assert server.create_types() == [2]

    await coord._async_enforce_holds({CHILD: none}, {CHILD: dt_util.utcnow()})
    await hass.async_block_till_done()
    assert server.create_types() == [2, 2]
    assert coord.web_api.web_calls == []


# Poll, entities, tokens, child id ----------------------------------------------


async def test_poll_reads_the_override(hass, rig, server):
    server.override = (2, iso_utc(_hours(2)))
    accounts = rig.data["accounts"]
    overrides: dict = {}
    read_at: dict = {}
    await rig._async_poll_xapprove(accounts, overrides, read_at)

    assert accounts[CHILD]["xbox_xapprove"]["type"] == "blockUntil"
    assert overrides[CHILD]["type"] == "blockUntil" and CHILD in read_at
    assert "Xbox" in accounts[CHILD]["blocked_platforms"]
    over = FamilySafetyXboxOverrideSensor(rig, rig.entry, CHILD)
    assert over.native_value == "block"
    attrs = over.extra_state_attributes
    assert attrs["ours"] is False and attrs["hold"] == "parent" and attrs["source"] == "xapprove"
    _no_secrets(accounts)
    _no_secrets(attrs)


async def test_hold_is_on_the_allowed_now_sensor_and_the_lock_switch(hass, rig, server):
    await rig.async_lock_platform(CHILD, "Xbox", _hours(2))
    await rig._async_poll_xapprove(rig.data["accounts"], {}, {})
    allowed = binary_sensor.FamilySafetyAllowedNowSensor(rig, rig.entry, CHILD, "Xbox")
    lock = switch.FamilySafetyPlatformLockSwitch(rig, rig.entry, CHILD, "Alex", "Xbox")
    for attrs in (allowed.extra_state_attributes, lock.extra_state_attributes):
        assert attrs["hold"] == "ours"
        assert attrs["override"] == "blockUntil"
        assert attrs["lock_path"] == "xapprove"
        assert {"override_until", "allow_until"} <= set(attrs)
    assert lock.is_on is True


async def test_tokens_are_reused(rig, server):
    await rig.async_lock_platform(CHILD, "Xbox", _hours(3))
    await rig.async_unlock_platform(CHILD, "Xbox")
    assert server.sisu_calls == [
        "token:refresh_token:service::user.auth.xboxlive.com::MBI_SSL", "device", "authorize",
    ]
    assert len(server.urls("https://accounts.xboxlive.com/")) == 1


async def test_the_entered_xuid_wins_over_the_lookup(hass, server):
    coord, _ = await make_rig(hass, server, {CONF_XBOX_XUIDS: f"alex = {CHILD_XUID}"})
    await coord.async_lock_platform(CHILD, "Xbox", _hours(3))
    assert server.urls("https://accounts.xboxlive.com/") == []
    assert server.create_types() == [2]


async def test_the_first_poll_finds_the_child_before_data_is_set(rig, server):
    accounts = rig.data["accounts"]
    rig.data = None  # the first poll: nothing stored yet
    server.override = (1, iso_utc(_hours(1)))
    await rig._async_poll_xapprove(accounts, {}, {})
    assert accounts[CHILD]["xbox_xapprove"]["type"] == "allowUntil"


def test_parse_xuid_option():
    assert parse_xuid_option("Alex=2533274800000000, 1234567890123456: 2533274800000001\nbad") == {
        "alex": "2533274800000000", "1234567890123456": "2533274800000001",
    }


def test_matching_by_name_id_and_elimination():
    members = [
        {"xuid": "1", "isAdult": True, "firstName": "Sam"},
        {"xuid": "2", "firstName": "Alex", "lastName": "Rivera"},
        {"xuid": "3", "firstName": "Robin", "lastName": "Doe", "msaUserId": "0xNOPE"},
        {"xuid": "4", "puid": "000000000000000A"},
    ]
    children = {
        "10": {"first_name": "Someone", "surname": "Else"},
        "20": {"first_name": "Alex", "surname": "Rivera"},
        "30": {"first_name": "Jamie", "surname": "Lee"},
    }
    found = xapprove.match_children(members, children)
    assert found["10"] == ("4", "family_id")  # hex puid 0xA == 10
    assert found["20"] == ("2", "family_name")
    assert found["30"] == ("3", "elimination")


def test_adults_by_role():
    assert xapprove.is_adult({"role": "Adult"}) is True
    assert xapprove.is_adult({"role": "Admin"}) is True
    assert xapprove.is_adult({"role": "FamilyOrganizer"}) is True
    assert xapprove.is_adult({"role": "Child"}) is False
    assert xapprove.is_adult({"memberRole": "Teen"}) is False
    assert xapprove.is_adult({"isAdult": False, "role": "Adult"}) is False
    assert xapprove.is_adult({}) is False
    members = [
        {"xuid": "1", "role": "Adult", "firstName": "Sam", "lastName": "Rivera"},
        {"xuid": "5", "firstName": "Sam", "lastName": "Rivera"},  # the signed-in parent, no role
        {"xuid": "2", "role": "Child", "firstName": "Alex", "lastName": "Rivera"},
    ]
    children = {
        "20": {"first_name": "Alex", "surname": "Rivera"},
        "30": {"first_name": "Robin", "surname": "Rivera"},
    }
    found = xapprove.match_children(members, children, {"5"})
    assert found == {"20": ("2", "family_name")}  # Robin is not paired with an adult


# Without the Family web session (a mobile-only entry) ----------------------------


async def test_mobile_only_with_the_xbox_sign_in(hass, server):
    coord, entry = await make_rig(hass, server, web_session=False)
    assert coord.xapprove_enabled and not coord.web_session_enabled
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coord
    coord.update_interval = None

    # The poll reads the child's Xbox; the lock switch and the sensor appear.
    await coord._async_poll_xapprove(coord.data["accounts"], {}, {})
    switches: list = []
    await switch.async_setup_entry(hass, entry, switches.extend)
    sensors: list = []
    await sensor.async_setup_entry(hass, entry, sensors.extend)
    locks = [e for e in switches if isinstance(e, switch.FamilySafetyPlatformLockSwitch)]
    assert [e._platform for e in locks if e._platform == "Xbox"] == ["Xbox"]
    assert [e for e in sensors if isinstance(e, FamilySafetyXboxOverrideSensor)]

    locked = await coord.async_lock_platform(CHILD, "Xbox", _hours(2))
    unlocked = await coord.async_unlock_platform(CHILD, "Xbox")
    assert (locked["lock_path"], unlocked["lock_path"]) == ("xapprove", "xapprove")
    assert server.create_types() == [2, 100]
    assert coord.web_api.web_calls == []

    # xapprove out of reach: today's schedule, still no web call.
    server.xapprove_status = 403
    fallback = await coord.async_lock_platform(CHILD, "Xbox", _hours(2))
    assert fallback["lock_path"] == "mobile_schedule"
    assert coord.web_api.day_writes and coord.web_api.web_calls == []


async def test_no_entities_and_no_xapprove_without_the_sign_in(hass, server):
    coord, entry = await make_rig(hass, server, signed_in=False)
    assert not coord.xapprove_enabled
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coord
    coord.update_interval = None
    await coord._async_poll_xapprove(coord.data["accounts"], {}, {})
    switches: list = []
    await switch.async_setup_entry(hass, entry, switches.extend)
    assert not [
        e for e in switches
        if isinstance(e, switch.FamilySafetyPlatformLockSwitch) and e._platform == "Xbox"
    ]
    result = await coord.async_lock_platform(CHILD, "Xbox", _hours(2))
    assert result["lock_path"] == "mobile_schedule"
    assert server.calls == []


# probe_xapprove ----------------------------------------------------------------


async def test_probe_refuses_without_confirm_risk(rig, server):
    with pytest.raises(ServiceValidationError, match="confirm_risk"):
        await rig.async_probe_xapprove(False)
    assert server.calls == []


async def test_probe_answers_without_secrets(rig, server, caplog):
    caplog.set_level("DEBUG")
    server.override = (2, iso_utc(_hours(2)))
    result = await rig.async_probe_xapprove(True)

    assert result["identity"] == "xbox_family_settings" and result["xbox_signed_in"] is True
    assert result["steps"]["sisu_authorize"]["status"] == 200
    family = result["family"]
    assert family["ok"] is True and family["members"] == 2
    assert family["adults"] == 1 and family["roles"] == ["Admin", "Child"]
    assert family["signed_in_parent_listed"] is True
    assert result["children"] == {CHILD: {"resolved": True, "via": "family_name"}}
    assert result["overrides_attempts"] == [
        {"name": "sisu", "stage": "xapprove", "status": 200, "xerr": None}
    ]
    read = result["overrides_read"]
    assert read["ok"] is True and read["override_active"] is True
    assert read["type"] == "blockUntil" and read["raw_type"] == 2
    assert result["working_method"] == "sisu" and result["method_saved"] is True
    assert server.creates == []  # the probe writes nothing
    assert rig.web_api.web_calls == []
    _no_secrets(result)
    for secret in SECRETS:
        assert secret not in _ours(caplog)


async def test_probe_tries_each_way_and_reports_the_refusal(rig, server):
    server.accepts = lambda h: h.get("Authorization") == f"WLID1.0 t={SISU_ACCESS}"
    result = await rig.async_probe_xapprove(True)

    names = [a["name"] for a in result["overrides_attempts"]]
    assert names == ["sisu", "sisu_unsigned", "sisu_contract_1", "sisu_xapprove_scope_wlid"]
    first = result["overrides_attempts"][0]
    assert first["status"] == 401
    assert first["www_authenticate"]["value"] == "Token realm='xboxlive.com', error='token_required'"
    assert result["working_method"] == "sisu_xapprove_scope_wlid"
    assert rig._xapprove_method.name == "sisu_xapprove_scope_wlid"
    _no_secrets(result)


async def test_probe_when_nothing_works(rig, server):
    server.xapprove_status = 401
    result = await rig.async_probe_xapprove(True)
    read = result["overrides_read"]
    assert read["ok"] is False and read["status"] == 401 and read["xerr"] == "2148916238"
    assert result["working_method"] is None and result["method_saved"] is False
    _no_secrets(result)


async def test_probe_without_the_sign_in(hass, server):
    coord, _ = await make_rig(hass, server, signed_in=False)
    result = await coord.async_probe_xapprove(True)
    assert result["xbox_signed_in"] is False and "sign in" in result["error"]
    assert server.calls == []


async def test_probe_service_is_for_administrators(hass, rig, hass_read_only_user, hass_admin_user):
    hass.data.setdefault(DOMAIN, {})[rig.entry.entry_id] = rig
    _register_services(hass)
    rig.async_probe_xapprove = AsyncMock(return_value={"steps": {}})
    with pytest.raises(Unauthorized):
        await hass.services.async_call(
            DOMAIN, "probe_xapprove", {"confirm_risk": True}, blocking=True,
            return_response=True, context=Context(user_id=hass_read_only_user.id),
        )
    response = await hass.services.async_call(
        DOMAIN, "probe_xapprove", {"confirm_risk": True}, blocking=True,
        return_response=True, context=Context(user_id=hass_admin_user.id),
    )
    assert response == {"steps": {}}
    rig.async_probe_xapprove.assert_awaited_once_with(True)


async def test_the_saved_way_survives_a_restart(hass, rig, server):
    server.accepts = lambda h: h.get("Authorization") == f"WLID1.0 t={SISU_ACCESS}"
    await rig.async_probe_xapprove(True)
    restarted = FamilySafetyDataUpdateCoordinator(hass, rig.entry)
    await restarted._async_load_xapprove_method()
    assert restarted._xapprove_method.name == "sisu_xapprove_scope_wlid"
