"""Per-app usage: app lists for attributes, which apps get a sensor, which apps grew.

Microsoft's mobile API gives each app's minutes used today (pyfamilysafety's
``Application.usage``), already polled for the app block switches. Every
installed app would be dozens of sensors per child, so the option
``app_usage_sensors`` sets how many of each child's most used apps get one
(0, the default, turns them and the usage event off). An app keeps its
sensor once it has one, also across restarts.

The usage event (``microsoft_family_safety_app_usage``) reports an app whose
minutes today grew between two polls. The first poll of a day, or after a
restart, only sets the baseline: what grew unseen is not known.
"""
from __future__ import annotations

from typing import Any

from .const import DOMAIN

#: An app used less than this today is not worth a sensor yet.
MIN_RANKED_MINUTES = 1.0
#: Growth below this is rounding between two answers, not use.
MIN_GROWTH_MINUTES = 0.5
EVENT_APP_USAGE = f"{DOMAIN}_app_usage"

#: App id prefix -> platform, first match wins. Microsoft gives each platform
#: its own id for an app: a game is x:<id> on the Xbox and appx:/win32: on
#: Windows. A Windows app can also be listed as x:windowsplt:<id>, so that
#: prefix comes before the plain x: of the Xbox.
_PLATFORM_PREFIXES = (
    ("x:windowsplt:", "Windows"),
    ("x:", "Xbox"),
    ("appx:", "Windows"),
    ("win32:", "Windows"),
    ("a:", "Mobile"),
)


def safe_app_id(app_id: str) -> str:
    """The app id as the unique ids of this integration carry it."""
    return str(app_id).replace(":", "_").replace(" ", "_")


def app_usage_unique_id(entry_id: str, account_id: Any, app_id: str) -> str:
    """Unique id of a child's app usage sensor."""
    return f"{entry_id}_{account_id}_app_usage_{safe_app_id(app_id)}"


def app_platform(app_id: str) -> str | None:
    """The platform an app id belongs to, from its prefix."""
    app_id = str(app_id).lower()
    for prefix, platform in _PLATFORM_PREFIXES:
        if app_id.startswith(prefix):
            return platform
    return None


def top_apps(applications: list[dict[str, Any]] | None, count: int) -> list[dict[str, Any]]:
    """The ``count`` apps used most today, most first, leaving out the barely used."""
    ranked = sorted(
        (
            app for app in applications or []
            if isinstance(app, dict) and (app.get("usage_minutes") or 0) >= MIN_RANKED_MINUTES
        ),
        key=lambda app: app.get("usage_minutes") or 0,
        reverse=True,
    )
    return ranked[: max(count, 0)]


def apps_attribute(
    applications: list[dict[str, Any]] | None, *, used_only: bool = False
) -> list[dict[str, Any]]:
    """A compact app list: ``{package, name, minutes, platform}``, most used first.

    ``package`` is Microsoft's app id, which carries the platform, so an app
    on two platforms is two entries; ``platform`` (Windows, Xbox, Mobile or
    None) says which. ``minutes`` is today's use. No icons, so the list stays
    small.
    """
    apps = [
        {
            "package": app["app_id"],
            "name": app.get("app_name") or app["app_id"],
            "minutes": round(float(app.get("usage_minutes") or 0), 1),
            "platform": app_platform(app["app_id"]),
        }
        for app in applications or []
        if isinstance(app, dict) and app.get("app_id")
    ]
    if used_only:
        apps = [app for app in apps if app["minutes"] > 0]
    return sorted(apps, key=lambda app: (-app["minutes"], app["name"].lower()))


def usage_summary(applications: list[dict[str, Any]] | None) -> dict[str, Any]:
    """A short summary of a child's apps, small enough for the recorder."""
    apps = [app for app in applications or [] if isinstance(app, dict)]
    blocked = [app.get("app_name") or app.get("app_id") for app in apps if app.get("blocked")]
    return {
        "blocked_count": len(blocked),
        "blocked_apps": blocked[:20],
        "top_apps": [
            {"app_name": app.get("app_name"), "usage_minutes": app.get("usage_minutes")}
            for app in top_apps(apps, 5)
        ],
    }


class AppUsageGrowthTracker:
    """Remembers each child's minutes per app to see which apps grew."""

    def __init__(self) -> None:
        self._seen: dict[str, tuple[str, dict[str, float]]] = {}

    def update(
        self, account_id: Any, day: str, applications: list[dict[str, Any]] | None
    ) -> list[dict[str, Any]]:
        """Record this poll; return the apps that grew since the last one, most first."""
        key = str(account_id)
        seen_day, seen = self._seen.get(key, (None, {}))
        current: dict[str, float] = {}
        grew: list[dict[str, Any]] = []
        for app in applications or []:
            if not isinstance(app, dict) or not app.get("app_id"):
                continue
            minutes = float(app.get("usage_minutes") or 0)
            current[app["app_id"]] = minutes
            delta = minutes - seen.get(app["app_id"], 0.0)
            if seen_day == day and delta >= MIN_GROWTH_MINUTES:
                grew.append({
                    "app_id": app["app_id"],
                    "app_name": app.get("app_name") or app["app_id"],
                    "platform": app_platform(app["app_id"]),
                    "grew_minutes": round(delta, 1),
                    "today_minutes": round(minutes, 1),
                })
        self._seen[key] = (day, current)
        return sorted(grew, key=lambda item: item["grew_minutes"], reverse=True)
