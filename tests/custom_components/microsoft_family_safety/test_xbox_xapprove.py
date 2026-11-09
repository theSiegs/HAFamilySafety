"""The Xbox through the Xbox Family Settings API (xapprove), with mocked HTTP.

Lock = an override of type 2 (block) until the lock's end; unlock = type 100
(cancel), only for our own block; extra time = type 1 (allow). An allow and a
block never stand together: ours is cancelled first. A parent's override is
never replaced. Every request is signed with the Xbox Family Settings
sign-in's ProofKey. When xapprove cannot be used, a lock falls back to
today's Xbox schedule; the Family web session is never used for the Xbox.
"""

from __future__ import annotations

import json
import re
from unittest.mock import AsyncMock, patch

import pytest

from homeassistant.core import Context
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError, Unauthorized
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from custom_components.microsoft_family_safety import xbox_sisu
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
    FakeResponse,
    CHILD_XUID,
    PATCH_SESSION,
    SECRETS,
    SISU_ACCESS,
    SISU_AUTH,
    SISU_DEVICE_TOKEN,
    TITLE_ID,
    UCS_TOKENS,
    WEB_SIG,
    XSTS_AUTH,
    XSTS_XUI,
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
        "xsts:http://xboxlive.com",
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
        {"name": "xsts_signed", "path": "overrides", "stage": "xapprove", "status": 200, "xerr": None}
    ]
    read = result["overrides_read"]
    assert read["ok"] is True and read["override_active"] is True
    assert read["type"] == "blockUntil" and read["raw_type"] == 2
    assert result["working_method"] == "xsts_signed" and result["method_saved"] is True
    assert server.creates == []  # the probe writes nothing
    assert rig.web_api.web_calls == []
    _no_secrets(result)
    for secret in SECRETS:
        assert secret not in _ours(caplog)


async def test_probe_tries_each_way_and_reports_the_refusal(rig, server):
    server.accepts = lambda h: h.get("Authorization") == f"WLID1.0 t={SISU_ACCESS}"
    result = await rig.async_probe_xapprove(True)

    assert _attempted(result) == ALL_ATTEMPTS[:ALL_ATTEMPTS.index("msa_wlid_signed@overrides") + 1]
    first = result["overrides_attempts"][0]
    assert first["status"] == 401
    assert first["www_authenticate"]["value"] == "Token realm='xboxlive.com', error='token_required'"
    assert result["working_method"] == "msa_wlid_signed"
    assert rig._xapprove_method.name == "msa_wlid_signed"
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
    assert restarted._xapprove_method.name == "msa_wlid_signed"


# The ways of signing xapprove (the explicit XSTS step, the app's headers) -------


UCS = [
    "ucs_raw_prompts_noc", "ucs_t_prompts_noc", "ucs_raw_noprompts_noc", "ucs_t_noprompts_noc",
    "ucs_raw_prompts_c1", "ucs_t_prompts_c1", "ucs_raw_noprompts_c1", "ucs_t_noprompts_c1",
]
UCS_SCOPES = [
    "https://consentservice.microsoft.com/checkin/UnifiedUserConsent.Read",
    "https://consentservice.microsoft.com/web/UnifiedUserConsent.ReadWrite",
]
ALL_NAMES = [
    "xsts_signed", "xsts_unsigned",
    "xsts_no_title_signed", "xsts_sisu_device_signed",
    "webview_xsts_signed", "webview_reauth_xsts_signed",
    "session_xsts_signed", "session_ucs_xsts_signed",
    *[f"{p}_xsts_signed" for p in UCS],
    "ucs_raw_prompts_noc_sisu_signed", "ucs_t_prompts_noc_sisu_signed",
    "sisu_signed", "sisu_unsigned",
    "sisu_c1_signed", "sisu_c1_unsigned",
    "sisu_c2_signed", "sisu_c2_unsigned",
    "sisu_c3_signed", "sisu_c3_unsigned",
    "msa_wlid_signed", "msa_wlid_unsigned",
    "msa_xbl_signed", "msa_xbl_unsigned",
]
#: Every probe attempt, "way@path", in order.
ALL_ATTEMPTS = [
    "xsts_signed@overrides", "xsts_signed@screentime", "xsts_unsigned@overrides",
    "xsts_no_title_signed@overrides", "xsts_sisu_device_signed@overrides",
    "webview_xsts_signed@overrides", "webview_reauth_xsts_signed@overrides",
    "session_xsts_signed@overrides", "session_ucs_xsts_signed@overrides",
    *[f"{p}_xsts_signed@overrides" for p in UCS],
    "ucs_raw_prompts_noc_sisu_signed@overrides", "ucs_t_prompts_noc_sisu_signed@overrides",
    "sisu_signed@overrides", "sisu_signed@screentime", "sisu_unsigned@overrides",
    "sisu_c1_signed@overrides", "sisu_c1_unsigned@overrides",
    "sisu_c2_signed@overrides", "sisu_c2_unsigned@overrides",
    "sisu_c3_signed@overrides", "sisu_c3_unsigned@overrides",
    "msa_wlid_signed@overrides", "msa_wlid_unsigned@overrides",
    "msa_xbl_signed@overrides", "msa_xbl_unsigned@overrides",
]


def _attempted(result) -> list[str]:
    return [f"{a['name']}@{a['path']}" for a in result["overrides_attempts"]]


def _auth_of(name: str) -> str:
    """The Authorization header the fake server expects from a way (the fake
    marks the tokens of a sisu/authorize with the consent tokens)."""
    profile = xapprove.METHODS_BY_NAME[name].sisu_profile
    shape = f"~{profile}" if profile else ""
    if "xsts" in name:
        return f"{XSTS_AUTH}{shape}"
    if "sisu" in name:
        return f"{SISU_AUTH}{shape}"
    if name.startswith("msa_wlid"):
        return f"WLID1.0 t={SISU_ACCESS}"
    return f"XBL3.0 x=4343;{SISU_ACCESS}"


def _accepts_only(name: str):
    method = xapprove.METHODS_BY_NAME[name]
    return lambda h: (
        h.get("Authorization") == _auth_of(name)
        and ("Signature" in h) == method.signed
        and h.get("x-xbl-contract-version") == method.contract
        and h.get("Accept-Language") == method.accept_language
    )


def test_the_probe_attempts_most_app_like_first():
    assert [m.name for m in xapprove.PROBE_METHODS] == ALL_NAMES
    assert [f"{m.name}@{p}" for m, p in xapprove.PROBE_ATTEMPTS] == ALL_ATTEMPTS
    assert len(ALL_ATTEMPTS) == 32
    assert xapprove.DEFAULT_METHOD.name == "xsts_signed"
    for method in xapprove.PROBE_METHODS:
        assert method.app_like
        assert method.signed == method.name.endswith("_signed")


@pytest.mark.parametrize(
    "name", [n for n in ALL_NAMES if not n.startswith(("webview", "session", "xsts_no", "xsts_sisu"))]
)
async def test_the_probe_finds_each_way_and_the_lock_uses_it(rig, server, name):
    server.accepts = _accepts_only(name)
    result = await rig.async_probe_xapprove(True)

    attempts = result["overrides_attempts"]
    assert _attempted(result) == ALL_ATTEMPTS[:ALL_ATTEMPTS.index(f"{name}@overrides") + 1]
    assert all(a["status"] == 401 and "www_authenticate" in a for a in attempts[:-1])
    assert all("response_header_names" in a for a in attempts[:-1])
    assert attempts[-1]["status"] == 200 and "response_body" not in attempts[-1]
    assert result["working_method"] == name and result["method_saved"] is True
    _no_secrets(result)

    server.xapprove_headers.clear()
    locked = await rig.async_lock_platform(CHILD, "Xbox", _hours(3))
    assert locked["lock_path"] == "xapprove" and server.create_types() == [2]
    sent = server.xapprove_headers[-1]
    assert sent["User-Agent"] == "okhttp/4.9.2" and "MS-CV" in sent
    if name.startswith("msa_"):
        assert any(c.endswith(xapprove.XAPPROVE_SCOPE) for c in server.sisu_calls)


async def test_a_settings_read_alone_is_reported_not_saved(rig, server):
    server.accepts = lambda h: False
    server.screentime_accepts = _accepts_only("sisu_signed")
    result = await rig.async_probe_xapprove(True)
    assert _attempted(result) == ALL_ATTEMPTS
    assert result["screentime_read_ok"] == ["sisu_signed"]
    assert result["working_method"] is None and result["method_saved"] is False
    _no_secrets(result)


async def test_a_refusal_is_described_without_secrets(rig, server):
    server.accepts = lambda h: False
    jwt = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJl"
    server.refusal_headers = {
        "x-err": "2148916238", "MS-CV": "abc.1", "Content-Length": "0",
        "WWW-Authenticate": f"XBL3.0 error='invalid_token', hint='{jwt}'",
        "X-Debug-Error": f"user {CHILD_XUID} {SISU_ACCESS}{'A' * 40}",
    }
    server.refusal_text = json.dumps({
        "code": "Unauthorized", "token": jwt, "xuid": CHILD_XUID, "blob": "Q" * 80, "msg": "no",
    })
    result = await rig.async_probe_xapprove(True)

    first = result["overrides_attempts"][0]
    assert first["response_header_names"] == sorted(server.refusal_headers)
    headers = first["response_headers"]
    assert headers["x-err"] == "2148916238" and headers["MS-CV"] == "abc.1"
    assert "Content-Length" not in headers
    assert "<jwt>" in headers["WWW-Authenticate"] and "<number>" in headers["X-Debug-Error"]
    body = first["response_body"]
    assert "Unauthorized" in body and "<jwt>" in body and "<redacted>" in body
    assert len(body) <= 300
    text = json.dumps(result)
    assert jwt not in text and "Q" * 40 not in text and "A" * 40 not in text
    _no_secrets(result)


async def test_xsts_variants_until_one_answers(hass, rig, server):
    # As live: with the optional claims the request is refused (0x8015DC37);
    # without them (the default now) it works at once.
    server.xsts_accepts = lambda headers, props: "OptionalDisplayClaims" not in props
    result = await rig.async_probe_xapprove(True)

    steps = result["steps"]
    worked = steps["xsts_authorize[http://xboxlive.com|no_claims]"]
    assert worked["status"] == 200 and worked["claims_sent"] is False
    assert "xsts_authorize[http://xboxlive.com|app]" not in steps
    assert result["xsts"]["http://xboxlive.com"] == {"variant": "no_claims", "xui_keys": sorted(XSTS_XUI)}
    assert result["working_method"] == "xsts_signed"
    # The variant is saved with the way and tried first after a restart.
    saved = await rig._xapprove_method_store.async_load()
    assert saved["method"] == "xsts_signed" and saved["xsts_variant"] == "no_claims"
    restarted = FamilySafetyDataUpdateCoordinator(hass, rig.entry)
    await restarted._async_load_xapprove_method()
    assert restarted._sisu.preferred_variant == "no_claims"
    _no_secrets(result)


@pytest.mark.parametrize(
    ("variant", "accepts"),
    [
        ("no_title", lambda h, p: "TitleToken" not in p),
        ("app", lambda h, p: "OptionalDisplayClaims" in p),
        ("sisu_device", lambda h, p: p["DeviceToken"] == SISU_DEVICE_TOKEN),
        ("unsigned", lambda h, p: "Signature" not in h),
        ("plain_json", lambda h, p: h["Content-Type"] == "application/json"),
    ],
)
async def test_each_xsts_variant_is_tried(rig, server, variant, accepts):
    server.xsts_accepts = accepts
    result = await rig.async_probe_xapprove(True)
    assert result["xsts"]["http://xboxlive.com"]["variant"] == variant
    assert result["steps"][f"xsts_authorize[http://xboxlive.com|{variant}]"]["status"] == 200


async def test_without_a_sisu_device_token_that_variant_is_skipped(rig, server):
    server.sisu_device_token = False
    server.xsts_accepts = lambda h, p: False
    server.accepts = _accepts_only("sisu_signed")
    result = await rig.async_probe_xapprove(True)
    steps = result["steps"]
    assert steps["xsts_authorize[http://xboxlive.com|sisu_device]"] == {
        "skipped": "no DeviceToken in SISU's answer"
    }
    assert steps["xsts_authorize[http://xboxlive.com|app]"]["status"] == 400
    assert result["xsts"]["http://xboxlive.com"] == {"variant": None, "xui_keys": None}
    # Every variant refused once: the unsigned attempt does not sweep again.
    # (the plain profile, the two web view ones, the two session ones and
    # the eight UCS ones, 5 sent variants each, plus the forced no_title
    # attempt; forced sisu_device is skipped without a device token)
    assert server.sisu_calls.count("xsts:http://xboxlive.com") == 66
    first = result["overrides_attempts"][0]
    assert first["stage"] == "token" and first["xerr"] == 2148916279
    assert result["working_method"] == "sisu_signed"


async def test_without_a_title_token_the_xsts_step_goes_without(rig, server):
    server.title_token = False
    result = await rig.async_probe_xapprove(True)
    assert result["title_token_received"] is False
    assert result["steps"]["xsts_authorize[http://xboxlive.com|no_claims]"]["title_token_sent"] is False
    assert result["working_method"] == "xsts_signed"


async def test_a_401_renews_the_title_bound_xsts(rig, server):
    await rig.async_lock_platform(CHILD, "Xbox", _hours(3))
    calls = list(server.sisu_calls)
    accepted = server.accepts
    answers = iter([False])
    server.accepts = lambda h: next(answers, True) and accepted(h)
    await rig.async_unlock_platform(CHILD, "Xbox")
    # Once refused, SISU's tokens and the XSTS were got again.
    assert server.sisu_calls[len(calls):] == ["authorize", "xsts:http://xboxlive.com"]
    assert server.create_types() == [2, 100]


@pytest.mark.parametrize(
    ("saved", "loaded"),
    [("sisu", "sisu_signed"), ("sisu_xapprove_scope_wlid", "msa_wlid_signed"),
     ("sisu_contract_1", "xsts_signed"), ("msa_xbl_unsigned", "msa_xbl_unsigned")],
)
async def test_a_way_saved_by_an_earlier_version_still_loads(hass, rig, saved, loaded):
    await rig._xapprove_method_store.async_save({"method": saved})
    restarted = FamilySafetyDataUpdateCoordinator(hass, rig.entry)
    await restarted._async_load_xapprove_method()
    assert restarted._xapprove_method.name == loaded
    assert restarted.xbox_signed_in  # the stored sign-in is still read


# The consent service tokens, the title id, sisu/authenticate ---------------------


@pytest.mark.parametrize("profile", UCS)
async def test_each_ucs_authorize_is_tried_and_saved(hass, rig, server, profile):
    name = f"{profile}_xsts_signed"
    server.accepts = _accepts_only(name)
    result = await rig.async_probe_xapprove(True)

    assert result["working_method"] == name and result["method_saved"] is True
    # The consent tokens were fetched with the stored refresh token.
    assert f"token:refresh_token:{UCS_SCOPES[0]}" in server.sisu_calls
    assert f"token:refresh_token:{UCS_SCOPES[1]}" in server.sisu_calls
    step = result["steps"][f"sisu_authorize[{profile}]"]
    assert step["status"] == 200 and step["ucs_sent"] is True
    assert result["xsts"][f"{profile}|http://xboxlive.com"]["variant"] == "no_claims"
    _no_secrets(result)
    # The default from now on, also after a restart, and a lock uses it.
    restarted = FamilySafetyDataUpdateCoordinator(hass, rig.entry)
    await restarted._async_load_xapprove_method()
    assert restarted._xapprove_method.name == name
    locked = await rig.async_lock_platform(CHILD, "Xbox", _hours(3))
    assert locked["lock_path"] == "xapprove"


async def test_the_app_like_ucs_authorize_sends_no_contract(rig, server):
    server.accepts = _accepts_only("ucs_raw_prompts_noc_xsts_signed")
    await rig.async_probe_xapprove(True)
    ucs = [
        (b, c) for b, c in zip(server.authorize_bodies, server.authorize_contracts)
        if "UcsCheckinToken" in b
    ]
    body, contract = ucs[0]
    assert contract is None and body["UserPrompts"] == []
    assert body["UcsCheckinToken"] == UCS_TOKENS[UCS_SCOPES[0]]
    assert body["UcsWebToken"] == UCS_TOKENS[UCS_SCOPES[1]]
    # The plain authorize keeps contract 1 and sends no consent tokens.
    assert server.authorize_contracts[0] == "1"
    assert "UcsCheckinToken" not in server.authorize_bodies[0]


async def test_a_refused_consent_token_is_not_an_expired_sign_in(hass, rig, server):
    real = server.request

    def request(method, url, **kwargs):
        data = kwargs.get("data")
        if isinstance(data, dict) and data.get("scope") in UCS_TOKENS:
            server.sisu_calls.append(f"token:refresh_token:{data['scope']}")
            return FakeResponse(400, {"error": "invalid_grant"})
        return real(method, url, **kwargs)

    server.request = request
    server.accepts = _accepts_only("sisu_signed")
    result = await rig.async_probe_xapprove(True)
    refused = {a["name"]: a for a in result["overrides_attempts"]}[f"{UCS[0]}_xsts_signed"]
    assert refused["stage"] == "token" and refused["error"] == "invalid_grant"
    assert result["working_method"] == "sisu_signed"
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"xbox_signin_expired_{rig.entry.entry_id}") is None
    stored = await rig._xbox_signin_store.async_load()
    assert stored["refresh_token"].startswith("sisu-refresh-")


async def test_another_scopes_refresh_token_is_not_stored(rig, server):
    server.accepts = _accepts_only("msa_wlid_signed")
    result = await rig.async_probe_xapprove(True)
    assert result["working_method"] == "msa_wlid_signed"
    stored = await rig._xbox_signin_store.async_load()
    assert stored["refresh_token"].startswith("sisu-refresh-")


async def test_title_id_and_sisu_authenticate_are_reported(rig, server):
    result = await rig.async_probe_xapprove(True)
    assert result["title_id"] == TITLE_ID
    assert result["steps"]["sisu_authorize"]["title_id"] == TITLE_ID
    assert result["steps"]["sisu_authorize"]["title_xti_keys"] == ["tid"]
    assert result["steps"]["device_authenticate"]["xdi_keys"] == ["dcs", "did"]
    auth = result["sisu_authenticate"]
    assert auth["status"] == 200 and auth["title_id_sent"] is True
    assert auth["msa_oauth_redirect_received"] is True
    assert auth["session_id_received"] is False and auth["session_id_header"] is True
    assert auth["response_keys"] == ["MsaOauthRedirect", "MsaRequestParameters"]
    body = server.authenticate_bodies[0]
    assert body["TitleId"] == TITLE_ID and body["TokenType"] == "code"
    assert body["Offers"] == ["service::user.auth.xboxlive.com::MBI_SSL"]
    assert body["RedirectUri"] == "https://login.live.com/oauth20_desktop.srf"
    query = body["Query"]
    assert query["code_challenge_method"] == "S256" and query["display"] == "android_phone"
    assert query["prompt"] == "select_account" and query["state"] and query["code_challenge"]
    assert body["DeviceToken"] and body["Sandbox"] == "RETAIL"
    _no_secrets(result)


async def test_xapprove_asks_for_gzip(rig, server):
    await rig.async_lock_platform(CHILD, "Xbox", _hours(3))
    assert server.xapprove_headers[-1]["Accept-Encoding"] == "gzip"


# The SISU web view (WebPage) -------------------------------------------------------


def _session_of(headers) -> int | None:
    found = re.search(r"~s(\d+)$", headers.get("Authorization", ""))
    return int(found.group(1)) if found else None


async def test_the_web_view_is_replayed_before_the_xsts(rig, server):
    server.web_page = True
    # As the capture suggests: xapprove takes the tokens of a session whose
    # web view was finished (ReadAccount, then ReadProfile).
    server.accepts = lambda h: _session_of(h) in server.done_sessions and "Signature" in h
    result = await rig.async_probe_xapprove(True)

    assert result["working_method"] == "webview_xsts_signed"
    assert result["proxy_way"] == "page_xbl_signed"
    page = result["steps"]["sisu_authorize"]["web_page"]
    assert page == {
        "host": "sisu.xboxlive.com",
        "path": "/client/v68/00000000482c8f49/view/index.html",
        "view": "view",
        "query_keys": ["MS-CV", "redirect", "sid", "sig", "state"],
        "fragment_keys": [],
        "sid_present": True,
        "sig_present": True,
        "sig_shape": {"length": len(WEB_SIG), "base64_like": True, "dots": 0, "jwt_like": False},
        "redirect_scheme": "ms-xal-00000000482c8f49",
    }
    steps = result["steps"]
    read = steps["sisu_proxy[webview|page_xbl_signed|ReadAccount]"]
    assert read["status"] == 200 and read["response_keys"] == {"Account": ["AgeGroup", "Gamertag"]}
    profile = steps["sisu_proxy[webview|page_xbl_signed|ReadProfile]"]
    assert profile["status"] == 200 and profile["response_keys"] == {"Profile": ["Gamertag"], "Prompts": "list"}
    assert server.proxy_calls == ["ReadAccount", "ReadProfile"]
    fetch = steps["web_page_fetch"]
    assert fetch["status"] == 200 and fetch["scripts"] == ["/client/v68/app.js", "/x.js"]
    assert {"ReadAccount", "ReadProfile", "CreateAccount", "AcceptTerms"} <= set(fetch["further_steps"])
    text = json.dumps(result)
    assert "SamParent" not in text
    _no_secrets(result)

    # The lock uses it too: a new SISU session is replayed again first.
    rig._sisu.forget_tokens()
    locked = await rig.async_lock_platform(CHILD, "Xbox", _hours(3))
    assert locked["lock_path"] == "xapprove"


async def test_the_proxy_ways_are_tried_in_turn(rig, server):
    server.web_page = True
    server.proxy_accepts = lambda h, sid: "Authorization" not in h and "Signature" not in h
    server.accepts = lambda h: _session_of(h) in server.done_sessions and "Signature" in h
    result = await rig.async_probe_xapprove(True)

    assert result["proxy_way"] == "page_none_unsigned"
    steps = result["steps"]
    for name in ("page_xbl_signed", "page_none_signed", "page_sig_signed", "page_xbl_unsigned"):
        assert steps[f"sisu_proxy[webview|{name}|ReadAccount]"]["status"] == 401
    assert result["working_method"] == "webview_xsts_signed"
    _no_secrets(result)


async def test_reauthorizing_after_the_web_view(rig, server):
    server.web_page = True
    server.web_page_after_done = False
    # Only a session made after one was finished is accepted.
    server.accepts = lambda h: _session_of(h) in server.fresh_sessions and "Signature" in h
    result = await rig.async_probe_xapprove(True)

    assert result["working_method"] == "webview_reauth_xsts_signed"
    after = result["steps"]["sisu_authorize[webview_reauth][after]"]
    assert after["status"] == 200 and after["web_page"] is None
    _no_secrets(result)


async def test_no_proxy_way_answers(rig, server):
    server.web_page = True
    server.proxy_accepts = lambda h, sid: False
    server.accepts = lambda h: False
    result = await rig.async_probe_xapprove(True)

    assert result["proxy_way"] == ""
    assert result["steps"]["web_view[webview]"] == {"proxy_way": None, "web_page_sid": True}
    # Not swept again for the second web view way.
    assert result["steps"]["web_view[webview_reauth]"] == {"skipped": "no proxy way answered before"}
    # The six page ways and the two with the authenticate session id.
    assert server.proxy_calls == ["ReadAccount"] * 8
    assert result["working_method"] is None
    _no_secrets(result)


async def test_without_a_web_page_nothing_is_replayed(rig, server):
    server.accepts = lambda h: False
    result = await rig.async_probe_xapprove(True)
    assert result["steps"]["web_view[webview]"] == {"skipped": "no WebPage in SISU's answer"}
    assert server.proxy_calls == []


def test_a_web_page_is_described_without_its_values():
    page = xbox_sisu.parse_web_page(
        "https://sisu.xboxlive.com/client/v68/2535400000000002/consent/index.html"
        "?sid=abc&sig=eyJhbGciOi.x.y#token=1&state=2"
    )
    assert page.sid == "abc" and page.sig == "eyJhbGciOi.x.y"
    facts = page.facts
    assert facts["path"] == "/client/v68/<number>/consent/index.html" and facts["view"] == "consent"
    assert facts["fragment_keys"] == ["state", "token"]
    assert facts["sig_shape"] == {"length": 14, "base64_like": False, "dots": 2, "jwt_like": True}
    assert "abc" not in json.dumps(facts) and "eyJ" not in json.dumps(facts)
    assert xbox_sisu.parse_web_page(None) is None


# A session from sisu/authenticate on a refresh-style authorize -------------------------


async def test_a_session_gives_the_web_page_a_sid(rig, server):
    server.web_page = True
    server.web_page_needs_session = True
    server.accepts = lambda h: (
        "~sess" in h.get("Authorization", "") and _session_of(h) in server.done_sessions
        and "Signature" in h
    )
    result = await rig.async_probe_xapprove(True)

    steps = result["steps"]
    # As live: without a session the WebPage is bare.
    assert steps["sisu_authorize"]["web_page"]["sid_present"] is False
    assert steps["sisu_authorize"]["web_page"]["query_keys"] == []
    assert steps["web_view[webview]"]["web_page_sid"] is False
    session = steps["sisu_authorize[session]"]
    assert session["session_id_sent"] is True and session["web_page"]["sid_present"] is True
    assert steps["sisu_authenticate[session]"]["session_id_header"] is True
    assert steps["web_view[session]"] == {"proxy_way": "page_xbl_signed", "web_page_sid": True}
    assert result["working_method"] == "session_xsts_signed"
    _no_secrets(result)


async def test_a_session_with_the_consent_tokens(rig, server):
    server.web_page = True
    server.web_page_needs_session = True
    server.accepts = lambda h: (
        "~ucs_raw_prompts_noc~sess" in h.get("Authorization", "")
        and _session_of(h) in server.done_sessions and "Signature" in h
    )
    result = await rig.async_probe_xapprove(True)
    assert result["working_method"] == "session_ucs_xsts_signed"
    assert result["steps"]["sisu_authorize[session_ucs]"]["ucs_sent"] is True
    _no_secrets(result)


# Round 6: endpoints, the extra XSTS-variant tokens, the authorize Flow -------------


async def test_the_endpoints_document_is_read(rig, server):
    result = await rig.async_probe_xapprove(True)
    endpoints = result["endpoints"]
    assert endpoints["status"] == 200
    xapprove_entries = endpoints["xapprove"]
    assert len(xapprove_entries) == 1
    entry = xapprove_entries[0]
    assert entry["host"] == "xapprove.xboxlive.com"
    assert entry["relyingParty"] == "http://xapprove.xboxlive.com/"
    assert entry["subRelyingParty"] == "http://xapprove.xboxlive.com/screentime"
    assert entry["tokenType"] == "JWT" and entry["signaturePolicyIndex"] == 2
    assert entry["signaturePolicy"] == {
        "version": 3, "supportedAlgorithms": ["ES256", "RS256"], "maxBodyBytes": 1048576
    }
    # accounts.xboxlive.com is kept for comparison.
    assert any(e["host"] == "accounts.xboxlive.com" for e in endpoints["related"])
    _no_secrets(result)


async def test_the_endpoints_document_failure_is_recorded(rig, server):
    server.endpoints_status = 500
    result = await rig.async_probe_xapprove(True)
    assert result["endpoints"] == {"status": 500}
    # The rest of the probe still runs.
    assert "overrides_attempts" in result


async def test_the_extra_xsts_variant_tokens_are_tried(rig, server):
    # With a device token in SISU's answer, both extra XSTS variants produce
    # a token; xapprove sees each (here it rejects all but the MSA token).
    server.sisu_device_token = True
    server.accepts = lambda h: h.get("Authorization") == f"WLID1.0 t={SISU_ACCESS}"
    result = await rig.async_probe_xapprove(True)
    by_name = {a["name"]: a for a in result["overrides_attempts"]}
    # Both reached xapprove (401), not a token-stage failure.
    assert by_name["xsts_no_title_signed"]["stage"] == "xapprove"
    assert by_name["xsts_sisu_device_signed"]["stage"] == "xapprove"
    assert result["steps"]["xsts_authorize[http://xboxlive.com|no_title]"]["status"] == 200
    assert result["steps"]["xsts_authorize[http://xboxlive.com|sisu_device]"]["status"] == 200
    assert result["working_method"] == "msa_wlid_signed"
    _no_secrets(result)


async def test_the_extra_xsts_variant_is_skipped_without_a_device_token(rig, server):
    # Live: SISU returns no DeviceToken, so sisu_device is skipped cleanly.
    server.sisu_device_token = False
    server.accepts = lambda h: False
    result = await rig.async_probe_xapprove(True)
    by_name = {a["name"]: a for a in result["overrides_attempts"]}
    assert by_name["xsts_sisu_device_signed"]["stage"] == "token"
    _no_secrets(result)


async def test_the_authorize_flow_is_recorded(rig, server):
    server.authorize_flow = "CreateGamertag"
    result = await rig.async_probe_xapprove(True)
    step = result["steps"]["sisu_authorize"]
    assert step["flow"] == "CreateGamertag" and "Flow" in step["response_keys"]
