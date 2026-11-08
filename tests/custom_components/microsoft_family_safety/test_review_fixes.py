"""Fixes from the code review of the parity branch, one test (or more) each."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_capture_events

from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.microsoft_family_safety import _register_services, switch
from custom_components.microsoft_family_safety.app_pause import AppPauser
from custom_components.microsoft_family_safety.const import (
    CONF_STRICT_MODE,
    DOMAIN,
    EVENT_REQUEST,
)
from custom_components.microsoft_family_safety.coordinator import (
    FamilySafetyDataUpdateCoordinator,
)
from custom_components.microsoft_family_safety.screen_time import (
    parse_allowance_minutes,
    platform_allowance,
)

from .test_platform_lock import CHILD, FakeWebAPI, _hours, _make, _windows


# 1. "1.00:00:00" is a whole day, not 0.
@pytest.mark.parametrize(
    ("value", "minutes"),
    [("1.00:00:00", 1440), ("0.02:30:00", 150), ("02:30:00", 150), ("24:00:00", 1440),
     ("PT1H30M", 90), ("garbage", 0), (None, 0)],
)
def test_allowance_parses_dotnet_days(value, minutes):
    assert parse_allowance_minutes(value) == minutes


def test_a_whole_day_allowance_is_not_a_zero_allowance():
    policy = {"isEnabled": True, "dailyRestrictions": {
        day: {"allowance": "1.00:00:00", "allottedIntervals": [{"begin": "00:00:00", "end": "1.00:00:00"}]}
        for day in ("sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday")
    }}
    now = dt_util.now().replace(hour=12, minute=0)
    status = platform_allowance(policy, 300, now)
    assert status["allowed"] is True
    assert status["allowance_minutes"] == 1440


# 2. stop refuses an empty read.
async def test_stop_refuses_a_read_without_blocked_sites(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={"refresh_token": "x"})
    entry.add_to_hass(hass)
    coord = FamilySafetyDataUpdateCoordinator(hass, entry)
    coord.web_api = SimpleNamespace(
        get_web_browsing_settings=AsyncMock(return_value={"exceptions": []}),
        remove_website=AsyncMock(),
    )
    url = "https://lists.example/x.txt"
    await coord._site_sync_store.async_save({CHILD: {url: ["a.example"]}})

    with pytest.raises(HomeAssistantError, match="incomplete"):
        await coord.async_sync_site_list(CHILD, url, stop=True)

    coord.web_api.remove_website.assert_not_awaited()
    assert (await coord._site_sync_store.async_load())[CHILD][url] == ["a.example"]


# 3. The lock switch's off cancels and forgets the hold.
async def test_lock_switch_off_cancels_without_restoring(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={"refresh_token": "x"})
    entry.add_to_hass(hass)
    coord = _make(hass, entry)
    web = FakeWebAPI()
    coord.web_api = web
    web.windows = _windows("blockUntil", _hours(1))  # a parent lock
    await coord.async_lock_platform(CHILD, "Windows", _hours(3))
    web.writes.clear()

    entity = switch.FamilySafetyPlatformLockSwitch(coord, entry, CHILD, "Alex", "Windows")
    entity.hass = hass
    entity.entity_id = "switch.alex_windows_lock"
    await entity.async_turn_off()

    assert [(p, k) for p, k, _ in web.writes] == [("windows", "cancel")]
    assert coord.platform_hold(CHILD, "Windows") is None
    # A later unlock_platform has nothing stale to put back.
    web.writes.clear()
    result = await coord.async_unlock_platform(CHILD, "Windows")
    assert result["prior"] is None and web.writes[-1][1] == "cancel"


# 5. An empty poll does not make every open request new again.
async def test_request_ids_survive_an_empty_poll(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={"refresh_token": "x"})
    entry.add_to_hass(hass)
    coord = FamilySafetyDataUpdateCoordinator(hass, entry)
    events = async_capture_events(hass, EVENT_REQUEST)
    request = {"id": "r1", "puid": CHILD}

    coord._fire_request_events([request], {})  # baseline
    coord._fire_request_events([], {})  # a failed or empty read
    coord._fire_request_events([request], {})
    await hass.async_block_till_done()
    assert events == []

    # Gone for three polls in a row: forgotten, so it would be news again.
    for _ in range(3):
        coord._fire_request_events([], {})
    coord._fire_request_events([request], {})
    await hass.async_block_till_done()
    assert len(events) == 1


# 6. entity_id may be a list (a target:); one child only.
async def test_entity_list_targets_one_child(hass):
    coord = SimpleNamespace(
        data={"accounts": {CHILD: {}, "999": {}}, "devices": {}},
        async_lock_account=AsyncMock(),
    )
    hass.data.setdefault(DOMAIN, {})["entry"] = coord
    from unittest.mock import patch

    def entity(object_id, identifier):
        cfg = MockConfigEntry(domain=DOMAIN)
        cfg.add_to_hass(hass)
        device = dr.async_get(hass).async_get_or_create(
            config_entry_id=cfg.entry_id, identifiers={(DOMAIN, identifier)}
        )
        return er.async_get(hass).async_get_or_create(
            "switch", DOMAIN, object_id, suggested_object_id=object_id,
            config_entry=cfg, device_id=device.id,
        ).entity_id

    first, second, other = entity("alex_a", CHILD), entity("alex_b", CHILD), entity("sam", "999")
    with patch("custom_components.microsoft_family_safety._get_coordinator", return_value=coord):
        _register_services(hass)
        await hass.services.async_call(
            DOMAIN, "lock_account", {"entity_id": [first, second]}, blocking=True
        )
        coord.async_lock_account.assert_awaited_once_with(CHILD)
        with pytest.raises(ServiceValidationError, match="different"):
            await hass.services.async_call(
                DOMAIN, "lock_account", {"entity_id": [first, other]}, blocking=True
            )


# 7. A refused block drops the saved prior.
async def test_refused_block_leaves_nothing_to_resume(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={"refresh_token": "x"})
    entry.add_to_hass(hass)
    pauser = AppPauser(hass, entry.entry_id)
    app = SimpleNamespace(blocked=False)
    coord = SimpleNamespace(
        get_application=lambda account_id, app_id: app,
        async_block_app=AsyncMock(side_effect=HomeAssistantError("Microsoft rejected blocking")),
        async_unblock_app=AsyncMock(),
    )
    result = await pauser.async_pause(coord, CHILD, ["x:game"])
    assert result["paused"] == [] and "x:game" in result["skipped"]
    assert await pauser.async_saved(CHILD) == {}

    # A network failure may have gone through: that one stays resumable.
    coord.async_block_app.side_effect = asyncio.TimeoutError()
    await pauser.async_pause(coord, CHILD, ["x:game"])
    assert await pauser.async_saved(CHILD) == {"x:game": {"blocked": False}}


# 8. Accounts keyed by int are found from a str id.
async def test_web_filter_toggle_finds_an_int_keyed_account(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={"refresh_token": "x"})
    entry.add_to_hass(hass)
    coord = FamilySafetyDataUpdateCoordinator(hass, entry)
    coord.web_api = SimpleNamespace(toggle_web_filter=AsyncMock())
    coord.async_request_refresh = AsyncMock()
    coord.data = {"accounts": {int(CHILD): {"web_browsing": {"enabled": True}}}, "devices": {}}

    await coord.async_toggle_web_filter(CHILD, False)

    assert coord.web_filter_enabled(CHILD) is False
    assert coord.web_filter_enabled(int(CHILD)) is False
    assert coord.account_data(CHILD) is coord.data["accounts"][int(CHILD)]


# 9. Strict mode reads Windows/Mobile overrides together, each capped.
async def test_strict_mode_reads_in_parallel_with_a_timeout(hass, monkeypatch):
    from custom_components.microsoft_family_safety import coordinator as coordinator_module

    monkeypatch.setattr(coordinator_module, "_STRICT_READ_TIMEOUT", 0.05)
    entry = MockConfigEntry(
        domain=DOMAIN, data={"refresh_token": "x"}, options={CONF_STRICT_MODE: True}
    )
    entry.add_to_hass(hass)
    coord = _make(hass, entry)
    until = (dt_util.utcnow() + timedelta(hours=1)).isoformat()
    coord._platform_holds_loaded = True
    coord._platform_holds = {
        f"{CHILD}|windows": {"account_id": CHILD, "platform": "Windows", "until": until, "confirmed": True},
        f"{CHILD}|mobile": {"account_id": CHILD, "platform": "Mobile", "until": until, "confirmed": True},
    }
    started: list[str] = []

    async def slow_read(account_id, platform):
        started.append(platform)
        await asyncio.sleep(10)

    coord._async_read_platform_override = slow_read
    loop = asyncio.get_running_loop()
    begin = loop.time()
    await coord._async_enforce_holds({})
    assert loop.time() - begin < 1
    assert sorted(started) == ["Mobile", "Windows"]


# 10. One shared mixin module.
def test_shared_entity_helpers_live_in_one_module():
    from custom_components.microsoft_family_safety import binary_sensor, entity_base, sensor

    assert binary_sensor.FamilySafetyAllowedNowSensor.__mro__[1] is entity_base.ChildEntityMixin
    assert sensor.MinuteUpdateMixin is entity_base.MinuteUpdateMixin
    assert not hasattr(binary_sensor, "MinuteUpdateMixin") or (
        binary_sensor.MinuteUpdateMixin is entity_base.MinuteUpdateMixin
    )

