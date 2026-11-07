"""Per-app usage sensors and event (opt-in), and the slimmer Applications sensor."""

from __future__ import annotations

import json

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_capture_events

from homeassistant.helpers import entity_registry as er

from custom_components.microsoft_family_safety import sensor
from custom_components.microsoft_family_safety.app_usage import (
    EVENT_APP_USAGE,
    AppUsageGrowthTracker,
    top_apps,
    usage_summary,
)
from custom_components.microsoft_family_safety.const import CONF_APP_USAGE_SENSORS, DOMAIN
from custom_components.microsoft_family_safety.coordinator import (
    FamilySafetyDataUpdateCoordinator,
)

CHILD = "1234567890123456"


def _apps(**minutes):
    return [
        {"app_id": f"appx:{name}", "app_name": name.title(), "blocked": name == "game",
         "icon": "https://example.com/" + "x" * 200, "usage_minutes": value}
        for name, value in minutes.items()
    ]


def test_top_apps_and_summary():
    apps = _apps(browser=30, game=50, editor=0.2, music=5)
    assert [a["app_name"] for a in top_apps(apps, 2)] == ["Game", "Browser"]
    summary = usage_summary(apps)
    assert summary["blocked_count"] == 1
    assert summary["blocked_apps"] == ["Game"]
    assert [a["app_name"] for a in summary["top_apps"]] == ["Game", "Browser", "Music"]


def test_summary_stays_small_with_many_apps():
    apps = _apps(**{f"app{i}": i for i in range(300)})
    assert len(json.dumps(usage_summary(apps))) < 2000


def test_growth_tracker_baselines_then_reports():
    tracker = AppUsageGrowthTracker()
    assert tracker.update(CHILD, "2026-11-07", _apps(game=10, browser=5)) == []
    grew = tracker.update(CHILD, "2026-11-07", _apps(game=25, browser=5.2))
    assert grew == [{"app_id": "appx:game", "app_name": "Game", "grew_minutes": 15.0, "today_minutes": 25.0}]
    # A new day starts a new baseline.
    assert tracker.update(CHILD, "2026-11-08", _apps(game=3)) == []


def _coordinator(hass, count):
    entry = MockConfigEntry(
        domain=DOMAIN, data={"refresh_token": "x"}, options={CONF_APP_USAGE_SENSORS: count}
    )
    entry.add_to_hass(hass)
    coord = FamilySafetyDataUpdateCoordinator(hass, entry)
    coord.update_interval = None
    coord.data = {
        "accounts": {CHILD: {
            "first_name": "Alex", "screen_time_date": "2026-11-07",
            "applications": _apps(browser=30, game=50, music=5),
        }},
        "devices": {},
    }
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coord
    return coord, entry


async def _sensors(hass, entry):
    added: list = []
    await sensor.async_setup_entry(hass, entry, added.extend)
    return added


async def test_no_app_sensors_or_events_by_default(hass):
    coord, entry = _coordinator(hass, 0)
    entities = await _sensors(hass, entry)
    assert not [e for e in entities if isinstance(e, sensor.FamilySafetyAppUsageSensor)]

    events = async_capture_events(hass, EVENT_APP_USAGE)
    coord._fire_app_usage_events(coord.data["accounts"])
    await hass.async_block_till_done()
    assert events == []


async def test_most_used_apps_get_a_sensor(hass):
    coord, entry = _coordinator(hass, 2)
    entities = await _sensors(hass, entry)
    apps = [e for e in entities if isinstance(e, sensor.FamilySafetyAppUsageSensor)]
    assert sorted(e._app_name for e in apps) == ["Browser", "Game"]
    game = next(e for e in apps if e._app_name == "Game")
    assert game.native_value == 50
    assert game.unique_id == f"{entry.entry_id}_{CHILD}_app_usage_appx_game"


async def test_an_app_keeps_its_sensor_after_a_restart(hass):
    coord, entry = _coordinator(hass, 1)
    er.async_get(hass).async_get_or_create(
        "sensor", DOMAIN, f"{entry.entry_id}_{CHILD}_app_usage_appx_music",
        config_entry=entry,
    )
    entities = await _sensors(hass, entry)
    apps = sorted(
        e._app_name for e in entities if isinstance(e, sensor.FamilySafetyAppUsageSensor)
    )
    assert apps == ["Game", "Music"]


async def test_usage_event_fires_for_apps_that_grew(hass):
    coord, _ = _coordinator(hass, 3)
    events = async_capture_events(hass, EVENT_APP_USAGE)
    coord._fire_app_usage_events(coord.data["accounts"])  # baseline
    coord.data["accounts"][CHILD]["applications"] = _apps(browser=30, game=62, music=5)
    coord._fire_app_usage_events(coord.data["accounts"])
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data["app_name"] == "Game"
    assert events[0].data["grew_minutes"] == 12.0
    assert events[0].data["account_id"] == CHILD


async def test_applications_sensor_no_longer_carries_the_full_list(hass):
    coord, entry = _coordinator(hass, 0)
    count = sensor.FamilySafetyApplicationCountSensor(coord, entry, CHILD)
    attrs = count.extra_state_attributes
    assert "applications" not in attrs
    assert attrs["blocked_count"] == 1
    assert count.native_value == 3
