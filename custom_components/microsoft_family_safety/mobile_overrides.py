"""Device-limits overrides on the Family Safety mobile API.

``/v4/devicelimits/{child}/overrides`` (with ``Plat-Info: ALL``) answers the
child's full override state::

    {"lockablePlatforms": [
        {"appliesTo": "Windows", "overrides": [
            {"overrideType": "BlockUntil", "validUntil": "..."}], "devices": [...]},
        ...]}

A POST to the same route takes ``{target, overrideType, validUntil, culture}``,
where ``target`` is the platform name Microsoft lists in ``appliesTo``. That
name is not fixed: Windows has been listed as "Windows" and as "Desktop", so
the name to send is always taken from a read.

This module only interprets those answers; the requests are made by
``FamilySafetyWebAPI`` (mobile token) and by the pyfamilysafety read patch.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.util import dt as dt_util

MOBILE_OVERRIDES_PATH = "/v4/devicelimits/{child_id}/overrides"
MOBILE_OVERRIDES_PLAT_INFO = "ALL"

MOBILE_OVERRIDE_BLOCK = "BlockUntil"
MOBILE_OVERRIDE_ALLOW = "AllowUntil"
MOBILE_OVERRIDE_CANCEL = "Cancel"

#: appliesTo names (lower case) and the platform name this integration uses.
_PLATFORM_NAMES = {
    "windows": "Windows",
    "desktop": "Windows",
    "xbox": "Xbox",
    "mobile": "Mobile",
}

#: Override types that do not lock the platform.
_NOT_A_LOCK = frozenset({MOBILE_OVERRIDE_ALLOW.lower(), MOBILE_OVERRIDE_CANCEL.lower()})


def mobile_platform_name(applies_to: Any) -> str:
    """The integration's platform name for an ``appliesTo`` value."""
    raw = str(applies_to or "")
    return _PLATFORM_NAMES.get(raw.lower(), raw)


def _platforms(state: Any) -> list[dict]:
    platforms = state.get("lockablePlatforms") if isinstance(state, dict) else None
    return [p for p in platforms if isinstance(p, dict)] if isinstance(platforms, list) else []


def mobile_platform_entry(state: Any, platform: str) -> dict | None:
    """The ``lockablePlatforms`` entry for ``platform`` (e.g. "Windows"), if listed."""
    for entry in _platforms(state):
        if mobile_platform_name(entry.get("appliesTo")).lower() == str(platform).lower():
            return entry
    return None


def _until(value: Any) -> datetime | None:
    parsed = dt_util.parse_datetime(str(value)) if value else None
    if parsed is not None and parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_util.UTC)
    return parsed


def mobile_lock_of(entry: dict | None, now: datetime | None = None) -> dict | None:
    """The lock in force on a platform entry, if any.

    Any override other than extra time (AllowUntil) or a Cancel counts as a
    lock, so an unknown type is never read as "unlocked". An override whose
    validUntil has passed is not in force; one without a readable end is.
    """
    now = now or dt_util.utcnow()
    for override in (entry or {}).get("overrides") or []:
        if not isinstance(override, dict):
            continue
        if str(override.get("overrideType") or "").lower() in _NOT_A_LOCK:
            continue
        until = _until(override.get("validUntil"))
        if until is not None and until <= now:
            continue
        return override
    return None


def mobile_blocked_platforms(state: Any, now: datetime | None = None) -> list[str]:
    """The platforms locked right now, by the integration's platform names."""
    now = now or dt_util.utcnow()
    blocked: list[str] = []
    for entry in _platforms(state):
        name = mobile_platform_name(entry.get("appliesTo"))
        if name and name not in blocked and mobile_lock_of(entry, now) is not None:
            blocked.append(name)
    return blocked
