"""The Xbox lock and extra time through the Xbox Family Settings API (xapprove).

With the option ``xbox_xapprove`` on, an Xbox ``lock_platform``,
``unlock_platform``, the Xbox lock switch, ``add_time_bonus`` and
``cancel_time_bonus`` go through xapprove first (xapprove.py), on an Xbox Live
token taken from the integration's own stored sign-in. That needs no Family
web session, so it keeps working while the web session waits for its daily
sign-in. Whenever xapprove cannot be used (no Xbox user id for the child, a
network error, an answer that does not confirm the write, or Microsoft
refusing the token), the call falls through to the web override and the
mobile-schedule fallback exactly as before.

The write rules follow the Xbox Family Settings app:

* an allow and a block never stand together: when one of **ours** is in
  force, it is cancelled (type 100) before the other is created, and the
  reply's ``overrides`` must show the new one, or the call falls back;
* ours of the same type is also cancelled first and then created again (the
  safe order); a lock already in force until the time asked, or later, is
  left as it is with no write at all;
* an override that is **not ours** (a parent's pause or extra time from the
  Xbox Family Settings or Family Safety app, or one that replaced ours) is
  never cancelled to put ours in: the lock answers ``left_parent_override``
  and the hold state reads ``parent``, extra time is refused with a clear
  error.

"Ours" means: a block whose ``validUntil`` is within 2 minutes of the end of
the hold this integration recorded (any non-mobile hold: the web lock writes
the same override), or an allow within 2 minutes of the extra time
add_time_bonus recorded.

When Microsoft refuses our token (401/403 on a token step or on xapprove,
after one retry with fresh tokens) xapprove is left alone for 6 hours, a
Repairs issue says so, and the other paths are used meanwhile.

This module is a mixin of the coordinator.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
import copy
from datetime import datetime
import logging
import re
import time
from typing import Any

from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    CONF_XBOX_XAPPROVE,
    CONF_XBOX_XUIDS,
    DEFAULT_XBOX_XAPPROVE,
    DOMAIN,
    LOCK_PATH_XAPPROVE,
    XAPPROVE_REFUSED_ISSUE,
    XBOX_SIGNIN_EXPIRED_ISSUE,
    XBOX_SIGNIN_STORAGE_KEY,
)
from .screen_time import (
    ALLOW_UNTIL,
    BLOCK_UNTIL,
    NO_OVERRIDE,
    UNKNOWN,
    active_until,
    iso_utc,
    override as make_override,
    parse_utc,
)
from .xapprove import (
    DEFAULT_METHOD,
    METHODS_BY_NAME,
    OVERRIDE_ALLOW,
    OVERRIDE_BLOCK,
    PROBE_METHODS,
    SISU_METHOD,
    SISU_SCHEMES,
    AuthMethod,
    XapproveClient,
    XapproveError,
    XapproveRefused,
    XapproveUnavailable,
    is_adult,
    match_children,
    member_roles,
    member_xuid,
    overrides_url,
)
from .xbox_sisu import XboxSignInExpired, XboxSisuAuth

_LOGGER = logging.getLogger(__name__)

#: How long xapprove is left alone after Microsoft refused our token.
_REFUSED_BACKOFF = 6 * 3600
#: How often an unresolved child's Xbox user id is looked up again.
_LOOKUP_BACKOFF = 3600

_XUID_PAIR = re.compile(r"\s*([^=:,;\n]+?)\s*[=:]\s*(\d{6,20})\s*")


def parse_xuid_option(text: Any) -> dict[str, str]:
    """``"Alex=2533..., 1234567890123456: 2533..."`` -> {casefolded key: xuid}."""
    found: dict[str, str] = {}
    for part in re.split(r"[,;\n]", str(text or "")):
        match = _XUID_PAIR.fullmatch(part)
        if match:
            found[match.group(1).casefold()] = match.group(2)
    return found


def _plain(state: dict[str, Any] | None) -> dict[str, Any] | None:
    """An xapprove state as the integration's override record."""
    if not isinstance(state, dict):
        return None
    return {"type": state.get("type"), "valid_until": state.get("valid_until")}


class XboxXapproveMixin:
    """Coordinator part: the Xbox through xapprove, before the other paths."""

    def _init_xapprove(self, entry) -> None:
        self._xapprove_enabled = bool(
            entry.options.get(CONF_XBOX_XAPPROVE, DEFAULT_XBOX_XAPPROVE)
        )
        self._xuid_option = parse_xuid_option(entry.options.get(CONF_XBOX_XUIDS))
        #: account id -> (xuid, how it was found). Memory only, never logged.
        self._xuids: dict[str, tuple[str, str]] = {}
        self._xuid_lookup_after = 0.0
        self._xapprove_refused_until = 0.0
        self._xapprove_refusal: dict[str, Any] | None = None
        self._xapprove: XapproveClient | None = None
        #: account id -> names, from the poll in progress (self.data is not
        #: set yet on the first poll).
        self._xapprove_roster: dict[str, dict[str, Any]] = {}
        #: The way of signing xapprove requests that probe_xapprove found to
        #: work (xapprove.PROBE_METHODS), kept across restarts.
        self._xapprove_method_store: Store = Store(
            self.hass, 1, f"{DOMAIN}.xapprove.{entry.entry_id}"
        )
        self._xapprove_method: AuthMethod = DEFAULT_METHOD
        self._xapprove_method_loaded = False
        #: The Xbox Family Settings sign-in (options flow), if there is one.
        self._xbox_signin_store: Store = Store(
            self.hass, 1, f"{XBOX_SIGNIN_STORAGE_KEY}.{entry.entry_id}"
        )
        self._sisu: XboxSisuAuth | None = None

    # ------------------------------------------------------------------
    # Client, token source, refusal
    # ------------------------------------------------------------------

    @asynccontextmanager
    async def _xapprove_refresh_token(self):
        """The stored refresh token, with the Family token refresh held off."""
        if self.api is None:
            raise XapproveUnavailable("The Microsoft API is not ready yet.")
        authenticator = self.api.api.authenticator
        async with authenticator._login_lock:
            token = authenticator.refresh_token
            if not token:
                raise XapproveUnavailable("No mobile refresh token is stored.")
            yield token

    def _family_access_token(self) -> str | None:
        if self.api is None:
            return None
        return getattr(self.api.api.authenticator, "access_token", None)

    def _xapprove_client(self) -> XapproveClient:
        if self._xapprove is None:
            self._xapprove = XapproveClient(
                lambda: async_get_clientsession(self.hass),
                self._xapprove_refresh_token,
                method=self._xapprove_method,
                family_token_getter=self._family_access_token,
                sisu=self._sisu,
            )
        return self._xapprove

    @property
    def xbox_signed_in(self) -> bool:
        return self._sisu is not None

    async def _async_save_xbox_signin(self, credential: dict[str, Any]) -> None:
        """Microsoft rotated the Xbox sign-in's refresh token: keep the new one."""
        await self._xbox_signin_store.async_save(credential)

    async def _async_load_xapprove_method(self) -> None:
        """Before the first xapprove call: the Xbox sign-in and the saved way."""
        if self._xapprove_method_loaded:
            return
        self._xapprove_method_loaded = True
        credential = await self._xbox_signin_store.async_load()
        if isinstance(credential, dict) and credential.get("refresh_token") and credential.get("proof_key"):
            try:
                self._sisu = XboxSisuAuth(
                    lambda: async_get_clientsession(self.hass),
                    credential,
                    self._async_save_xbox_signin,
                )
            except ValueError as err:
                _LOGGER.warning("The stored Xbox Family Settings sign-in is unreadable (%s)", type(err).__name__)
        data = await self._xapprove_method_store.async_load()
        name = data.get("method") if isinstance(data, dict) else None
        method = METHODS_BY_NAME.get(name) if name else None
        if method is not None and method.scheme in SISU_SCHEMES and self._sisu is None:
            method = None  # signed out since
        self._xapprove_method = method or (SISU_METHOD if self._sisu is not None else DEFAULT_METHOD)
        if self._xapprove is not None:
            self._xapprove.method = self._xapprove_method
            self._xapprove.sisu = self._sisu

    async def _async_save_xapprove_method(self, method: AuthMethod) -> None:
        self._xapprove_method = method
        self._xapprove_method_loaded = True
        if self._xapprove is not None:
            self._xapprove.method = method
        await self._xapprove_method_store.async_save(
            {"method": method.name, "found": iso_utc(dt_util.utcnow())}
        )

    @property
    def xapprove_enabled(self) -> bool:
        return self._xapprove_enabled and not self._mobile_only

    def _xapprove_usable(self) -> bool:
        return self.xapprove_enabled and time.monotonic() >= self._xapprove_refused_until

    def _xapprove_issue_id(self) -> str:
        return f"{XAPPROVE_REFUSED_ISSUE}_{self.entry.entry_id}"

    def _xapprove_mark_refused(self, err: XapproveRefused) -> None:
        if self._xapprove_refusal is None:
            _LOGGER.warning(
                "The Xbox Family Settings API (xapprove) refused the integration's Xbox "
                "token (%s). The Xbox lock uses the Family web session and today's Xbox "
                "schedule instead; xapprove is tried again in 6 hours.", err,
            )
        self._xapprove_refusal = err.facts()
        self._xapprove_refused_until = time.monotonic() + _REFUSED_BACKOFF
        if isinstance(err, XboxSignInExpired):
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                f"{XBOX_SIGNIN_EXPIRED_ISSUE}_{self.entry.entry_id}",
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=XBOX_SIGNIN_EXPIRED_ISSUE,
                translation_placeholders={"title": self.entry.title or DOMAIN},
            )
        if self._xapprove is not None:
            self._xapprove.forget_tokens()
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            self._xapprove_issue_id(),
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=XAPPROVE_REFUSED_ISSUE,
            translation_placeholders={
                "title": self.entry.title or DOMAIN,
                "error": str(err)[:200],
            },
        )

    def _xapprove_mark_ok(self) -> None:
        if self._xapprove_refusal is not None:
            _LOGGER.info("The Xbox Family Settings API (xapprove) accepts the Xbox token again")
            self._xapprove_refusal = None
        ir.async_delete_issue(self.hass, DOMAIN, self._xapprove_issue_id())
        ir.async_delete_issue(
            self.hass, DOMAIN, f"{XBOX_SIGNIN_EXPIRED_ISSUE}_{self.entry.entry_id}"
        )

    # ------------------------------------------------------------------
    # The child's Xbox user id
    # ------------------------------------------------------------------

    def _children(self) -> dict[str, dict[str, Any]]:
        accounts = (self.data or {}).get("accounts") or {}
        children = {
            str(k): {"first_name": v.get("first_name"), "surname": v.get("surname")}
            for k, v in accounts.items() if isinstance(v, dict)
        }
        return {**children, **self._xapprove_roster}

    def _option_xuid(self, account_id: str) -> str | None:
        if not self._xuid_option:
            return None
        if account_id.casefold() in self._xuid_option:
            return self._xuid_option[account_id.casefold()]
        name = str(self._children().get(account_id, {}).get("first_name") or "").casefold()
        return self._xuid_option.get(name) if name else None

    async def _async_lookup_xuids(self) -> None:
        """Find the children's Xbox user ids in the parent's Xbox family."""
        self._xuid_lookup_after = time.monotonic() + _LOOKUP_BACKOFF
        children = {
            k: v for k, v in self._children().items()
            if k not in self._xuids and self._option_xuid(k) is None
        }
        if not children:
            return
        client = self._xapprove_client()
        # Token trouble (a refusal above all) is reported as such; only the
        # family read itself failing means "no Xbox user id".
        await client.async_tokens()
        try:
            members = await client.async_family_members()
        except XapproveError as err:
            if isinstance(err, XapproveRefused) and err.step != "family":
                raise
            raise XapproveUnavailable(f"The Xbox family could not be read ({err}).") from err
        if not members:
            return
        exclude = {client.parent_xuid} if client.parent_xuid else set()
        for account_id, found in match_children(members, children, exclude).items():
            self._xuids[account_id] = found

    async def _async_xapprove_xuid(self, account_id: Any) -> str:
        await self._async_load_xapprove_method()
        account_id = str(account_id)
        option = self._option_xuid(account_id)
        if option is not None:
            return option
        if account_id not in self._xuids and time.monotonic() >= self._xuid_lookup_after:
            await self._async_lookup_xuids()
        if account_id in self._xuids:
            return self._xuids[account_id][0]
        raise XapproveUnavailable("No Xbox user id is known for this child.")

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    def xapprove_state(self, account_id: Any) -> dict[str, Any] | None:
        """The Xbox override xapprove last read for this child (no xuid in it)."""
        account = self.account_data(account_id) or {}
        state = account.get("xbox_xapprove")
        return state if isinstance(state, dict) else None

    def _xbox_current_override(self, account: dict[str, Any]) -> dict[str, Any] | None:
        """The Xbox override in force: xapprove's read first, else the web policy."""
        state = account.get("xbox_xapprove")
        if isinstance(state, dict):
            return _plain(state)
        return None

    def _apply_xapprove_lock_state(
        self, account_id: str, account_data: dict[str, Any], state: dict[str, Any]
    ) -> None:
        key = (str(account_id), "xbox")
        until = active_until(state) if state.get("type") == BLOCK_UNTIL else None
        if until is not None:
            self._platform_override_until[key] = until
        else:
            self._platform_override_until.pop(key, None)
        self._xbox_lock_known.add(key[0])
        blocked = [p for p in (account_data.get("blocked_platforms") or []) if p != "Xbox"]
        if until is not None:
            blocked.append("Xbox")
        account_data["blocked_platforms"] = blocked
        account_data["xbox_lock_known"] = True

    def _set_xapprove_state(
        self, account_id: Any, state: dict[str, Any] | None, read_at: datetime | None = None
    ) -> None:
        """Keep what xapprove answered on the account data, and the lock state with it."""
        if not isinstance(state, dict):
            return
        account = self.account_data(account_id)
        if account is None:
            return
        account["xbox_xapprove"] = {
            "type": state.get("type"),
            "valid_until": state.get("valid_until"),
            "raw_type": state.get("raw_type"),
            "read_at": iso_utc(read_at or dt_util.utcnow()),
        }
        self._apply_xapprove_lock_state(str(account_id), account, state)

    def _bonus_is_ours(self, key: str, current: dict[str, Any] | None) -> bool:
        bonus = self._bonuses.get(key)
        if bonus is None or not isinstance(current, dict) or current.get("type") != ALLOW_UNTIL:
            return False
        theirs = parse_utc(current.get("valid_until"))
        ours = parse_utc(bonus.get("until"))
        return (
            theirs is not None and ours is not None
            and abs(theirs - ours) <= self._OWN_LOCK_TOLERANCE
        )

    def _drop_unconfirmed_xapprove_hold(self, key: str) -> bool:
        hold = self._platform_holds.get(key)
        if hold is not None and hold.get("path") == LOCK_PATH_XAPPROVE and not hold.get("confirmed"):
            self._platform_holds.pop(key, None)
            return True
        return False

    # ------------------------------------------------------------------
    # One write: cancel ours first, then create, confirmed by the reply
    # ------------------------------------------------------------------

    async def _async_xapprove_put(
        self, xuid: str, code: int, until: datetime, current: dict[str, Any]
    ) -> dict[str, Any]:
        """Replace OUR override (or nothing) with ``code`` until ``until``.

        The caller has checked that anything in force is ours.
        """
        client = self._xapprove_client()
        want = BLOCK_UNTIL if code == OVERRIDE_BLOCK else ALLOW_UNTIL
        async with client.write_lock(xuid):
            if current.get("type") != NO_OVERRIDE:
                after = await client.async_cancel(xuid)
                if after is None:
                    after = await client.async_get_overrides(xuid)
                if after.get("type") != NO_OVERRIDE:
                    raise XapproveError(
                        f"xapprove: the cancel before the new override read back {after.get('type')}"
                    )
            reply = await client.async_create(xuid, code, until)
            if reply is None:
                reply = await client.async_get_overrides(xuid)
        got = parse_utc(reply.get("valid_until"))
        if (
            reply.get("type") != want
            or got is None
            or abs(got - dt_util.as_utc(until)) > self._OWN_LOCK_TOLERANCE
        ):
            raise XapproveError(
                f"xapprove: sent {want} until {iso_utc(until)}, read back "
                f"{reply.get('type')} until {reply.get('valid_until')}"
            )
        return reply

    async def _async_xapprove_cancel(self, xuid: str) -> dict[str, Any]:
        client = self._xapprove_client()
        async with client.write_lock(xuid):
            after = await client.async_cancel(xuid)
            if after is None:
                after = await client.async_get_overrides(xuid)
        if after.get("type") != NO_OVERRIDE:
            raise XapproveError(f"xapprove: the cancel read back {after.get('type')}")
        return after

    def _xapprove_fell_back(self, action: str, err: Exception, key: str | None = None) -> None:
        if isinstance(err, XapproveRefused):
            self._xapprove_mark_refused(err)
        elif isinstance(err, XapproveUnavailable):
            _LOGGER.debug("Xbox %s: xapprove not used (%s)", action, err)
        else:
            _LOGGER.info("Xbox %s through xapprove failed (%s); using the other paths", action, err)
        if key is not None:
            self._drop_unconfirmed_xapprove_hold(key)

    # ------------------------------------------------------------------
    # Lock, unlock, release
    # ------------------------------------------------------------------

    async def _async_xapprove_lock(
        self, account_id: str, requested: datetime, key: str
    ) -> dict[str, Any] | None:
        """lock_platform on Xbox through xapprove; None = use the other paths.

        The caller holds the hold lock, with the holds loaded. On a fall back
        the hold record is left as it was before the call.
        """
        before: dict[str, Any] | None = None
        try:
            xuid = await self._async_xapprove_xuid(account_id)
            client = self._xapprove_client()
            await self._async_load_bonuses()
            now = dt_util.utcnow()
            hold = self._platform_holds.get(key)
            if self._is_mobile_hold(hold):
                # Our schedule lock goes back before xapprove takes over; if it
                # cannot, the schedule path stays in charge.
                try:
                    await self._async_put_back_mobile_day(account_id, hold)
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug("Xbox schedule lock of %s not put back: %r", account_id, err)
                    return None
                self._platform_holds.pop(key, None)
                await self._async_save_platform_holds()
                self._cancel_mobile_restore(key)
                self._clear_not_restored_issue(key)
                hold = None
            elif hold is not None and not self._hold_in_force(hold, now):
                self._platform_holds.pop(key, None)
                hold = None
            before = copy.deepcopy(hold)
            read_at = dt_util.utcnow()
            current = await client.async_get_overrides(xuid)
            ours_block = hold is not None and self._is_own_lock(hold, current)
            ours_allow = self._bonus_is_ours(key, current)
            if current["type"] != NO_OVERRIDE and not (ours_block or ours_allow):
                # A parent's pause or extra time: theirs stands, nothing is written.
                _LOGGER.info(
                    "Xbox of %s has an override that is not ours (%s); not locking over it",
                    account_id, current["type"],
                )
                if self._platform_holds.pop(key, None) is not None:
                    await self._async_save_platform_holds()
                self._set_xapprove_state(account_id, current, read_at)
                self._xapprove_mark_ok()
                return {
                    "prior": _plain(current),
                    "applied": "left_parent_override",
                    "lock_path": LOCK_PATH_XAPPROVE,
                    "current": self._override_details(_plain(current)),
                }
            target = dt_util.as_utc(requested)
            already = False
            if ours_block:
                in_force = parse_utc(current.get("valid_until"))
                if in_force is not None and in_force >= target - self._OWN_LOCK_TOLERANCE:
                    # Ours already holds until then or later: never shortened, no write.
                    target, already = in_force, True
            if hold is None:
                hold = {
                    "account_id": str(account_id),
                    "platform": "Xbox",
                    "prior": _plain(current) or make_override(UNKNOWN),
                    "until": None,
                    "confirmed": False,
                    "created": iso_utc(now),
                }
                self._platform_holds[key] = hold
            hold["path"] = LOCK_PATH_XAPPROVE
            hold["until"] = iso_utc(target)
            # Recorded before the write, so a retry knows the block it reads is ours.
            await self._async_save_platform_holds()
            reply = current if already else await self._async_xapprove_put(
                xuid, OVERRIDE_BLOCK, target, current
            )
            if ours_allow and self._bonuses.pop(key, None) is not None:
                await self._bonus_store.async_save(self._bonuses)
            hold["confirmed"] = True
            hold["confirmed_at"] = iso_utc(dt_util.utcnow())
            await self._async_save_platform_holds()
        except XapproveError as err:
            self._xapprove_fell_back("lock", err, key)
            if before is not None:
                self._platform_holds[key] = before
            await self._async_save_platform_holds()
            return None
        self._set_xapprove_state(account_id, reply)
        self._xapprove_mark_ok()
        await self.async_request_refresh()
        return {
            "prior": dict(hold["prior"]),
            "applied": make_override(BLOCK_UNTIL, target),
            "lock_path": LOCK_PATH_XAPPROVE,
        }

    async def _async_xapprove_unlock(
        self, account_id: str, key: str, hold: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        """unlock_platform on Xbox through xapprove; None = use the other paths.

        Only our own block is cancelled. The caller holds the hold lock.
        """
        try:
            xuid = await self._async_xapprove_xuid(account_id)
            prior = dict(hold["prior"]) if hold and isinstance(hold.get("prior"), dict) else None
            if hold is not None and hold.get("until") and not self._hold_in_force(hold, dt_util.utcnow()):
                self._platform_holds.pop(key, None)
                await self._async_save_platform_holds()
                self._platform_override_until.pop((str(account_id), "xbox"), None)
                return {"prior": prior, "applied": "hold_expired", "lock_path": LOCK_PATH_XAPPROVE}
            read_at = dt_util.utcnow()
            current = await self._xapprove_client().async_get_overrides(xuid)
            if hold is None or not self._is_own_lock(hold, current):
                # No hold of ours: nothing to undo, whatever is in force stays.
                # Ours replaced or cancelled by a parent: theirs stands.
                applied = (
                    "left_parent_override"
                    if hold is not None and hold.get("confirmed")
                    else "not_locked"
                )
                if hold is not None:
                    _LOGGER.info(
                        "Xbox of %s was changed since our lock (%s); left as it is",
                        account_id, current["type"],
                    )
                    self._platform_holds.pop(key, None)
                    await self._async_save_platform_holds()
                self._set_xapprove_state(account_id, current, read_at)
                self._xapprove_mark_ok()
                return {
                    "prior": prior,
                    "applied": applied,
                    "lock_path": LOCK_PATH_XAPPROVE,
                    "current": self._override_details(_plain(current)),
                }
            restore = active_until(prior)
            if restore is not None and prior.get("type") == BLOCK_UNTIL:
                # A web lock of ours had extended a parent's earlier block: it comes back.
                after = await self._async_xapprove_put(xuid, OVERRIDE_BLOCK, restore, current)
                applied: Any = make_override(BLOCK_UNTIL, restore)
            else:
                after = await self._async_xapprove_cancel(xuid)
                applied = make_override(NO_OVERRIDE)
            self._platform_holds.pop(key, None)
            await self._async_save_platform_holds()
        except XapproveError as err:
            self._xapprove_fell_back("unlock", err)
            return None
        self._set_xapprove_state(account_id, after)
        self._xapprove_mark_ok()
        await self.async_request_refresh()
        return {"prior": prior, "applied": applied, "lock_path": LOCK_PATH_XAPPROVE}

    async def _async_xapprove_release(self, account_id: str, key: str) -> bool:
        """The lock switch's off: cancel whatever is in force. False = other paths."""
        try:
            xuid = await self._async_xapprove_xuid(account_id)
            current = await self._xapprove_client().async_get_overrides(xuid)
            after = current
            if current["type"] != NO_OVERRIDE:
                after = await self._async_xapprove_cancel(xuid)
        except XapproveError as err:
            self._xapprove_fell_back("release", err)
            return False
        if self._platform_holds.pop(key, None) is not None:
            await self._async_save_platform_holds()
        self._set_xapprove_state(account_id, after)
        self._xapprove_mark_ok()
        await self.async_request_refresh()
        return True

    # ------------------------------------------------------------------
    # Extra time
    # ------------------------------------------------------------------

    async def _async_xapprove_bonus(
        self, account_id: str, until: datetime, key: str
    ) -> dict[str, Any] | None:
        """add_time_bonus on Xbox: an allow until ``until``. None = other paths.

        The caller holds the hold lock, holds and bonuses loaded. A parent's
        override in force is not replaced: HomeAssistantError.
        """
        try:
            xuid = await self._async_xapprove_xuid(account_id)
            hold = self._platform_holds.get(key)
            current = await self._xapprove_client().async_get_overrides(xuid)
            ours = (
                hold is not None and not self._is_mobile_hold(hold)
                and self._is_own_lock(hold, current)
            ) or self._bonus_is_ours(key, current)
            if current["type"] != NO_OVERRIDE and not ours:
                self._set_xapprove_state(account_id, current)
                details = self._override_details(_plain(current))
                raise HomeAssistantError(
                    f"A parent's Xbox override ({current['type']} until "
                    f"{details['override_until']}) is in force; it is left as it is and "
                    "no extra time was given."
                )
            reply = await self._async_xapprove_put(xuid, OVERRIDE_ALLOW, until, current)
        except XapproveError as err:
            self._xapprove_fell_back("extra time", err)
            return None
        released = hold is not None
        if self._is_mobile_hold(hold):
            try:
                await self._async_put_back_mobile_day(account_id, hold)
                self._cancel_mobile_restore(key)
            except Exception as err:  # noqa: BLE001 - put back by its end anyway
                _LOGGER.warning(
                    "Could not put the Xbox schedule of %s back after extra time: %s",
                    account_id, err,
                )
                released = False
        if released:
            self._platform_holds.pop(key, None)
            await self._async_save_platform_holds()
        self._bonuses[key] = {
            "account_id": str(account_id), "platform": "Xbox", "until": iso_utc(until),
        }
        await self._bonus_store.async_save(self._bonuses)
        self._set_xapprove_state(account_id, reply)
        self._xapprove_mark_ok()
        await self.async_request_refresh()
        return {
            "platform": "Xbox",
            "applied_until": dt_util.as_local(until).isoformat(),
            "read_back": self._override_details(_plain(reply)),
            "released_hold": released,
            "lock_path": LOCK_PATH_XAPPROVE,
        }

    async def _async_xapprove_cancel_bonus(
        self, account_id: str, key: str
    ) -> dict[str, Any] | None:
        """cancel_time_bonus on Xbox: cancel extra time in force, never a block."""
        try:
            xuid = await self._async_xapprove_xuid(account_id)
            current = await self._xapprove_client().async_get_overrides(xuid)
            if current["type"] != ALLOW_UNTIL:
                if self._bonuses.pop(key, None) is not None:
                    await self._bonus_store.async_save(self._bonuses)
                self._set_xapprove_state(account_id, current)
                return {
                    "platform": "Xbox",
                    "cancelled": False,
                    "reason": f"no extra time in force ({current['type']})",
                    "read_back": self._override_details(_plain(current)),
                    "lock_path": LOCK_PATH_XAPPROVE,
                }
            ours = self._bonus_is_ours(key, current)
            after = await self._async_xapprove_cancel(xuid)
        except XapproveError as err:
            self._xapprove_fell_back("extra time cancel", err)
            return None
        if self._bonuses.pop(key, None) is not None:
            await self._bonus_store.async_save(self._bonuses)
        self._set_xapprove_state(account_id, after)
        self._xapprove_mark_ok()
        await self.async_request_refresh()
        return {
            "platform": "Xbox",
            "cancelled": True,
            "ours": ours,
            "read_back": self._override_details(_plain(current)),
            "lock_path": LOCK_PATH_XAPPROVE,
        }

    async def _async_xapprove_reassert(self, account_id: str, until: datetime) -> bool:
        """Strict mode: place our block again (nothing is in force). False = other paths."""
        try:
            xuid = await self._async_xapprove_xuid(account_id)
            current = await self._xapprove_client().async_get_overrides(xuid)
            if current["type"] != NO_OVERRIDE:
                return True  # something is in force again: nothing to put back
            reply = await self._async_xapprove_put(xuid, OVERRIDE_BLOCK, until, current)
        except XapproveError as err:
            self._xapprove_fell_back("strict mode lock", err)
            return False
        self._set_xapprove_state(account_id, reply)
        return True

    # ------------------------------------------------------------------
    # Poll
    # ------------------------------------------------------------------

    async def _async_poll_xapprove(
        self,
        accounts_data: dict[str, Any],
        xbox_overrides: dict[str, Any],
        xbox_read_at: dict[str, datetime],
    ) -> None:
        """Read each child's Xbox override through xapprove; it wins over the web read."""
        if not self._xapprove_usable():
            return
        self._xapprove_roster = {
            str(k): {"first_name": v.get("first_name"), "surname": v.get("surname")}
            for k, v in accounts_data.items() if isinstance(v, dict)
        }
        client = self._xapprove_client()
        for account_id, account_data in accounts_data.items():
            try:
                xuid = await self._async_xapprove_xuid(account_id)
                read_at = dt_util.utcnow()
                state = await client.async_get_overrides(xuid)
            except XapproveRefused as err:
                self._xapprove_mark_refused(err)
                return
            except XapproveError as err:
                _LOGGER.debug("Xbox override of %s not read through xapprove: %s", account_id, err)
                continue
            self._xapprove_mark_ok()
            account_data["xbox_xapprove"] = {
                "type": state["type"],
                "valid_until": state["valid_until"],
                "raw_type": state.get("raw_type"),
                "read_at": iso_utc(read_at),
            }
            self._apply_xapprove_lock_state(str(account_id), account_data, state)
            xbox_overrides[str(account_id)] = _plain(state)
            xbox_read_at[str(account_id)] = read_at

    # ------------------------------------------------------------------
    # Diagnostic
    # ------------------------------------------------------------------

    async def async_probe_xapprove(self, confirm_risk: bool = False) -> dict[str, Any]:
        """probe_xapprove: token steps, the child lookup, and read-only overrides GETs.

        The GET is tried with each way of signing it in xapprove.PROBE_METHODS
        until one answers 2xx; that one is saved and used by the integration
        from then on (behind the same option). Answers with non-secret facts
        only: no token, xuid or gamertag.
        """
        if not confirm_risk:
            raise ServiceValidationError(
                "probe_xapprove uses the stored Microsoft sign-in for the Xbox Live scope "
                "(and for the xapprove scope). Microsoft does not document whether that "
                "endpoint revokes the old refresh token on use; if it did, the integration "
                "would need a new sign-in. Call it again with confirm_risk: true to accept that."
            )
        if self.api is None:
            raise HomeAssistantError("The Microsoft API is not ready yet.")
        await self._async_load_xapprove_method()
        authenticator = self.api.api.authenticator
        session = async_get_clientsession(self.hass)
        steps: dict[str, Any] = {}
        result: dict[str, Any] = {
            "entry_id": self.entry.entry_id,
            "option_enabled": self._xapprove_enabled,
            "method_before": self._xapprove_method.name,
            "refused_earlier": dict(self._xapprove_refusal) if self._xapprove_refusal else None,
            "steps": steps,
        }
        # The whole probe runs with the Family token refresh held off, so the
        # stored token cannot rotate under it.
        async with authenticator._login_lock:
            stored = authenticator.refresh_token
            if not stored:
                raise HomeAssistantError("No mobile refresh token is stored.")

            @asynccontextmanager
            async def _stored():
                yield stored

            client = XapproveClient(
                lambda: session, _stored, family_token_getter=self._family_access_token,
                sisu=self._sisu,
            )
            client.token_facts = steps
            if self._sisu is not None:
                self._sisu.token_facts = steps
            result["xbox_signed_in"] = self._sisu is not None
            result["identity"] = (
                "xbox_family_settings" if self._sisu is not None else "family_safety_signin"
            )
            tokens = None
            try:
                tokens = await client.async_tokens()
            except XapproveError as err:
                if self._sisu is None:
                    result["token_error"] = str(err)
                else:
                    # The Xbox sign-in failed: say how, and look the family up
                    # with the Family Safety sign-in instead.
                    result["xbox_signin_error"] = (
                        err.facts() if isinstance(err, XapproveRefused) else {"error": str(err)}
                    )
                    if isinstance(err, XapproveRefused):
                        self._xapprove_mark_refused(err)
                    client.sisu = None
                    result["identity"] = "family_safety_signin"
                    try:
                        tokens = await client.async_tokens()
                    except XapproveError as err2:
                        result["token_error"] = str(err2)
            if tokens is not None:
                await self._async_probe_family_and_read(client, tokens, result)
            client.forget_tokens()
            if self._sisu is not None:
                self._sisu.token_facts = {}
            result["stored_refresh_token_unchanged"] = authenticator.refresh_token == stored
            stored = None
        try:
            await authenticator.perform_refresh()
        except Exception as err:  # noqa: BLE001 - reported, not raised
            result["family_refresh_after"] = {"ok": False, "error": type(err).__name__}
            _LOGGER.warning(
                "The Family token refresh after probe_xapprove failed (%s): the "
                "integration may need a new sign-in", type(err).__name__,
            )
        else:
            result["family_refresh_after"] = {"ok": True}
            await self._async_persist_current_auth()
        return result

    async def _async_probe_family_and_read(
        self, client: XapproveClient, tokens: Any, result: dict[str, Any]
    ) -> None:
        result["parent_xuid_claim_present"] = bool(tokens.parent_xuid)
        children = self._children()
        family: dict[str, Any] = {}
        matched: dict[str, tuple[str, str]] = {}
        try:
            members = await client.async_family_members()
        except XapproveRefused as err:
            family = {"ok": False, **err.facts()}
        except XapproveError as err:
            family = {"ok": False, "error": str(err)}
        else:
            if members is None:
                family = {"ok": False, "error": "no parent xuid claim or no member list"}
            else:
                keys: set[str] = set()
                roles: set[str] = set()
                for member in members:
                    keys.update(str(k) for k in member)
                    roles.update(member_roles(member))
                parent = tokens.parent_xuid
                family = {
                    "ok": True,
                    "members": len(members),
                    "members_with_xuid": sum(1 for m in members if member_xuid(m)),
                    "adults": sum(1 for m in members if is_adult(m)),
                    "signed_in_parent_listed": bool(
                        parent and any(member_xuid(m) == parent for m in members)
                    ),
                    "roles": sorted(roles),
                    "member_keys": sorted(keys),
                }
                matched = match_children(members, children, {parent} if parent else set())
        result["family"] = family
        resolved: dict[str, Any] = {}
        first: tuple[str, str] | None = None
        for account_id in children:
            option = self._option_xuid(account_id)
            xuid, via = (option, "option") if option else matched.get(account_id, (None, None))
            resolved[account_id] = {"resolved": xuid is not None, "via": via}
            if xuid is not None and first is None:
                first = (account_id, xuid)
        result["children"] = resolved
        if first is None:
            return
        url = overrides_url(first[1])
        # The family was read with the Family Safety sign-in if the Xbox one
        # failed; the xapprove attempts still try the Xbox one first.
        client.sisu = self._sisu
        attempts: list[dict[str, Any]] = []
        working: AuthMethod | None = None
        state: dict[str, Any] | None = None
        for method in PROBE_METHODS:
            if method.scheme in SISU_SCHEMES and self._sisu is None:
                continue  # no Xbox Family Settings sign-in: nothing to try
            facts = await client.async_try(method, url)
            state = facts.pop("state", None)
            attempts.append(facts)
            status = facts.get("status")
            if isinstance(status, int) and 200 <= status < 300:
                working = method
                break
        result["overrides_attempts"] = attempts
        result["working_method"] = working.name if working else None
        read: dict[str, Any] = {"account_id": first[0]}
        if working is None:
            base = attempts[0] if attempts else {}
            read.update({
                "ok": False,
                "status": base.get("status"),
                "xerr": base.get("xerr"),
                "error": base.get("error"),
            })
            result["method_saved"] = False
        else:
            read.update({
                "ok": True,
                "method": working.name,
                "readable": state is not None,
            })
            if state is not None:
                read.update({
                    "override_active": state["type"] != NO_OVERRIDE,
                    "type": state["type"],
                    "raw_type": state.get("raw_type"),
                    "valid_until": state["valid_until"],
                    "actions": state.get("actions"),
                })
            # The integration signs xapprove this way from now on.
            await self._async_save_xapprove_method(working)
            self._xapprove_refused_until = 0.0
            result["method_saved"] = True
        result["overrides_read"] = read
