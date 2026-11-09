"""Xbox lock through the Family Safety *mobile* device-limits override.

A headless alternative to the schedule rewrite (``xbox_mobile_lock.py``): the
Family Safety mobile API has its own "override a device's screen-time now"
endpoint, ``/v4/devicelimits/{child}/overrides``, used by pyfamilysafety
(``account.override_device``). It runs on the same long-lived mobile token the
integration already holds -- no Family web session, no Xbox Live / SISU / device
attestation (the wall the xapprove effort hit).

Compared with the schedule rewrite this is cleaner: nothing to save and put
back, no midnight cap and no restore timers, because Microsoft stores a real
``validUntil`` and expires the block itself. So a lock here is a BlockUntil
override, unlock is a Cancel, and extra time is an allow override.

CAVEAT worth keeping in mind: the fork's own ``api_client`` notes that the
mobile aggregator *accepts and stores* an Xbox override but that Microsoft
"stopped honouring it on the console" -- which is why the schedule rewrite
exists at all. The deep-research finding says the proper ``devicelimits/
overrides`` call should work. Only a live check settles it, which is what
``probe_xbox_mobile_overrides`` and ``test_xbox_mobile_override`` are for; and
even those confirm only that Microsoft accepts, stores and reads back the
override, not that the console physically blocks (that needs eyes on the Xbox).
So this path is **default off** (``CONF_XBOX_MOBILE_OVERRIDE``) and the schedule
rewrite stays the fallback whenever the override is unavailable or refused.

This module is a mixin of the coordinator; it uses the coordinator's hold
Store, hold locks and web API client.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import logging
from typing import Any

from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.util import dt as dt_util

from .api_client import (
    MOBILE_OVERRIDE_ALLOW,
    MOBILE_OVERRIDE_BLOCK,
    MOBILE_OVERRIDE_CANCEL,
)
from .const import (
    CONF_XBOX_MOBILE_OVERRIDE,
    DEFAULT_XBOX_MOBILE_OVERRIDE,
    LOCK_PATH_MOBILE_OVERRIDE,
)
from .screen_time import (
    ALLOW_UNTIL,
    BLOCK_UNTIL,
    NO_OVERRIDE,
    UNKNOWN,
    iso_utc,
    override as make_override,
    parse_utc,
)

_LOGGER = logging.getLogger(__name__)

#: How long the bounded live self-test blocks for before clearing itself.
_SELF_TEST_SECONDS = 150


class XboxMobileOverrideUnavailable(Exception):
    """The mobile override cannot be used now; the caller falls back.

    Deliberately not a ``HomeAssistantError``: it is a control-flow signal
    inside the coordinator, never surfaced to a user as an error.
    """


class XboxMobileOverrideMixin:
    """Coordinator part: the Xbox lock through a mobile device-limits override."""

    def _init_xbox_mobile_override(self, entry) -> None:
        self._xbox_mobile_override = bool(
            entry.options.get(CONF_XBOX_MOBILE_OVERRIDE, DEFAULT_XBOX_MOBILE_OVERRIDE)
        )

    @property
    def _xbox_mobile_override_enabled(self) -> bool:
        """The option is on and a mobile client exists to send the override."""
        return bool(self._xbox_mobile_override and self.web_api is not None)

    @staticmethod
    def _is_override_hold(hold: Any) -> bool:
        return isinstance(hold, dict) and hold.get("path") == LOCK_PATH_MOBILE_OVERRIDE

    @staticmethod
    def _xbox_blocked_in(summary: dict[str, Any]) -> bool:
        """True when a summary from ``summarize_mobile_overrides`` blocks Xbox."""
        xbox = (summary.get("platforms") or {}).get("Xbox")
        return bool(isinstance(xbox, dict) and xbox.get("blocked"))

    @staticmethod
    def _xbox_present_in(summary: dict[str, Any]) -> bool:
        return "Xbox" in (summary.get("platforms") or {})

    # ------------------------------------------------------------------
    # Lock, unlock, release, extra time
    # ------------------------------------------------------------------

    async def _async_lock_xbox_override(
        self, account_id: str, requested: datetime, key: str
    ) -> dict[str, Any]:
        """Block the Xbox now with a BlockUntil override, confirmed from the reply.

        Raises :class:`XboxMobileOverrideUnavailable` (so the caller falls back
        to the schedule rewrite) when the override cannot be used -- no client,
        the state cannot be read, another lock is already in force, or Microsoft
        refuses or does not apply the block.
        """
        web = self.web_api
        if web is None:
            raise XboxMobileOverrideUnavailable("the Microsoft API is not ready")
        target = dt_util.as_utc(requested)
        async with self._hold_lock(key):
            await self._async_load_platform_holds()
            now = dt_util.utcnow()
            hold = self._platform_holds.get(key)
            if self._is_mobile_hold(hold) and self._hold_in_force(hold, now):
                # A schedule lock already holds the console: leave that path in
                # charge rather than running two mechanisms at once.
                raise XboxMobileOverrideUnavailable("a schedule lock is in force")
            if (
                hold is not None
                and not self._is_override_hold(hold)
                and hold.get("confirmed")
                and self._hold_in_force(hold, now)
            ):
                raise XboxMobileOverrideUnavailable("a web lock is in force")
            try:
                before = web.summarize_mobile_overrides(
                    await web.get_mobile_device_overrides(account_id), "Xbox"
                )
            except Exception as err:  # noqa: BLE001 - fall back to the schedule
                raise XboxMobileOverrideUnavailable(
                    f"the override state could not be read ({err})"
                ) from err
            if not self._xbox_present_in(before):
                raise XboxMobileOverrideUnavailable(
                    "this child has no Xbox in device limits"
                )
            if (
                self._xbox_blocked_in(before)
                and not self._is_override_hold(hold)
            ):
                # A block we did not place (a parent's): never override it.
                raise XboxMobileOverrideUnavailable("a block not ours is in force")
            previous = parse_utc(hold.get("until")) if self._is_override_hold(hold) else None
            if previous is not None and previous > target:
                target = previous
            if not self._is_override_hold(hold):
                hold = {
                    "account_id": str(account_id),
                    "platform": "Xbox",
                    "path": LOCK_PATH_MOBILE_OVERRIDE,
                    "prior": make_override(UNKNOWN),
                    "confirmed": False,
                    "created": iso_utc(now),
                }
                self._platform_holds[key] = hold
            hold["until"] = iso_utc(target)
            hold["requested_until"] = iso_utc(requested)
            # The hold is on disk before the write: a retry after a crash can
            # tell its own block from a parent's.
            await self._async_save_platform_holds()
            try:
                reply = await web.set_mobile_device_override(
                    account_id, "Xbox", MOBILE_OVERRIDE_BLOCK, target
                )
            except Exception as err:  # noqa: BLE001 - fall back to the schedule
                if not hold.get("confirmed"):
                    self._platform_holds.pop(key, None)
                    await self._async_save_platform_holds()
                raise XboxMobileOverrideUnavailable(
                    f"Microsoft refused the override ({err})"
                ) from err
            if not self._xbox_blocked_in(web.summarize_mobile_overrides(reply, "Xbox")):
                # Accepted the POST but did not show the block: do not claim a
                # lock the console may not have. Let the schedule rewrite try.
                if not hold.get("confirmed"):
                    self._platform_holds.pop(key, None)
                    await self._async_save_platform_holds()
                raise XboxMobileOverrideUnavailable(
                    "Microsoft did not report the Xbox block after the override"
                )
            hold["confirmed"] = True
            hold["confirmed_at"] = iso_utc(dt_util.utcnow())
            await self._async_save_platform_holds()
            self._platform_override_until[(str(account_id), "xbox")] = target
            self._xbox_lock_known.add(str(account_id))
        _LOGGER.info(
            "Xbox of %s blocked through a mobile device-limits override until %s",
            account_id, iso_utc(target),
        )
        await self.async_request_refresh()
        return {
            "prior": dict(hold["prior"]),
            "applied": make_override(BLOCK_UNTIL, target),
            "lock_path": LOCK_PATH_MOBILE_OVERRIDE,
        }

    async def _async_unlock_xbox_override(
        self, account_id: str, key: str, hold: dict[str, Any]
    ) -> dict[str, Any]:
        """unlock_platform for a mobile-override lock: cancel it and forget the hold."""
        web = self.web_api
        prior = dict(hold.get("prior") or make_override(UNKNOWN))
        expired = bool(hold.get("until")) and not self._hold_in_force(hold, dt_util.utcnow())
        applied: Any
        if expired:
            applied = "hold_expired"
        elif web is None:
            raise HomeAssistantError("The Microsoft API is not ready yet.")
        else:
            try:
                await web.set_mobile_device_override(
                    account_id, "Xbox", MOBILE_OVERRIDE_CANCEL, dt_util.utcnow()
                )
            except Exception as err:
                raise HomeAssistantError(
                    f"Could not cancel the Xbox override ({err}); the lock is kept. "
                    "It expires on its own at the time it was set for."
                ) from err
            applied = make_override(NO_OVERRIDE)
        self._platform_holds.pop(key, None)
        await self._async_save_platform_holds()
        self._platform_override_until.pop((str(account_id), "xbox"), None)
        await self.async_request_refresh()
        return {"prior": prior, "applied": applied, "lock_path": LOCK_PATH_MOBILE_OVERRIDE}

    async def _async_release_xbox_override(
        self, account_id: str, key: str, hold: dict[str, Any]
    ) -> None:
        """release_platform for a mobile-override lock: cancel and forget, no restore."""
        web = self.web_api
        if web is not None and self._hold_in_force(hold, dt_util.utcnow()):
            try:
                await web.set_mobile_device_override(
                    account_id, "Xbox", MOBILE_OVERRIDE_CANCEL, dt_util.utcnow()
                )
            except Exception as err:  # noqa: BLE001 - the switch still goes off
                _LOGGER.warning(
                    "Could not cancel the Xbox override of %s on release: %s",
                    account_id, err,
                )
        self._platform_holds.pop(key, None)
        await self._async_save_platform_holds()
        self._platform_override_until.pop((str(account_id), "xbox"), None)

    async def _async_bonus_xbox_override(
        self, account_id: str, until: datetime, key: str
    ) -> dict[str, Any]:
        """EXPERIMENTAL extra time on the Xbox through an allow override.

        INFERRED: ``MOBILE_OVERRIDE_ALLOW`` is a guess (pyfamilysafety models
        only block/cancel). Confirmed only when Microsoft reads an allow back.
        """
        web = self.web_api
        if web is None:
            raise HomeAssistantError("The Microsoft API is not ready yet.")
        until = dt_util.as_utc(until)
        async with self._hold_lock(key):
            await self._async_load_platform_holds()
            await self._async_load_bonuses()
            try:
                reply = await web.set_mobile_device_override(
                    account_id, "Xbox", MOBILE_OVERRIDE_ALLOW, until
                )
            except Exception as err:
                raise HomeAssistantError(
                    f"Microsoft refused the Xbox extra time (experimental mobile "
                    f"override): {err}"
                ) from err
            summary = web.summarize_mobile_overrides(reply, "Xbox")
            xbox = (summary.get("platforms") or {}).get("Xbox") or {}
            types = [str(t).lower() for t in xbox.get("override_types") or []]
            if MOBILE_OVERRIDE_ALLOW.lower() not in types:
                raise HomeAssistantError(
                    "Microsoft did not apply the Xbox extra time (experimental "
                    f"mobile override; read back override types {sorted(set(types))}). "
                    "Nothing is assumed."
                )
            released = self._platform_holds.pop(key, None) is not None
            if released:
                await self._async_save_platform_holds()
            self._platform_override_until.pop((str(account_id), "xbox"), None)
            self._bonuses[key] = {
                "account_id": str(account_id), "platform": "Xbox", "until": iso_utc(until),
            }
            await self._bonus_store.async_save(self._bonuses)
        await self.async_request_refresh()
        return {
            "platform": "Xbox",
            "applied_until": dt_util.as_local(until).isoformat(),
            "read_back": make_override(ALLOW_UNTIL, until),
            "released_hold": released,
            "lock_path": LOCK_PATH_MOBILE_OVERRIDE,
            "experimental": True,
        }

    # ------------------------------------------------------------------
    # Resolving which child / Xbox a service call means
    # ------------------------------------------------------------------

    def _child_accounts(self) -> dict[str, dict[str, Any]]:
        return {
            str(k): v
            for k, v in ((self.data or {}).get("accounts") or {}).items()
            if isinstance(v, dict)
        }

    def _resolve_child_account(self, child: str) -> str:
        """An account id or (case-insensitive) first name -> the account id."""
        accounts = self._child_accounts()
        if str(child) in accounts:
            return str(child)
        wanted = str(child).strip().lower()
        matches = [
            account_id
            for account_id, data in accounts.items()
            if str(data.get("first_name") or "").strip().lower() == wanted
        ]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise ServiceValidationError(
                f"No Microsoft Family Safety child matches {child!r}."
            )
        raise ServiceValidationError(
            f"More than one child is named {child!r}; give the account id instead."
        )

    # ------------------------------------------------------------------
    # Diagnostics: the read-only probe and the bounded live self-test
    # ------------------------------------------------------------------

    async def async_probe_xbox_mobile_overrides(
        self, confirm_risk: bool = False, raw_shape: bool = False
    ) -> dict[str, Any]:
        """Read every child's device-limits override state (GET only, no writes).

        Non-secret facts only (no tokens, xuids, gamertags or device ids): for
        each child, whether Xbox appears in device limits, whether it is blocked
        right now, and the override types/counts. This is what the HA session
        runs first to confirm the endpoint answers live before any write.

        ``raw_shape`` adds, per child, the KEY NAMES present in the Xbox
        lockablePlatforms entry (and in its override/device entries, if any) --
        names only, no values and no ids -- so the exact shape can be confirmed
        and a missed bindable field ruled out. (Confirmed live 2026-11: this
        family's Xbox entry has no ``devices`` entries, so there is no device id
        to target; the override is platform-level only.)
        """
        if not confirm_risk:
            raise ServiceValidationError(
                "probe_xbox_mobile_overrides reads each child's Family Safety "
                "device-limits override state from Microsoft. Call it again with "
                "confirm_risk: true to run it."
            )
        web = self.web_api
        if web is None:
            raise HomeAssistantError("The Microsoft API is not ready yet.")
        children: list[dict[str, Any]] = []
        for account_id, data in self._child_accounts().items():
            entry: dict[str, Any] = {
                "account_id": str(account_id),
                "first_name": str(data.get("first_name") or ""),
            }
            try:
                payload = await web.get_mobile_device_overrides(account_id)
                summary = web.summarize_mobile_overrides(payload)
                entry["overrides_read"] = True
                entry["lockable_platforms"] = summary["lockable_platform_names"]
                entry["xbox_present"] = self._xbox_present_in(summary)
                entry["xbox"] = (summary.get("platforms") or {}).get("Xbox")
                if raw_shape:
                    # Key names only (no values, no ids) to confirm the shape.
                    entry["xbox_shape"] = web.override_shape(payload, "Xbox")
            except Exception as err:  # noqa: BLE001 - reported per child, probe goes on
                entry["overrides_read"] = False
                entry["error"] = type(err).__name__
            try:
                # Reuse the xbox_mobile_lock resolution: a separate Xbox schedule.
                await web.get_mobile_xbox_day(account_id, 0)
                entry["xbox_schedule_present"] = True
            except Exception as err:  # noqa: BLE001 - informative only
                entry["xbox_schedule_present"] = False
                entry["xbox_schedule_note"] = type(err).__name__
            children.append(entry)
        return {
            "entry_id": self.entry.entry_id,
            "override_option_enabled": bool(self._xbox_mobile_override),
            "endpoint": "/v4/devicelimits/{child}/overrides",
            "children": children,
        }

    async def async_test_xbox_mobile_override(
        self,
        confirm_risk: bool = False,
        child: str | None = None,
        dry: bool = False,
    ) -> dict[str, Any]:
        """Bounded, reversible live self-test of the mobile override write path.

        With ``dry: true`` it only reads (like the probe) and reports, per
        candidate child, whether the Xbox is present and whether it already has
        an active hold -- so the HA session can pick a safe child.

        Otherwise, for one child whose Xbox has **no** active hold: set a
        BlockUntil override with a short ``validUntil`` (~2-3 min), read it back
        to confirm it applied, then Cancel it and read back to confirm it is
        gone. It always clears what it set (even on error), never leaves the
        Xbox blocked, and returns a concise non-secret trace. If the child's
        Xbox already has an active hold, it aborts and changes nothing.
        """
        if not confirm_risk:
            raise ServiceValidationError(
                "test_xbox_mobile_override writes a short, self-clearing Xbox "
                "block to confirm the mobile override path live. Call it again "
                "with confirm_risk: true to run it."
            )
        web = self.web_api
        if web is None:
            raise HomeAssistantError("The Microsoft API is not ready yet.")

        if dry:
            ids = (
                [self._resolve_child_account(child)]
                if child is not None
                else list(self._child_accounts())
            )
            candidates: list[dict[str, Any]] = []
            for account_id in ids:
                data = self._child_accounts().get(account_id, {})
                row: dict[str, Any] = {
                    "account_id": str(account_id),
                    "first_name": str(data.get("first_name") or ""),
                }
                try:
                    summary = web.summarize_mobile_overrides(
                        await web.get_mobile_device_overrides(account_id), "Xbox"
                    )
                    row["xbox_present"] = self._xbox_present_in(summary)
                    row["xbox_active_hold"] = self._xbox_blocked_in(summary)
                    row["safe_to_test"] = (
                        row["xbox_present"] and not row["xbox_active_hold"]
                    )
                except Exception as err:  # noqa: BLE001 - reported per child
                    row["error"] = type(err).__name__
                    row["safe_to_test"] = False
                candidates.append(row)
            return {"dry": True, "entry_id": self.entry.entry_id, "candidates": candidates}

        if child is None:
            raise ServiceValidationError(
                "Give child (a first name or account id) to test. Run it with "
                "dry: true first to pick a child whose Xbox has no active hold."
            )
        account_id = self._resolve_child_account(child)
        trace: list[dict[str, Any]] = []
        result: dict[str, Any] = {
            "entry_id": self.entry.entry_id,
            "account_id": str(account_id),
            "trace": trace,
        }

        before = web.summarize_mobile_overrides(
            await web.get_mobile_device_overrides(account_id), "Xbox"
        )
        trace.append({"step": "read_before", "xbox": (before.get("platforms") or {}).get("Xbox")})
        if not self._xbox_present_in(before):
            raise ServiceValidationError(
                f"Child {child!r} has no Xbox in device limits; nothing to test."
            )
        if self._xbox_blocked_in(before):
            result["aborted"] = True
            result["reason"] = "the Xbox already has an active hold; left untouched"
            _LOGGER.info(
                "test_xbox_mobile_override: %s already has an Xbox hold; aborting",
                account_id,
            )
            return result

        target = dt_util.utcnow() + timedelta(seconds=_SELF_TEST_SECONDS)
        wrote = False
        cleanup_error: Exception | None = None
        try:
            wrote = True  # set before the POST: cleanup runs even on a half-apply
            reply = await web.set_mobile_device_override(
                account_id, "Xbox", MOBILE_OVERRIDE_BLOCK, target
            )
            blocked_now = self._xbox_blocked_in(
                web.summarize_mobile_overrides(reply, "Xbox")
            )
            trace.append({
                "step": "block",
                "valid_for_seconds": _SELF_TEST_SECONDS,
                "reply_shows_block": blocked_now,
            })
            mid = web.summarize_mobile_overrides(
                await web.get_mobile_device_overrides(account_id), "Xbox"
            )
            confirmed = self._xbox_blocked_in(mid)
            trace.append({
                "step": "read_after_block",
                "xbox": (mid.get("platforms") or {}).get("Xbox"),
                "block_confirmed": confirmed,
            })
            result["block_confirmed"] = confirmed
        except Exception as err:  # noqa: BLE001 - recorded; cleanup still runs
            _LOGGER.warning("test_xbox_mobile_override block step failed: %s", err)
            trace.append({"step": "block", "error": type(err).__name__})
            result["block_confirmed"] = False
        finally:
            if wrote:
                try:
                    await web.set_mobile_device_override(
                        account_id, "Xbox", MOBILE_OVERRIDE_CANCEL, dt_util.utcnow()
                    )
                    after = web.summarize_mobile_overrides(
                        await web.get_mobile_device_overrides(account_id), "Xbox"
                    )
                    cleared = not self._xbox_blocked_in(after)
                    trace.append({
                        "step": "clear",
                        "xbox": (after.get("platforms") or {}).get("Xbox"),
                        "clear_confirmed": cleared,
                    })
                    result["left_blocked"] = not cleared
                except Exception as err:  # noqa: BLE001 - surfaced after finally
                    cleanup_error = err
                    result["left_blocked"] = True
                    trace.append({"step": "clear", "error": type(err).__name__})
                    _LOGGER.error(
                        "test_xbox_mobile_override could not clear the Xbox override "
                        "it set for %s: %s", account_id, err,
                    )
        if cleanup_error is not None:
            raise HomeAssistantError(
                "The Xbox mobile-override self-test set a short block but could "
                f"not confirm it was cleared ({type(cleanup_error).__name__}); the "
                "Xbox may still be blocked for up to "
                f"{_SELF_TEST_SECONDS // 60} min until it expires on its own. "
                "Re-run the probe to check."
            )
        await self.async_request_refresh()
        return result
