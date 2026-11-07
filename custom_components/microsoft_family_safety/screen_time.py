"""Pure screen-time helpers: overrides, and whether a platform is allowed now.

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
import re
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


# ----------------------------------------------------------------------
# Allowed now / time remaining
# ----------------------------------------------------------------------

_DAY_KEYS = ("sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday")

REASON_LOCKED = "locked"
REASON_EXTRA_TIME = "extra_time"
REASON_NO_LIMITS = "no_limits"
REASON_OUTSIDE_HOURS = "outside_allowed_hours"
REASON_ALLOWANCE_USED = "allowance_used"
REASON_WITHIN_LIMITS = "within_limits"
REASON_UNKNOWN = "unknown"


def parse_allowance_minutes(allowance: str | int | float | None) -> int:
    """Parse Microsoft allowance values ("HH:MM:SS", "PT2H30M") to total minutes."""
    if allowance is None:
        return 0
    if isinstance(allowance, (int, float)):
        return int(allowance)
    try:
        text = str(allowance).strip()
        if text.upper().startswith("PT"):
            match = re.fullmatch(
                r"PT(?:(\d+)H)?(?:(\d+)M)?(?:\d+(?:\.\d+)?S)?",
                text.upper(),
            )
            if not match:
                return 0
            return int(match.group(1) or 0) * 60 + int(match.group(2) or 0)
        parts = text.split(":")
        hours = int(parts[0])
        minutes = int(parts[1]) if len(parts) > 1 else 0
        return hours * 60 + minutes
    except (TypeError, ValueError, IndexError):
        return 0


def _clock_minutes(value: Any) -> int | None:
    """"HH:MM[:SS]" or .NET "1.00:00:00" (midnight at the end) to minutes."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    days = 0
    head = text.split(":")[0]
    if "." in head:
        day_part, text = text.split(".", 1)
        try:
            days = int(day_part or 0)
        except ValueError:
            return None
    try:
        parts = [int(float(p)) for p in text.split(":")[:2]] + [0]
    except ValueError:
        return None
    total = days * 1440 + parts[0] * 60 + parts[1]
    return total if 0 <= total <= 1440 else None


def day_slots(day: dict[str, Any]) -> list[bool] | None:
    """The day's 48 half-hour slots; None when the day carries no windows."""
    for key in ("timeline", "allowedIntervals", "AllowedIntervals"):
        value = day.get(key)
        if isinstance(value, list) and len(value) == 48 and all(
            isinstance(item, bool) for item in value
        ):
            return list(value)
    for key in ("allowedIntervals", "AllowedIntervals", "allottedIntervals"):
        value = day.get(key)
        if not isinstance(value, list):
            continue
        slots = [False] * 48
        for item in value:
            if not isinstance(item, dict):
                continue
            begin = _clock_minutes(item.get("begin") or item.get("Begin") or item.get("start"))
            end = _clock_minutes(item.get("end") or item.get("End"))
            if begin is None or end is None or end <= begin:
                continue
            for slot in range(begin // 30, min(48, -(-end // 30))):
                slots[slot] = True
        return slots
    return None


def platform_allowance(
    policy: dict[str, Any] | None,
    used_minutes: int | None,
    now: datetime,
    current_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Whether a platform may be used at ``now`` (local time), and for how long.

    Takes, in this order: an active override (a blockUntil locks, an
    allowUntil grants time until it ends), limits turned off, today's allowed
    hours, then today's allowance against today's usage. The minutes left are
    the smaller of the allowance left and the time to the end of the current
    allowed window (midnight at the latest); 0 whenever not allowed.

    Returns ``allowed`` (None when unknown), ``remaining_minutes`` (None when
    unknown), ``reason`` and the inputs used, for the entities' attributes.
    """
    minute_of_day = now.hour * 60 + now.minute + now.second / 60
    to_midnight = int(1440 - minute_of_day)
    result: dict[str, Any] = {
        "allowed": None,
        "remaining_minutes": None,
        "reason": REASON_UNKNOWN,
        "allowance_minutes": None,
        "used_minutes": used_minutes,
        "in_allowed_hours": None,
        "override": current_override,
    }

    kind = (current_override or {}).get("type")
    if kind == BLOCK_UNTIL and (
        current_override.get("valid_until") is None
        or active_until(current_override, dt_util.as_utc(now)) is not None
    ):
        return {**result, "allowed": False, "remaining_minutes": 0, "reason": REASON_LOCKED}
    if kind == ALLOW_UNTIL:
        until = active_until(current_override, dt_util.as_utc(now))
        if until is not None:
            left = int((until - dt_util.as_utc(now)).total_seconds() // 60)
            return {
                **result, "allowed": True, "remaining_minutes": min(left, to_midnight),
                "reason": REASON_EXTRA_TIME,
            }

    if not isinstance(policy, dict):
        return result
    enabled = policy.get("isEnabled", policy.get("IsEnabled"))
    if enabled is False:
        return {
            **result, "allowed": True, "remaining_minutes": to_midnight,
            "reason": REASON_NO_LIMITS,
        }
    daily = policy.get("dailyRestrictions") or policy.get("DailyRestrictions")
    day_key = _DAY_KEYS[(now.weekday() + 1) % 7]
    day = (
        daily.get(day_key) or daily.get(day_key.capitalize())
        if isinstance(daily, dict)
        else None
    )
    if not isinstance(day, dict):
        return result

    allowance = parse_allowance_minutes(day.get("allowance", day.get("Allowance")))
    result["allowance_minutes"] = allowance
    slots = day_slots(day) or [True] * 48
    slot = min(int(minute_of_day // 30), 47)
    in_hours = slots[slot]
    result["in_allowed_hours"] = in_hours
    if not in_hours:
        return {**result, "allowed": False, "remaining_minutes": 0, "reason": REASON_OUTSIDE_HOURS}
    end_slot = slot
    while end_slot < 48 and slots[end_slot]:
        end_slot += 1
    to_window_end = int(end_slot * 30 - minute_of_day)

    if allowance >= 1440:
        left = to_window_end
    elif used_minutes is None:
        return result
    else:
        left = max(allowance - int(used_minutes), 0)
    if left <= 0:
        return {**result, "allowed": False, "remaining_minutes": 0, "reason": REASON_ALLOWANCE_USED}
    return {
        **result, "allowed": True, "remaining_minutes": min(left, to_window_end),
        "reason": REASON_WITHIN_LIMITS,
    }
