"""The Xbox lock through a Family Safety *mobile* device-limits override.

Covers the api_client override contract (endpoint, verb, headers, body shape
taken from pyfamilysafety), the non-secret summary and probe, the bounded
self-test (block then always clear), and the lock/unlock/bonus wiring behind
``CONF_XBOX_MOBILE_OVERRIDE`` with the schedule rewrite as the fallback.
"""

from __future__ import annotations

import copy
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockUser,
)

from homeassistant.core import Context
from homeassistant.exceptions import (
    HomeAssistantError,
    ServiceValidationError,
    Unauthorized,
)
from homeassistant.util import dt as dt_util

from custom_components.microsoft_family_safety import _register_services
from custom_components.microsoft_family_safety.api_client import (
    MOBILE_OVERRIDE_ALLOW,
    MOBILE_OVERRIDE_BLOCK,
    MOBILE_OVERRIDE_CANCEL,
    FamilySafetyWebAPI,
    FamilySafetyWebAPIError,
)
from custom_components.microsoft_family_safety.const import (
    CONF_XBOX_LOCK_MOBILE_FALLBACK,
    CONF_XBOX_MOBILE_OVERRIDE,
    DAY_KEYS,
    DOMAIN,
    LOCK_PATH_MOBILE_OVERRIDE,
)
from custom_components.microsoft_family_safety.coordinator import (
    FamilySafetyDataUpdateCoordinator,
)
from custom_components.microsoft_family_safety.screen_time import parse_utc

CHILD = "1234567890123456"
CHILD2 = "2234567890123456"
# A device id that must never leak out of the probe/self-test.
DEVICE_ID = "g:2814111111111111"
TWO_HOURS = 2 * 3600 * 1000
EVENING = [{"begin": "07:00:00", "end": "21:00:00"}]


class FakeOverrideWeb:
    """A mobile client holding one override list per platform, plus a schedule.

    Mirrors the real ``FamilySafetyWebAPI`` override surface (and reuses its
    real parsing/formatting static methods) the way ``xbox_fakes.py`` fakes the
    xapprove server: it checks what the coordinator sends and answers with the
    full state Microsoft would.
    """

    # The real, unchanged helpers, so the summary/format under test is the one
    # that ships.
    summarize_mobile_overrides = staticmethod(FamilySafetyWebAPI.summarize_mobile_overrides)
    mobile_override_valid_until = staticmethod(FamilySafetyWebAPI.mobile_override_valid_until)

    def __init__(self, *, xbox: bool = True) -> None:
        self.has_web_cookies = True
        self.family_context_state = "auth_required"
        self.last_isolated_error_code: str | None = None
        self.last_web_error_code: str | None = None
        self._has_xbox = xbox
        #: child id -> {platform -> list of override dicts}.
        self._by_child: dict[str, dict[str, list[dict]]] = {}
        self.reads = 0
        self.writes: list[tuple[str, str]] = []
        self.fail_read = False
        self.fail_write = False
        self.fail_read_after = 0  # fail the Nth read (1-based); 0 = never

        # Schedule side, so the schedule-rewrite fallback can be exercised too.
        self.days = {
            day: {"allowance": TWO_HOURS, "allottedIntervals": copy.deepcopy(EVENING)}
            for day in range(7)
        }
        self.enabled = True
        self.day_writes: list[tuple[int, int, list]] = []

    def _platforms(self, child_id) -> dict[str, list[dict]]:
        platforms = self._by_child.get(str(child_id))
        if platforms is None:
            platforms = {"Desktop": []}
            if self._has_xbox:
                platforms["Xbox"] = []
            self._by_child[str(child_id)] = platforms
        return platforms

    def _state(self, child_id) -> dict:
        return {
            "lockablePlatforms": [
                {
                    "appliesTo": name,
                    "overrides": [dict(o) for o in overrides],
                    "devices": [{"deviceId": DEVICE_ID}],
                }
                for name, overrides in self._platforms(child_id).items()
            ]
        }

    # Mobile device-limits overrides ---------------------------------------
    async def get_mobile_device_overrides(self, child_id):
        self.reads += 1
        if self.fail_read or (self.fail_read_after and self.reads == self.fail_read_after):
            raise FamilySafetyWebAPIError("Mobile overrides read failed")
        return self._state(child_id)

    async def set_mobile_device_override(self, child_id, target, override_type, valid_until):
        self.writes.append((target, override_type))
        if self.fail_write:
            raise FamilySafetyWebAPIError("Mobile override write failed")
        overrides = self._platforms(child_id).setdefault(target, [])
        overrides.clear()
        if override_type != MOBILE_OVERRIDE_CANCEL:
            overrides.append({
                "overrideType": override_type,
                "validUntil": self.mobile_override_valid_until(valid_until),
            })
        return self._state(child_id)

    def xbox_overrides(self, child_id=CHILD) -> list[dict]:
        return self._platforms(child_id).get("Xbox", [])

    def block_xbox(self, child_id=CHILD) -> None:
        self._platforms(child_id)["Xbox"] = [{
            "overrideType": MOBILE_OVERRIDE_BLOCK,
            "validUntil": self.mobile_override_valid_until(
                dt_util.utcnow() + timedelta(hours=1)
            ),
        }]

    # Schedule side (for the fallback) -------------------------------------
    async def get_mobile_xbox_day(self, child_id, day_of_week):
        if not self._has_xbox:
            raise FamilySafetyWebAPIError("A separate, unambiguous Xbox schedule is required")
        return {**copy.deepcopy(self.days[day_of_week]), "enabled": self.enabled}

    async def set_mobile_xbox_day(self, child_id, day_of_week, allowance_ms, intervals):
        self.days[day_of_week] = {
            "allowance": allowance_ms, "allottedIntervals": copy.deepcopy(intervals),
        }
        self.day_writes.append((day_of_week, allowance_ms, copy.deepcopy(intervals)))

    async def set_mobile_schedule_enabled(self, child_id, enabled, platform="Windows"):
        self.enabled = enabled

    def policy(self) -> dict:
        return FamilySafetyWebAPI._convert_mobile_schedule({
            "enabled": self.enabled,
            "appliesTo": "Xbox",
            "dailyRestrictions": {DAY_KEYS[d]: v for d, v in self.days.items()},
        })


_MADE: list[FamilySafetyDataUpdateCoordinator] = []


@pytest.fixture(autouse=True)
def _stop_timers():
    yield
    while _MADE:
        _MADE.pop()._cancel_all_mobile_restores()


def _make(hass, entry, web) -> FamilySafetyDataUpdateCoordinator:
    coord = FamilySafetyDataUpdateCoordinator(hass, entry)
    _MADE.append(coord)
    coord._has_web_credentials = True
    coord._accounts[CHILD] = MagicMock(user_id=CHILD)
    coord._accounts[CHILD2] = MagicMock(user_id=CHILD2)
    coord.async_request_refresh = AsyncMock()
    coord.web_api = web
    coord.data = {
        "accounts": {
            CHILD: {"blocked_platforms": [], "first_name": "Alex",
                    "xbox_screentime_policy": web.policy()},
            CHILD2: {"blocked_platforms": [], "first_name": "Sam",
                     "xbox_screentime_policy": web.policy()},
        },
        "devices": {},
    }
    return coord


@pytest.fixture
def entry(hass):
    entry = MockConfigEntry(
        domain=DOMAIN, data={"refresh_token": "x"},
        options={CONF_XBOX_MOBILE_OVERRIDE: True},
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
def web():
    return FakeOverrideWeb()


@pytest.fixture
def coord(hass, entry, web):
    return _make(hass, entry, web)


def _in(hours: float):
    return dt_util.utcnow() + timedelta(hours=hours)


# api_client contract ---------------------------------------------------------


async def test_override_methods_hit_the_v4_endpoint_with_the_right_body():
    api = FamilySafetyWebAPI(authenticator=SimpleNamespace(access_token="t"))
    sent: dict = {}

    async def fake_request(method, path, json_data=None, params=None, extra_headers=None):
        sent.update(method=method, path=path, json=json_data, params=params,
                    headers=extra_headers)
        return {"lockablePlatforms": []}

    with patch.object(api, "_request", side_effect=fake_request):
        await api.get_mobile_device_overrides(CHILD)
        assert sent["method"] == "GET"
        assert sent["path"] == f"/v4/devicelimits/{CHILD}/overrides"
        assert sent["headers"] == {"Plat-Info": "ALL"}

        until = dt_util.utcnow() + timedelta(minutes=2)
        await api.set_mobile_device_override(CHILD, "Xbox", MOBILE_OVERRIDE_BLOCK, until)
        assert sent["method"] == "POST"
        assert sent["path"] == f"/v4/devicelimits/{CHILD}/overrides"
        assert sent["headers"] == {"Plat-Info": "ALL"}
        assert sent["json"]["target"] == "Xbox"
        assert sent["json"]["overrideType"] == "BlockUntil"
        assert sent["json"]["culture"] == "en-us"
        # validUntil is the pyfamilysafety shape: ...000+0000, in real UTC.
        assert sent["json"]["validUntil"].endswith("+0000")
        assert ".000+0000" in sent["json"]["validUntil"]


def test_summary_is_non_secret_and_reads_block_state():
    payload = {
        "lockablePlatforms": [
            {"appliesTo": "Xbox",
             "overrides": [{"overrideType": "BlockUntil",
                            "validUntil": "2099-01-01T00:00:00.000+0000"}],
             "devices": [{"deviceId": DEVICE_ID}]},
            {"appliesTo": "Desktop", "overrides": [], "devices": []},
        ]
    }
    summary = FamilySafetyWebAPI.summarize_mobile_overrides(payload)
    assert summary["lockable_platform_names"] == ["Desktop", "Xbox"]
    assert summary["platforms"]["Xbox"]["blocked"] is True
    assert summary["platforms"]["Xbox"]["override_types"] == ["BlockUntil"]
    assert summary["platforms"]["Xbox"]["device_count"] == 1
    assert summary["platforms"]["Desktop"]["blocked"] is False
    # No device id anywhere in the summary.
    assert DEVICE_ID not in json.dumps(summary)


# Lock / unlock wiring --------------------------------------------------------


async def test_lock_uses_the_mobile_override(coord, web, hass_storage):
    result = await coord.async_lock_platform(CHILD, "Xbox", _in(2))

    assert result["lock_path"] == LOCK_PATH_MOBILE_OVERRIDE
    assert result["applied"]["type"] == "blockUntil"
    assert web.writes == [("Xbox", MOBILE_OVERRIDE_BLOCK)]
    assert web.day_writes == []  # schedule not touched
    hold = coord.platform_hold(CHILD, "Xbox")
    assert hold["path"] == LOCK_PATH_MOBILE_OVERRIDE
    assert hold["confirmed"] is True
    assert ("1234567890123456", "xbox") in coord._platform_override_until


async def test_unlock_cancels_the_override(coord, web, hass_storage):
    await coord.async_lock_platform(CHILD, "Xbox", _in(2))
    web.writes.clear()

    result = await coord.async_unlock_platform(CHILD, "Xbox")

    assert result["lock_path"] == LOCK_PATH_MOBILE_OVERRIDE
    assert ("Xbox", MOBILE_OVERRIDE_CANCEL) in web.writes
    assert coord.platform_hold(CHILD, "Xbox") is None
    assert ("1234567890123456", "xbox") not in coord._platform_override_until


async def test_relock_extends_not_shortens(coord, web, hass_storage):
    await coord.async_lock_platform(CHILD, "Xbox", _in(3))
    far = coord.platform_hold(CHILD, "Xbox")["until"]
    await coord.async_lock_platform(CHILD, "Xbox", _in(1))  # shorter
    assert coord.platform_hold(CHILD, "Xbox")["until"] == far


async def test_a_block_not_ours_is_not_overridden(coord, web, hass_storage):
    web.block_xbox()  # a parent's block, no hold of ours
    with pytest.raises(HomeAssistantError):
        # override refuses -> no schedule fallback configured, web fake has no
        # set_platform_time_override, so the whole lock fails rather than
        # stepping on the parent's block.
        await coord.async_lock_platform(CHILD, "Xbox", _in(2))
    assert web.writes == []  # nothing written


async def test_override_write_refused_falls_back_to_the_schedule(hass, hass_storage):
    entry = MockConfigEntry(
        domain=DOMAIN, data={"refresh_token": "x"},
        options={CONF_XBOX_MOBILE_OVERRIDE: True, CONF_XBOX_LOCK_MOBILE_FALLBACK: True},
    )
    entry.add_to_hass(hass)
    web = FakeOverrideWeb()
    web.fail_write = True  # override POST refused
    coord = _make(hass, entry, web)

    result = await coord.async_lock_platform(CHILD, "Xbox", _in(2))

    # The override was attempted, then the schedule rewrite took over.
    assert ("Xbox", MOBILE_OVERRIDE_BLOCK) in web.writes
    assert result["lock_path"] == "mobile_schedule"
    assert web.day_writes and web.day_writes[0][1] == 0


# Probe (read only) -----------------------------------------------------------


async def test_probe_reads_each_child_without_secrets(coord, web):
    web.block_xbox()  # Alex's Xbox is blocked
    result = await coord.async_probe_xbox_mobile_overrides(True)

    assert result["endpoint"] == "/v4/devicelimits/{child}/overrides"
    assert result["override_option_enabled"] is True
    by_id = {c["account_id"]: c for c in result["children"]}
    assert by_id[CHILD]["xbox_present"] is True
    assert by_id[CHILD]["xbox"]["blocked"] is True
    assert by_id[CHILD]["xbox_schedule_present"] is True
    assert web.writes == []  # nothing written
    assert DEVICE_ID not in json.dumps(result)


async def test_probe_needs_confirm_risk(coord):
    with pytest.raises(ServiceValidationError, match="confirm_risk"):
        await coord.async_probe_xbox_mobile_overrides(False)


# Bounded self-test -----------------------------------------------------------


async def test_self_test_blocks_then_clears(coord, web, hass_storage):
    result = await coord.async_test_xbox_mobile_override(True, "Alex")

    assert web.writes == [("Xbox", MOBILE_OVERRIDE_BLOCK), ("Xbox", MOBILE_OVERRIDE_CANCEL)]
    assert result["block_confirmed"] is True
    assert result["left_blocked"] is False
    steps = [s["step"] for s in result["trace"]]
    assert steps == ["read_before", "block", "read_after_block", "clear"]
    # Ends clear: the Xbox is not blocked afterwards.
    assert web.xbox_overrides() == []
    assert DEVICE_ID not in json.dumps(result)


async def test_self_test_aborts_on_an_existing_hold(coord, web):
    web.block_xbox()
    result = await coord.async_test_xbox_mobile_override(True, "Alex")
    assert result["aborted"] is True
    assert web.writes == []  # never touched the real hold


async def test_self_test_always_clears_even_when_readback_fails(coord, web, hass_storage):
    # Reads: 1 before, 2 after block (make this one fail), then clear's read.
    web.fail_read_after = 2
    result = await coord.async_test_xbox_mobile_override(True, "Alex")
    # The block was still cancelled despite the failed read-back.
    assert web.writes == [("Xbox", MOBILE_OVERRIDE_BLOCK), ("Xbox", MOBILE_OVERRIDE_CANCEL)]
    assert web.xbox_overrides() == []
    assert result["left_blocked"] is False


async def test_self_test_dry_reports_safe_children(coord, web):
    web.block_xbox()  # Alex blocked; Sam free
    result = await coord.async_test_xbox_mobile_override(True, dry=True)
    assert result["dry"] is True
    by_id = {c["account_id"]: c for c in result["candidates"]}
    assert by_id[CHILD]["safe_to_test"] is False
    assert by_id[CHILD2]["safe_to_test"] is True
    assert web.writes == []


async def test_self_test_needs_confirm_risk(coord):
    with pytest.raises(ServiceValidationError, match="confirm_risk"):
        await coord.async_test_xbox_mobile_override(False, "Alex")


async def test_self_test_needs_a_child_when_not_dry(coord):
    with pytest.raises(ServiceValidationError, match="child"):
        await coord.async_test_xbox_mobile_override(True)


# Services --------------------------------------------------------------------


async def test_probe_and_test_services_are_admin_only(
    hass, entry, web, hass_admin_user, hass_read_only_user
):
    coord = _make(hass, entry, web)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coord
    _register_services(hass)
    coord.async_probe_xbox_mobile_overrides = AsyncMock(return_value={"children": []})
    coord.async_test_xbox_mobile_override = AsyncMock(return_value={"dry": True})

    for service, data, method in (
        ("probe_xbox_mobile_overrides", {"confirm_risk": True},
         coord.async_probe_xbox_mobile_overrides),
        ("test_xbox_mobile_override", {"confirm_risk": True, "dry": True},
         coord.async_test_xbox_mobile_override),
    ):
        with pytest.raises(Unauthorized):
            await hass.services.async_call(
                DOMAIN, service, data, blocking=True, return_response=True,
                context=Context(user_id=hass_read_only_user.id),
            )
        response = await hass.services.async_call(
            DOMAIN, service, data, blocking=True, return_response=True,
            context=Context(user_id=hass_admin_user.id),
        )
        assert isinstance(response, dict)
        method.assert_awaited()
