"""Extra time (add_time_bonus) and the platform holds of lock_platform.

Extra time this integration gives is recorded as its own: the hold reads
``bonus_ours`` while it is in force. Given over a lock the integration holds,
the hold is kept: strict mode never locks over that extra time, and the lock
is put back once it ends or is cancelled.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from custom_components.microsoft_family_safety.const import CONF_STRICT_MODE, DOMAIN
from custom_components.microsoft_family_safety.screen_time import (
    iso_utc,
    parse_utc,
    parse_xbox_override,
)
from custom_components.microsoft_family_safety.xbox_family_api import (
    OVERRIDE_ALLOW,
    OVERRIDE_BLOCK,
    OVERRIDE_REVOKE,
)

from .test_platform_lock import CHILD, FakeMobile, FakeXbox, _make, _xbox

NONE = {"type": "none", "valid_until": None}
KEY = f"{CHILD}|xbox"


@pytest.fixture
def xbox():
    return FakeXbox()


@pytest.fixture
def mobile():
    return FakeMobile()


def _coordinator(hass, xbox, mobile, strict: bool = False):
    entry = MockConfigEntry(
        domain=DOMAIN, data={"refresh_token": "x"}, options={CONF_STRICT_MODE: strict}
    )
    entry.add_to_hass(hass)
    return _make(hass, entry, xbox, mobile)


def _minutes(minutes: float):
    return dt_util.utcnow() + timedelta(minutes=minutes)


def _polled(coord, xbox) -> None:
    """What the next poll would hold: the Xbox override xapprove reports now."""
    coord.data["accounts"][CHILD]["xbox_override"] = parse_xbox_override(
        {"overrides": list(xbox.overrides or [])}
    )


async def _locked_then_bonus(coord, xbox, minutes=30):
    await coord.async_lock_platform(CHILD, "Xbox", _minutes(180))
    return await coord.async_add_time_bonus(CHILD, "Xbox", _minutes(minutes), replace_lock=True)


async def test_extra_time_over_our_lock_reads_bonus_ours(hass, xbox, mobile):
    coord = _coordinator(hass, xbox, mobile)
    result = await _locked_then_bonus(coord, xbox)

    assert result["added"] is True
    assert result["replaced_lock"] is True
    assert result["hold_kept"] is True
    assert result["applied_until"] == result["until"]
    hold = coord.platform_hold(CHILD, "Xbox")
    assert parse_utc(hold["bonus_until"]) == parse_utc(xbox.overrides[0]["validUntil"])

    _polled(coord, xbox)
    state = coord.platform_hold_state(CHILD, "Xbox")
    assert state["hold"] == "bonus_ours"
    assert state["override"] == "allowUntil"
    assert state["allow_until"] is not None


async def test_extra_time_without_a_lock_reads_bonus_ours(hass, xbox, mobile):
    coord = _coordinator(hass, xbox, mobile)
    result = await coord.async_add_time_bonus(CHILD, "Xbox", _minutes(30))

    assert result["hold_kept"] is False
    assert coord.platform_hold(CHILD, "Xbox") is None
    _polled(coord, xbox)
    assert coord.platform_hold_state(CHILD, "Xbox")["hold"] == "bonus_ours"


async def test_a_parents_extra_time_still_reads_parent(hass, xbox, mobile):
    coord = _coordinator(hass, xbox, mobile)
    await coord.async_add_time_bonus(CHILD, "Xbox", _minutes(30))
    # A parent gave a different amount of time in the app since.
    xbox.overrides = [_xbox(OVERRIDE_ALLOW, _minutes(90))]
    _polled(coord, xbox)
    assert coord.platform_hold_state(CHILD, "Xbox")["hold"] == "parent"


async def test_our_lock_still_needs_replace_lock(hass, xbox, mobile):
    coord = _coordinator(hass, xbox, mobile)
    await coord.async_lock_platform(CHILD, "Xbox", _minutes(180))
    with pytest.raises(HomeAssistantError, match="replace_lock"):
        await coord.async_add_time_bonus(CHILD, "Xbox", _minutes(30))
    assert "bonus_until" not in coord.platform_hold(CHILD, "Xbox")
    assert KEY not in coord._time_bonuses


async def test_extra_time_over_a_parents_lock_keeps_no_hold(hass, xbox, mobile):
    coord = _coordinator(hass, xbox, mobile)
    xbox.overrides = [_xbox(OVERRIDE_BLOCK, _minutes(60))]
    result = await coord.async_add_time_bonus(CHILD, "Xbox", _minutes(30), replace_lock=True)
    assert result["replaced_lock"] is True
    assert result["hold_kept"] is False


async def test_strict_mode_never_locks_over_our_extra_time(hass, xbox, mobile):
    coord = _coordinator(hass, xbox, mobile, strict=True)
    await _locked_then_bonus(coord, xbox)
    hold = coord._platform_holds[KEY]
    hold["confirmed_at"] = iso_utc(dt_util.utcnow() - timedelta(minutes=30))
    xbox.writes.clear()

    # Even a read showing nothing in force (the extra time not seen yet).
    await coord._async_enforce_holds({CHILD: NONE})
    await hass.async_block_till_done()
    assert xbox.writes == []


async def test_lock_is_put_back_when_our_extra_time_has_ended(hass, xbox, mobile):
    coord = _coordinator(hass, xbox, mobile)
    await _locked_then_bonus(coord, xbox)
    hold = coord._platform_holds[KEY]
    until = hold["until"]
    hold["bonus_until"] = iso_utc(_minutes(-1))
    xbox.overrides = []
    xbox.writes.clear()

    # A read taken before the extra time ended decides nothing.
    await coord._async_resume_after_bonus({CHILD: NONE}, {CHILD: _minutes(-5)})
    await hass.async_block_till_done()
    assert xbox.writes == []
    _polled(coord, xbox)
    assert coord.platform_hold_state(CHILD, "Xbox")["hold"] == "ours"

    await coord._async_resume_after_bonus({CHILD: NONE}, {CHILD: dt_util.utcnow()})
    await hass.async_block_till_done()
    assert xbox.writes == [(OVERRIDE_BLOCK, until)]
    assert "bonus_until" not in coord.platform_hold(CHILD, "Xbox")
    assert KEY not in coord._time_bonuses
    _polled(coord, xbox)
    assert coord.platform_hold_state(CHILD, "Xbox")["hold"] == "ours"


async def test_a_parents_override_after_our_extra_time_ends_the_mark(hass, xbox, mobile):
    coord = _coordinator(hass, xbox, mobile)
    await _locked_then_bonus(coord, xbox)
    coord._platform_holds[KEY]["bonus_until"] = iso_utc(_minutes(-1))
    xbox.writes.clear()
    parent = parse_xbox_override({"overrides": [_xbox(OVERRIDE_ALLOW, _minutes(60))]})

    await coord._async_resume_after_bonus({CHILD: parent}, {CHILD: dt_util.utcnow()})
    await hass.async_block_till_done()
    assert xbox.writes == []
    assert "bonus_until" not in coord.platform_hold(CHILD, "Xbox")


async def test_cancelling_our_extra_time_puts_our_lock_back(hass, xbox, mobile):
    coord = _coordinator(hass, xbox, mobile)
    await _locked_then_bonus(coord, xbox)
    until = coord.platform_hold(CHILD, "Xbox")["until"]
    xbox.writes.clear()

    result = await coord.async_cancel_time_bonus(CHILD, "Xbox")

    assert result["cancelled"] is True
    assert result["lock_restored"] is True
    assert result["lock_in_force"] is True
    assert parse_utc(result["lock_until"]) == parse_utc(until)
    assert [kind for kind, _ in xbox.writes] == [OVERRIDE_REVOKE, OVERRIDE_BLOCK]
    assert xbox.writes[-1][1] == until
    assert "bonus_until" not in coord.platform_hold(CHILD, "Xbox")
    assert KEY not in coord._time_bonuses


async def test_cancelling_extra_time_without_a_hold_restores_nothing(hass, xbox, mobile):
    coord = _coordinator(hass, xbox, mobile)
    await coord.async_add_time_bonus(CHILD, "Xbox", _minutes(30))
    xbox.writes.clear()
    result = await coord.async_cancel_time_bonus(CHILD, "Xbox")
    assert result == {"cancelled": True, "lock_in_force": False}
    assert [kind for kind, _ in xbox.writes] == [OVERRIDE_REVOKE]


async def test_unlock_leaves_our_extra_time_running(hass, xbox, mobile):
    coord = _coordinator(hass, xbox, mobile)
    await _locked_then_bonus(coord, xbox)
    xbox.writes.clear()

    result = await coord.async_unlock_platform(CHILD, "Xbox")

    assert result["applied"] == "bonus_kept"
    assert result["current"]["type"] == "allowUntil"
    assert xbox.writes == []
    assert coord.platform_hold(CHILD, "Xbox") is None
    # Nothing left to put back once the extra time ends.
    await coord._async_resume_after_bonus({CHILD: NONE}, {CHILD: _minutes(60)})
    await hass.async_block_till_done()
    assert xbox.writes == []


async def test_unlock_without_a_hold_does_not_revoke_our_extra_time(hass, xbox, mobile):
    coord = _coordinator(hass, xbox, mobile)
    await coord.async_add_time_bonus(CHILD, "Xbox", _minutes(30))
    xbox.writes.clear()
    result = await coord.async_unlock_platform(CHILD, "Xbox")
    assert result["applied"] == "bonus_kept"
    assert xbox.writes == []


async def test_a_new_lock_replaces_our_extra_time(hass, xbox, mobile):
    coord = _coordinator(hass, xbox, mobile)
    await _locked_then_bonus(coord, xbox)
    await coord.async_lock_platform(CHILD, "Xbox", _minutes(120))
    assert "bonus_until" not in coord.platform_hold(CHILD, "Xbox")
    assert KEY not in coord._time_bonuses
    _polled(coord, xbox)
    assert coord.platform_hold_state(CHILD, "Xbox")["hold"] == "ours"


async def test_extra_time_records_survive_a_restart(hass, xbox, mobile):
    coord = _coordinator(hass, xbox, mobile)
    await coord.async_add_time_bonus(CHILD, "Xbox", _minutes(30))
    saved = dict(coord._time_bonuses)

    again = _make(hass, coord.entry, xbox, mobile)
    await again._async_load_platform_holds()
    assert again._time_bonuses == saved
