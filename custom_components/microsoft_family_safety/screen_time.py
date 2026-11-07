"""Pure screen-time helpers: platform overrides as Microsoft reports them.

No Home Assistant state and no I/O here, so every rule can be unit tested.

An override is kept as ``{"type": ..., "valid_until": ...}``:

* ``blockUntil`` -- the platform is locked until ``valid_until`` (UTC ISO).
* ``allowUntil`` -- extra time granted until ``valid_until``.
* ``none`` -- Microsoft answered and nothing is in force (a "cancel", an
  expired override, or a policy without one).
* ``unknown`` -- only used for a saved prior state that could not be read.

``None`` (not a dict) means the answer could not be read; callers treat that
as unknown, never as "not blocked".
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.util import dt as dt_util

BLOCK_UNTIL = "blockUntil"
ALLOW_UNTIL = "allowUntil"
NO_OVERRIDE = "none"
UNKNOWN = "unknown"

_TYPES = {"blockuntil": BLOCK_UNTIL, "allowuntil": ALLOW_UNTIL, "cancel": NO_OVERRIDE}
_TYPE_KEYS = ("screenTimeOverrideType", "overrideType", "type")


def parse_utc(value: Any) -> datetime | None:
    """An ISO date-time as aware UTC; None when missing or unreadable."""
    if isinstance(value, datetime):
        parsed: datetime | None = value
    elif isinstance(value, str) and value:
        parsed = dt_util.parse_datetime(value.replace("Z", "+00:00"))
    else:
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_util.UTC)
    return dt_util.as_utc(parsed)


def iso_utc(value: datetime) -> str:
    """UTC ISO text as Microsoft writes it (milliseconds, trailing Z)."""
    return dt_util.as_utc(value).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def override(kind: str, valid_until: datetime | None = None) -> dict[str, Any]:
    """An override record."""
    return {
        "type": kind,
        "valid_until": iso_utc(valid_until) if valid_until is not None else None,
    }


def _override_entry(entry: Any, now: datetime) -> dict[str, Any] | None:
    """One override object from Microsoft, normalised; None if unreadable."""
    if not isinstance(entry, dict):
        return None
    raw_type = next((entry[k] for k in _TYPE_KEYS if entry.get(k) is not None), None)
    kind = _TYPES.get(str(raw_type or "").lower())
    if kind is None:
        return None
    if kind == NO_OVERRIDE:
        return override(NO_OVERRIDE)
    until = parse_utc(entry.get("validUntil"))
    if until is None:
        return None
    if until <= now:
        return override(NO_OVERRIDE)
    return override(kind, until)


def _for_platform(entry: dict, platform: str) -> bool:
    value = entry.get("platformType") or entry.get("appliesTo") or entry.get("target")
    return value is None or str(value).lower() == platform.lower()


def parse_platform_override(
    payload: Any, platform: str, now: datetime | None = None
) -> dict[str, Any] | None:
    """The override in force for ``platform`` in one of Microsoft's answers.

    Understands the shapes seen in this integration:

    * the Xbox policy, ``{"data": {"screenTimeOverride": {...}}}``;
    * the web override route, ``{"data": {"screenTimeOverrides": [...]}}``;
    * the mobile ``/v1/devicelimits/{id}/overrides`` answer,
      ``{"lockablePlatforms": [{"appliesTo": ..., "overrides": [...]}]}``.
    """
    now = now or dt_util.utcnow()
    if not isinstance(payload, dict):
        return None
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload

    if "screenTimeOverride" in data:
        single = data.get("screenTimeOverride")
        if single is None:
            return override(NO_OVERRIDE)
        return _override_entry(single, now)

    entries: list | None = None
    if isinstance(data.get("screenTimeOverrides"), list):
        entries = [
            e for e in data["screenTimeOverrides"]
            if isinstance(e, dict) and _for_platform(e, platform)
        ]
    elif isinstance(data.get("lockablePlatforms"), list):
        entries = []
        for item in data["lockablePlatforms"]:
            if (
                isinstance(item, dict)
                and str(item.get("appliesTo", "")).lower() == platform.lower()
            ):
                entries.extend(item.get("overrides") or [])
    elif "dailyRestrictions" in data:
        # A policy Microsoft returned without any override in it.
        return override(NO_OVERRIDE)
    if entries is None:
        return None

    found = override(NO_OVERRIDE)
    for entry in entries:
        parsed = _override_entry(entry, now)
        if parsed is None:
            return None
        if parsed["type"] == BLOCK_UNTIL:
            return parsed
        if parsed["type"] == ALLOW_UNTIL:
            found = parsed
    return found


def active_until(record: dict[str, Any] | None, now: datetime | None = None) -> datetime | None:
    """The expiry of a blockUntil/allowUntil record still in the future, else None."""
    if not isinstance(record, dict) or record.get("type") not in (BLOCK_UNTIL, ALLOW_UNTIL):
        return None
    until = parse_utc(record.get("valid_until"))
    if until is None or until <= (now or dt_util.utcnow()):
        return None
    return until
