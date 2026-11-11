"""Pure helpers for platform overrides (locks and extra time) as Microsoft reports them.

No Home Assistant state and no I/O here, so every rule can be unit tested.

An override is kept as ``{"type": ..., "valid_until": ...}``:

* ``blockUntil`` -- the platform is locked until ``valid_until`` (UTC ISO).
* ``allowUntil`` -- extra time granted until ``valid_until``.
* ``none`` -- Microsoft answered and nothing is in force (a cancel, an
  expired override, or no override at all).
* ``unknown`` -- only used for a saved prior state that could not be read.

``None`` (not a dict) means the answer could not be read; callers treat that
as unknown, never as "not blocked".
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.util import dt as dt_util

from .xbox_family_api import OVERRIDE_ALLOW, OVERRIDE_BLOCK, OVERRIDE_REVOKE

BLOCK_UNTIL = "blockUntil"
ALLOW_UNTIL = "allowUntil"
NO_OVERRIDE = "none"
UNKNOWN = "unknown"

#: Override types of the mobile API (pyfamilysafety writes "BlockUntil" and
#: "Cancel"), lower-cased.
_MOBILE_TYPES = {"blockuntil": BLOCK_UNTIL, "allowuntil": ALLOW_UNTIL, "cancel": NO_OVERRIDE}
_MOBILE_TYPE_KEYS = ("overrideType", "screenTimeOverrideType", "type")
#: Override types of the Xbox Family Settings service (xapprove).
_XBOX_TYPES = {OVERRIDE_BLOCK: BLOCK_UNTIL, OVERRIDE_ALLOW: ALLOW_UNTIL, OVERRIDE_REVOKE: NO_OVERRIDE}


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


def _entry(kind: str | None, valid_until: Any, now: datetime) -> dict[str, Any] | None:
    """One override, normalised; None if it cannot be read."""
    if kind is None:
        return None
    if kind == NO_OVERRIDE:
        return override(NO_OVERRIDE)
    until = parse_utc(valid_until)
    if until is None:
        return None
    if until <= now:
        return override(NO_OVERRIDE)
    return override(kind, until)


_RANK = {NO_OVERRIDE: 0, ALLOW_UNTIL: 1, BLOCK_UNTIL: 2}
_NEVER = datetime.min.replace(tzinfo=dt_util.UTC)


def _weight(entry: dict[str, Any]) -> tuple[int, datetime]:
    return _RANK[entry["type"]], parse_utc(entry["valid_until"]) or _NEVER


def _strongest(entries: list[dict[str, Any] | None]) -> dict[str, Any] | None:
    """The override in force among several.

    A lock wins over extra time, extra time over nothing, and of two of a
    kind the one ending last; any unreadable entry makes it unknown.
    """
    if any(entry is None for entry in entries):
        return None
    return max(entries, key=_weight, default=override(NO_OVERRIDE))


def parse_mobile_override(
    payload: Any, platform: str, now: datetime | None = None
) -> dict[str, Any] | None:
    """The override in force for ``platform`` in the mobile API's answer.

    ``GET /v1/devicelimits/{id}/overrides`` answers
    ``{"lockablePlatforms": [{"appliesTo": "Windows", "overrides": [...]}]}``;
    a platform with an empty ``overrides`` list has nothing in force.
    """
    now = now or dt_util.utcnow()
    if not isinstance(payload, dict) or not isinstance(payload.get("lockablePlatforms"), list):
        return None
    entries: list[dict[str, Any] | None] = []
    for item in payload["lockablePlatforms"]:
        if not isinstance(item, dict) or str(item.get("appliesTo", "")).lower() != platform.lower():
            continue
        for raw in item.get("overrides") or []:
            if not isinstance(raw, dict):
                entries.append(None)
                continue
            raw_type = next((raw[k] for k in _MOBILE_TYPE_KEYS if raw.get(k) is not None), None)
            entries.append(_entry(_MOBILE_TYPES.get(str(raw_type or "").lower()), raw.get("validUntil"), now))
    return _strongest(entries)


def parse_xbox_override(payload: Any, now: datetime | None = None) -> dict[str, Any] | None:
    """The override in force in the Xbox Family Settings answer.

    ``GET .../settings/screentime/overrides`` answers
    ``{"overrides": [{"overrideType": 2, "validUntil": ...}]}`` (2 = block,
    1 = extra time, 100 = a release).
    """
    now = now or dt_util.utcnow()
    if not isinstance(payload, dict):
        return None
    raw_entries = payload.get("overrides")
    if raw_entries is None:
        raw_entries = []
    if not isinstance(raw_entries, list):
        return None
    return _strongest([
        _entry(_XBOX_TYPES.get(raw.get("overrideType")), raw.get("validUntil"), now)
        if isinstance(raw, dict) else None
        for raw in raw_entries
    ])


def active_until(record: dict[str, Any] | None, now: datetime | None = None) -> datetime | None:
    """The expiry of a blockUntil/allowUntil record still in the future, else None."""
    if not isinstance(record, dict) or record.get("type") not in (BLOCK_UNTIL, ALLOW_UNTIL):
        return None
    until = parse_utc(record.get("valid_until"))
    if until is None or until <= (now or dt_util.utcnow()):
        return None
    return until
