"""Per-app usage sensors and event (opt-in), and the app list attributes."""

from __future__ import annotations

import json

from pytest_homeassistant_custom_component.common import MockConfigEntry, async_capture_events

from homeassistant.helpers import entity_registry as er

from custom_components.microsoft_family_safety import sensor
from custom_components.microsoft_family_safety.app_usage import (
    EVENT_APP_USAGE,
    AppUsageGrowthTracker,
    app_platform,
    apps_attribute,
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


def test_app_platform_from_the_id():
    assert app_platform("x:1234") == "Xbox"
    assert app_platform("appx:Contoso.Game") == "Windows"
    assert app_platform("win32:game.exe") == "Windows"
    assert app_platform("a:com.example.app") == "Mobile"
    assert app_platform("other") is None


def test_a_windowsplt_app_is_windows_not_xbox():
    """x:windowsplt: ids are Windows apps, though they start with the Xbox x: prefix."""
    assert app_platform("x:windowsplt:61f08e27aff06317") == "Windows"
    assert app_platform("X:WindowsPLT:Example.App_abc") == "Windows"
    assert app_platform("x:1234") == "Xbox"


def test_growth_tracker_baselines_then_reports():
    tracker = AppUsageGrowthTracker()
    assert tracker.update(CHILD, "2026-11-07", _apps(game=10, browser=5)) == []
    grew = tracker.update(CHILD, "2026-11-07", _apps(game=25, browser=5.2))
    assert grew == [{
        "app_id": "appx:game", "app_name": "Game", "platform": "Windows",
        "grew_minutes": 15.0, "today_minutes": 25.0,
    }]
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
    assert coord.app_usage_sensors == 0
    entities = await _sensors(hass, entry)
    assert not [e for e in entities if isinstance(e, sensor.FamilySafetyAppUsageSensor)]


async def test_most_used_apps_get_a_sensor(hass):
    coord, entry = _coordinator(hass, 2)
    entities = await _sensors(hass, entry)
    apps = [e for e in entities if isinstance(e, sensor.FamilySafetyAppUsageSensor)]
    assert sorted(e._app_name for e in apps) == ["Browser", "Game"]
    game = next(e for e in apps if e._app_name == "Game")
    assert game.native_value == 50
    assert game.unique_id == f"{entry.entry_id}_{CHILD}_app_usage_appx_game"
    assert game.extra_state_attributes["platform"] == "Windows"
    # An app missing from today's list reads 0.
    coord.data["accounts"][CHILD]["applications"] = []
    assert game.native_value == 0


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
    data = events[0].data
    assert (data["app_name"], data["grew_minutes"], data["today_minutes"]) == ("Game", 12.0, 62.0)
    assert (data["account_id"], data["first_name"]) == (CHILD, "Alex")


async def test_applications_sensor_keeps_its_list_unrecorded(hass):
    coord, entry = _coordinator(hass, 0)
    count = sensor.FamilySafetyApplicationCountSensor(coord, entry, CHILD)
    attrs = count.extra_state_attributes
    # The example dashboards read applications[].usage_minutes.
    assert attrs["applications"] == coord.data["accounts"][CHILD]["applications"]
    assert attrs["blocked_count"] == 1
    assert attrs["blocked_apps"] == ["Game"]
    assert count.native_value == 3
    # The lists can exceed the recorder's limit: not recorded (the summary is).
    assert {"applications", "apps"} <= count._unrecorded_attributes
    assert "blocked_count" not in count._unrecorded_attributes


async def test_apps_attribute_shape(hass):
    coord, entry = _coordinator(hass, 0)
    coord.data["accounts"][CHILD]["applications"].append(
        {"app_id": "x:1234", "app_name": "Racing", "blocked": False, "usage_minutes": 0}
    )
    screen = sensor.FamilySafetyScreenTimeSensor(coord, entry, CHILD)
    count = sensor.FamilySafetyApplicationCountSensor(coord, entry, CHILD)

    used = screen.extra_state_attributes["apps"]
    assert used[0] == {"package": "appx:game", "name": "Game", "minutes": 50.0, "platform": "Windows"}
    assert [a["package"] for a in used] == ["appx:game", "appx:browser", "appx:music"]
    every = count.extra_state_attributes["apps"]
    assert {"package": "x:1234", "name": "Racing", "minutes": 0.0, "platform": "Xbox"} in every
    assert all("icon" not in a for a in every)
    assert "apps" in screen._unrecorded_attributes
    assert apps_attribute(None) == []
