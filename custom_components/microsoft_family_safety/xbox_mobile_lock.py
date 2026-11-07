"""Xbox lock without the Family web session: the mobile-schedule fallback.

The Xbox lock is a blockUntil override on the Family web API, which needs the
Family web session. Microsoft caps that session at 24 hours on some accounts,
after which every Xbox web route answers 401 until someone signs in again. The
mobile schedule API (``/v4/devicelimits/schedules``) runs on the long-lived
mobile token and still writes the Xbox schedule the console enforces.

So when the web route cannot be used, an Xbox lock instead:

1. reads today's Xbox day exactly as Microsoft holds it (allowance in ms,
   allowed intervals, whether the Xbox schedule is on) and saves it in the
   platform-hold Store, before any write;
2. writes today's allowance as 0 (the intervals are kept as they are, and a
   schedule that was off is turned on: an allowance means nothing otherwise);
3. puts the saved day back at unlock, at the lock's end, and at midnight in
   any case (the lock covers today only, it is never carried to the next day),
   also after a restart that missed the moment.

Putting it back follows the ownership rule of the web lock: only when the day
still reads as we wrote it. A day a parent changed in the app since is left
as the parent set it (``left_parent_override``).

This module is a mixin of the coordinator; it uses the coordinator's hold
Store, hold locks and web API client.
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta
import logging
from typing import Any, Callable

from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.util import dt as dt_util

from .api_client import FamilySafetyWebAPI
from .const import (
    CONF_XBOX_LOCK_MOBILE_FALLBACK,
    DAY_KEYS,
    DEFAULT_XBOX_LOCK_MOBILE_FALLBACK,
    DOMAIN,
    LOCK_PATH_MOBILE_SCHEDULE,
    XBOX_SCHEDULE_NOT_RESTORED_ISSUE,
)
from .screen_time import (
    BLOCK_UNTIL,
    NO_OVERRIDE,
    REASON_LOCKED,
    UNKNOWN,
    iso_utc,
    override as make_override,
    parse_allowance_minutes,
    parse_utc,
    platform_allowance,
)

_LOGGER = logging.getLogger(__name__)

#: Family context states in which the Xbox web routes cannot answer.
_WEB_DOWN_STATES = frozenset({"auth_required", "missing"})


def weekday_index(moment: datetime) -> int:
    """Microsoft's day index (0 = Sunday) of a local time."""
    return (moment.weekday() + 1) % 7


def next_local_midnight(moment: datetime) -> datetime:
    """The local midnight that ends ``moment``'s day."""
    local = dt_util.as_local(moment)
    return dt_util.start_of_local_day(local.date() + timedelta(days=1))


def _slots(intervals: Any) -> list[bool]:
    return FamilySafetyWebAPI._intervals_to_slots(intervals if isinstance(intervals, list) else [])


def _ms_to_hms(ms: int) -> str:
    seconds = int(ms) // 1000
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def day_details(day: dict[str, Any] | None) -> dict[str, Any] | None:
    """A schedule day for service answers: minutes, intervals, schedule on/off."""
    if not isinstance(day, dict):
        return None
    return {
        "type": "schedule",
        "allowance_minutes": int(day.get("allowance") or 0) // 60_000,
        "allotted_intervals": [dict(item) for item in day.get("allottedIntervals") or []],
        "schedule_enabled": day.get("enabled", True) is not False,
        "valid_until": None,
        "override_until": None,
        "allow_until": None,
    }


class XboxMobileLockMixin:
    """Coordinator part: the Xbox lock through today's mobile schedule day."""

    def _init_xbox_mobile_lock(self, entry) -> None:
        self._xbox_mobile_fallback = bool(
            entry.options.get(CONF_XBOX_LOCK_MOBILE_FALLBACK, DEFAULT_XBOX_LOCK_MOBILE_FALLBACK)
        )
        #: hold key -> cancel callback of the timer that puts the day back.
        self._mobile_restore_timers: dict[str, Callable[[], None]] = {}

    # ------------------------------------------------------------------
    # When to use it
    # ------------------------------------------------------------------

    @staticmethod
    def _is_mobile_hold(hold: Any) -> bool:
        return isinstance(hold, dict) and hold.get("path") == LOCK_PATH_MOBILE_SCHEDULE

    def _xbox_web_unavailable(self) -> bool:
        """True when the Xbox web override cannot be sent at all right now."""
        if not self._web_override_available("Xbox"):
            return True
        return getattr(self.web_api, "family_context_state", None) in _WEB_DOWN_STATES

    def _xbox_web_failed_for_session(self) -> bool:
        """After a failed Xbox web write: was it the session (401, context not ready)?"""
        web = self.web_api
        if web is None:
            return True
        if getattr(web, "last_isolated_error_code", None) == "HTTP_401":
            return True
        return getattr(web, "family_context_state", "ready") != "ready"

    # ------------------------------------------------------------------
    # Ownership
    # ------------------------------------------------------------------

    @staticmethod
    def _mobile_day_is_ours(hold: dict[str, Any], day: dict[str, Any]) -> bool:
        """The raw day Microsoft holds is still the one this lock wrote."""
        written = hold.get("written") or {}
        return (
            day.get("allowance") == written.get("allowance")
            and _slots(day.get("allottedIntervals")) == _slots(written.get("allottedIntervals"))
            and day.get("enabled", True) is not False
        )

    @staticmethod
    def _mobile_day_is_saved(hold: dict[str, Any], day: dict[str, Any]) -> bool:
        saved = hold.get("saved") or {}
        return (
            day.get("allowance") == saved.get("allowance")
            and _slots(day.get("allottedIntervals")) == _slots(saved.get("allottedIntervals"))
            and (day.get("enabled", True) is not False) == (hold.get("saved_enabled") is not False)
        )

    def _policy_matches_written(
        self, hold: dict[str, Any], policy: Any
    ) -> bool | None:
        """The polled (converted) Xbox policy still shows our write; None if unknown."""
        if not isinstance(policy, dict):
            return None
        day = (policy.get("dailyRestrictions") or {}).get(DAY_KEYS[int(hold["day"])])
        if not isinstance(day, dict) or day.get("allowance") is None:
            return None
        written = hold.get("written") or {}
        return (
            parse_allowance_minutes(day.get("allowance"))
            == int(written.get("allowance") or 0) // 60_000
            and _slots(day.get("allottedIntervals")) == _slots(written.get("allottedIntervals"))
            and policy.get("isEnabled") is not False
        )

    def xbox_mobile_lock_view(
        self, account_id: Any, account: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        """Our mobile-schedule Xbox lock in force for this child, from polled data.

        ``{"hold": ..., "ours": bool}``; ``ours`` is False only when the polled
        Xbox day no longer shows our write (a parent changed it in the app).
        None when no such lock is in force.
        """
        hold = self._platform_holds.get(self._hold_key(account_id, "xbox"))
        if (
            not self._is_mobile_hold(hold)
            or not hold.get("confirmed")
            or not self._hold_in_force(hold, dt_util.utcnow())
        ):
            return None
        if account is None:
            account = self.account_data(account_id) or {}
        matches = self._policy_matches_written(hold, account.get("xbox_screentime_policy"))
        return {"hold": hold, "ours": matches is not False}

    # ------------------------------------------------------------------
    # What the entities show
    # ------------------------------------------------------------------

    @staticmethod
    def _policy_with_saved_day(policy: Any, hold: dict[str, Any]) -> Any:
        """The polled Xbox policy with the locked day shown as it was saved."""
        if not isinstance(policy, dict):
            return policy
        shown = copy.deepcopy(policy)
        daily = shown.get("dailyRestrictions")
        key = DAY_KEYS[int(hold["day"])]
        if isinstance(daily, dict) and isinstance(daily.get(key), dict):
            saved = hold.get("saved") or {}
            slots = _slots(saved.get("allottedIntervals"))
            daily[key].update({
                "allowance": _ms_to_hms(int(saved.get("allowance") or 0)),
                "allottedIntervals": [dict(i) for i in saved.get("allottedIntervals") or []],
                "allowedIntervals": slots,
                "timeline": slots,
            })
        shown["isEnabled"] = hold.get("saved_enabled") is not False
        return shown

    def _xbox_mobile_lock_status(
        self, account_id: Any, policy: Any, used: Any, now: datetime
    ) -> dict[str, Any] | None:
        """platform_status while our mobile lock holds: locked, with the saved allowance."""
        view = self.xbox_mobile_lock_view(account_id)
        if view is None or not view["ours"]:
            return None
        hold = view["hold"]
        shown = self._policy_with_saved_day(policy, hold)
        base = platform_allowance(
            shown if isinstance(shown, dict) else None,
            used if isinstance(used, int) else None,
            now,
            None,
        )
        return {
            **base,
            "allowed": False,
            "remaining_minutes": 0,
            "reason": REASON_LOCKED,
            "override": make_override(BLOCK_UNTIL, parse_utc(hold.get("until"))),
        }

    def _xbox_mobile_hold_state(self, account_id: Any) -> dict[str, Any] | None:
        """platform_hold_state while a mobile lock of ours is recorded."""
        view = self.xbox_mobile_lock_view(account_id)
        if view is None:
            return None
        if not view["ours"]:
            # A parent changed the day in the app: our lock no longer decides,
            # and what is in force is the parent's schedule, not an override.
            return {
                "hold": "none",
                "override": NO_OVERRIDE,
                "override_until": None,
                "allow_until": None,
                "lock_path": LOCK_PATH_MOBILE_SCHEDULE,
            }
        until = parse_utc(view["hold"].get("until"))
        return {
            "hold": "ours",
            "override": BLOCK_UNTIL,
            "override_until": dt_util.as_local(until).isoformat() if until else None,
            "allow_until": None,
            "lock_path": LOCK_PATH_MOBILE_SCHEDULE,
        }

    def _apply_xbox_mobile_lock_state(self, account_id: Any, account_data: dict[str, Any]) -> None:
        """Poll: the lock switch reads Xbox locked while our mobile lock holds."""
        view = self.xbox_mobile_lock_view(account_id, account_data)
        if view is None or not view["ours"]:
            return
        blocked = [p for p in (account_data.get("blocked_platforms") or []) if p != "Xbox"]
        blocked.append("Xbox")
        account_data["blocked_platforms"] = blocked
        self._xbox_lock_known.add(str(account_id))
        account_data["xbox_lock_known"] = True

    def _show_xbox_day(self, account_id: Any, hold: dict[str, Any], day: dict[str, Any]) -> None:
        """Put a day just written into the polled policy, until the next poll reads it."""
        key = DAY_KEYS[int(hold["day"])]

        def _update(policy: dict) -> None:
            entry = (policy.get("dailyRestrictions") or {}).get(key)
            if isinstance(entry, dict):
                slots = _slots(day.get("allottedIntervals"))
                entry["allowance"] = _ms_to_hms(int(day.get("allowance") or 0))
                entry["allottedIntervals"] = [dict(i) for i in day.get("allottedIntervals") or []]
                entry["allowedIntervals"] = slots
                entry["timeline"] = slots
            policy["isEnabled"] = day.get("enabled", True) is not False

        self._set_xbox_policy_field(str(account_id), _update)

    # ------------------------------------------------------------------
    # Lock, put back
    # ------------------------------------------------------------------

    async def _async_lock_xbox_mobile(
        self, account_id: str, requested: datetime, key: str
    ) -> dict[str, Any]:
        """Lock today's Xbox day. The caller holds the hold lock, holds loaded."""
        if self.web_api is None:
            raise HomeAssistantError("Cannot lock Xbox: the Microsoft API is not ready yet.")
        hold = self._platform_holds.get(key)
        if self._is_mobile_hold(hold) and not self._hold_in_force(hold, dt_util.utcnow()):
            # A lock that should have ended already (a missed restore): its
            # day goes back first, or the new lock would save the zero.
            outcome, _ = await self._async_put_back_mobile_day(account_id, hold)
            _LOGGER.info("Xbox of %s: earlier schedule lock put back before locking (%s)", account_id, outcome)
            self._platform_holds.pop(key, None)
            await self._async_save_platform_holds()
            self._cancel_mobile_restore(key)
            hold = None
        now = dt_util.now()
        day_index = weekday_index(now)
        try:
            current = await self.web_api.get_mobile_xbox_day(account_id, day_index)
        except Exception as err:
            raise HomeAssistantError(
                "Cannot lock Xbox: the Family web session is not available and "
                f"today's Xbox schedule could not be read ({err}). Nothing was changed."
            ) from err
        target = min(dt_util.as_utc(requested), dt_util.as_utc(next_local_midnight(now)))
        already_ours = False
        if hold is not None:
            if self._mobile_day_is_ours(hold, current):
                already_ours = bool(hold.get("confirmed"))
            elif hold.get("confirmed"):
                # A parent changed the day in the app since our confirmed
                # lock: theirs stands, as a parent's web override would.
                _LOGGER.info(
                    "Xbox schedule of %s was changed since our lock; not locking over it",
                    account_id,
                )
                self._platform_holds.pop(key, None)
                await self._async_save_platform_holds()
                self._cancel_mobile_restore(key)
                self._clear_not_restored_issue(key)
                return {
                    "prior": dict(hold.get("prior") or make_override(UNKNOWN)),
                    "applied": "left_parent_override",
                    "lock_path": LOCK_PATH_MOBILE_SCHEDULE,
                    "current": day_details(current),
                }
            else:
                # Not (or no longer) our write: what Microsoft holds now, a
                # parent's change included, is what comes back at unlock.
                hold["saved"] = {
                    "allowance": current["allowance"],
                    "allottedIntervals": current["allottedIntervals"],
                }
                hold["saved_enabled"] = current["enabled"]
            previous = parse_utc(hold.get("until"))
            if previous is not None and previous > target:
                target = previous
        else:
            hold = {
                "account_id": str(account_id),
                "platform": "Xbox",
                "path": LOCK_PATH_MOBILE_SCHEDULE,
                "prior": make_override(UNKNOWN),
                "day": day_index,
                "date": now.date().isoformat(),
                "saved": {
                    "allowance": current["allowance"],
                    "allottedIntervals": current["allottedIntervals"],
                },
                "saved_enabled": current["enabled"],
                "confirmed": False,
                "created": iso_utc(dt_util.utcnow()),
            }
            self._platform_holds[key] = hold
        hold["written"] = {
            "allowance": 0,
            "allottedIntervals": [dict(i) for i in hold["saved"]["allottedIntervals"]],
        }
        hold["until"] = iso_utc(target)
        hold["requested_until"] = iso_utc(requested)
        # The restore point is on disk before anything is written.
        await self._async_save_platform_holds()
        # Whatever happens next, the day is looked at again by its end.
        self._schedule_mobile_restore(key, target)
        if not already_ours:
            try:
                await self.web_api.set_mobile_xbox_day(
                    account_id, day_index, 0, hold["written"]["allottedIntervals"]
                )
                if not current["enabled"]:
                    await self.web_api.set_mobile_schedule_enabled(account_id, True, platform="Xbox")
            except Exception as err:
                raise HomeAssistantError(
                    "Cannot lock Xbox: the Family web session is not available and "
                    f"Microsoft refused the Xbox schedule change ({err})."
                ) from err
        hold["confirmed"] = True
        hold["confirmed_at"] = iso_utc(dt_util.utcnow())
        await self._async_save_platform_holds()
        self._show_xbox_day(account_id, hold, {**hold["written"], "enabled": True})
        self._xbox_lock_known.add(str(account_id))
        await self.async_request_refresh()
        return {
            "prior": dict(hold["prior"]),
            "applied": make_override(BLOCK_UNTIL, target),
            "lock_path": LOCK_PATH_MOBILE_SCHEDULE,
            "saved_day": day_details({**hold["saved"], "enabled": hold.get("saved_enabled")}),
        }

    async def _async_put_back_mobile_day(
        self, account_id: str, hold: dict[str, Any]
    ) -> tuple[str, dict[str, Any]]:
        """Write the saved day back if it still shows our lock.

        Returns ``("restored" | "parent" | "not_locked", current day)``.
        Raises when the day cannot be read or written: nothing is assumed.
        """
        if self.web_api is None:
            raise HomeAssistantError("The Microsoft API is not ready yet.")
        day_index = int(hold["day"])
        current = await self.web_api.get_mobile_xbox_day(account_id, day_index)
        if self._mobile_day_is_ours(hold, current) and not (
            # Saved and written are the same (a day already at 0): nothing to do.
            self._mobile_day_is_saved(hold, current)
        ):
            saved = hold["saved"]
            await self.web_api.set_mobile_xbox_day(
                account_id, day_index, int(saved["allowance"]), saved["allottedIntervals"]
            )
            if hold.get("saved_enabled") is False:
                await self.web_api.set_mobile_schedule_enabled(account_id, False, platform="Xbox")
            self._show_xbox_day(
                account_id, hold, {**saved, "enabled": hold.get("saved_enabled") is not False}
            )
            return "restored", current
        if self._mobile_day_is_saved(hold, current):
            return ("restored" if hold.get("confirmed") else "not_locked"), current
        return ("parent" if hold.get("confirmed") else "not_locked"), current

    async def _async_unlock_xbox_mobile(
        self, account_id: str, key: str, hold: dict[str, Any]
    ) -> dict[str, Any]:
        """unlock_platform for a mobile-schedule lock. The caller holds the hold lock."""
        expired = bool(hold.get("until")) and not self._hold_in_force(hold, dt_util.utcnow())
        try:
            outcome, current = await self._async_put_back_mobile_day(account_id, hold)
        except Exception as err:
            raise HomeAssistantError(
                f"Could not put the Xbox schedule back ({err}); the lock is kept and "
                "is put back by its end at the latest."
            ) from err
        self._platform_holds.pop(key, None)
        await self._async_save_platform_holds()
        self._cancel_mobile_restore(key)
        self._clear_not_restored_issue(key)
        saved = {**hold["saved"], "enabled": hold.get("saved_enabled") is not False}
        result: dict[str, Any] = {
            "prior": dict(hold.get("prior") or make_override(UNKNOWN)),
            "lock_path": LOCK_PATH_MOBILE_SCHEDULE,
            "saved_day": day_details(saved),
        }
        if expired:
            result["applied"] = "hold_expired"
            result["restored"] = outcome == "restored"
        elif outcome == "parent":
            _LOGGER.info(
                "Xbox schedule of %s was changed since our lock; left as it is", account_id
            )
            result["applied"] = "left_parent_override"
            result["current"] = day_details(current)
        elif outcome == "not_locked":
            result["applied"] = "not_locked"
            result["current"] = day_details(current)
        else:
            result["applied"] = make_override(NO_OVERRIDE)
        await self.async_request_refresh()
        return result

    async def _async_xbox_not_locked(self, account_id: str) -> dict[str, Any]:
        """unlock_platform with no lock of ours and no web session: nothing to undo."""
        current = None
        if self.web_api is not None:
            try:
                current = await self.web_api.get_mobile_xbox_day(
                    account_id, weekday_index(dt_util.now())
                )
            except Exception as err:  # noqa: BLE001 - only informative
                _LOGGER.debug("Could not read the Xbox day of %s: %r", account_id, err)
        return {
            "prior": None,
            "applied": "not_locked",
            "lock_path": LOCK_PATH_MOBILE_SCHEDULE,
            "current": day_details(current),
        }

    # ------------------------------------------------------------------
    # Putting the day back at the lock's end (and at midnight)
    # ------------------------------------------------------------------

    def _schedule_mobile_restore(self, key: str, when: datetime) -> None:
        self._cancel_mobile_restore(key)

        @callback
        def _due(_now: datetime) -> None:
            self._mobile_restore_timers.pop(key, None)
            self.entry.async_create_background_task(
                self.hass,
                self._async_restore_mobile_hold_due(key, refresh=True),
                f"microsoft_family_safety restore Xbox day {key}",
            )

        self._mobile_restore_timers[key] = async_track_point_in_utc_time(
            self.hass, _due, dt_util.as_utc(when)
        )

    def _cancel_mobile_restore(self, key: str) -> None:
        cancel = self._mobile_restore_timers.pop(key, None)
        if cancel is not None:
            cancel()

    def _not_restored_issue_id(self, key: str) -> str:
        return f"{XBOX_SCHEDULE_NOT_RESTORED_ISSUE}_{self.entry.entry_id}_{key}"

    def _raise_not_restored_issue(self, key: str, err: Exception) -> None:
        """A lock past its end whose day is still not back: say so in Repairs."""
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            self._not_restored_issue_id(key),
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key=XBOX_SCHEDULE_NOT_RESTORED_ISSUE,
            translation_placeholders={
                "title": self.entry.title or DOMAIN,
                "error": str(err)[:200],
            },
        )

    def _clear_not_restored_issue(self, key: str) -> None:
        ir.async_delete_issue(self.hass, DOMAIN, self._not_restored_issue_id(key))

    def _cancel_all_mobile_restores(self) -> None:
        for key in list(self._mobile_restore_timers):
            self._cancel_mobile_restore(key)

    async def _async_restore_mobile_hold_due(self, key: str, *, refresh: bool = False) -> bool:
        """Put back a mobile lock whose end has come. True when the hold was settled."""
        async with self._hold_lock(key):
            hold = self._platform_holds.get(key)
            if not self._is_mobile_hold(hold) or self._hold_in_force(hold, dt_util.utcnow()):
                return False
            account_id = str(hold.get("account_id"))
            try:
                outcome, _ = await self._async_put_back_mobile_day(account_id, hold)
            except Exception as err:  # noqa: BLE001 - retried on the next poll
                _LOGGER.warning(
                    "Could not put the Xbox schedule of %s back at the lock's end "
                    "(retried on the next update): %s", account_id, err,
                )
                self._raise_not_restored_issue(key, err)
                return False
            self._platform_holds.pop(key, None)
            await self._async_save_platform_holds()
            self._cancel_mobile_restore(key)
            self._clear_not_restored_issue(key)
            _LOGGER.info("Xbox schedule lock of %s ended: %s", account_id, outcome)
        if refresh:
            await self.async_request_refresh()
        return True

    async def _async_check_mobile_holds(self) -> None:
        """Poll: put back what is due (a restart past the end too), arm the timers."""
        await self._async_load_platform_holds()
        now = dt_util.utcnow()
        for key, hold in list(self._platform_holds.items()):
            if not self._is_mobile_hold(hold):
                continue
            if self._hold_in_force(hold, now):
                if key not in self._mobile_restore_timers:
                    self._schedule_mobile_restore(key, parse_utc(hold["until"]))
                continue
            await self._async_restore_mobile_hold_due(key)
