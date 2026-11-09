"""The Xbox: lock and extra time through the Xbox Family Settings API (xapprove).

The Xbox never uses the Family web session. With the Xbox Family Settings
sign-in (xbox_sisu.py, options flow) and the option ``xbox_xapprove`` on, an
Xbox ``lock_platform``, ``unlock_platform``, the Xbox lock switch,
``add_time_bonus`` and ``cancel_time_bonus`` go through xapprove (xapprove.py).
Whenever xapprove cannot be used (no sign-in, no Xbox user id for the child, a
network error, an answer that does not confirm the write, or Microsoft
refusing the token), a lock falls back to today's Xbox schedule
(xbox_mobile_lock.py, mobile token); extra time has no fallback and fails
with a clear error.

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
the hold this integration recorded, or an allow within 2 minutes of the extra
time add_time_bonus recorded.

When Microsoft refuses our token (401/403 on a token step or on xapprove,
after one retry with fresh tokens) xapprove is left alone for 6 hours, a
Repairs issue says so, and the schedule fallback is used meanwhile.

This module is a mixin of the coordinator.
"""
from __future__ import annotations

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
    CONF_XBOX_SIGNED_IN_AT,
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
    OVERRIDE_ALLOW,
    OVERRIDE_BLOCK,
    PATH_OVERRIDES,
    PROBE_ATTEMPTS,
    AuthMethod,
    XapproveClient,
    XapproveError,
    XapproveRefused,
    XapproveUnavailable,
    is_adult,
    match_children,
    member_roles,
    member_xuid,
    method_by_name,
    overrides_url,
    redact,
    screentime_url,
)
from .xbox_sisu import XSTS_VARIANTS_BY_NAME, SignIn, XboxSignInExpired, XboxSisuAuth, xsts_key

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


async def _no_save(_credential: dict[str, Any]) -> None:
    """The in-flow check never refreshes, so never has a rotated token to save."""


class XboxXapproveMixin:
    """Coordinator part: the Xbox through xapprove, before the other paths."""

    def _init_xapprove(self, entry) -> None:
        self._xapprove_enabled = bool(
            entry.options.get(CONF_XBOX_XAPPROVE, DEFAULT_XBOX_XAPPROVE)
        )
        #: The options flow records when the Xbox sign-in was made; the
        #: credential itself is in its own Store (loaded on first use).
        self._xbox_signed_in_option = bool(entry.options.get(CONF_XBOX_SIGNED_IN_AT))
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

    def _xapprove_client(self) -> XapproveClient:
        if self._xapprove is None:
            self._xapprove = XapproveClient(
                lambda: async_get_clientsession(self.hass),
                method=self._xapprove_method,
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
        self._xapprove_method = method_by_name(name) or DEFAULT_METHOD
        variant = data.get("xsts_variant") if isinstance(data, dict) else None
        if self._sisu is not None and variant in XSTS_VARIANTS_BY_NAME:
            self._sisu.preferred_variant = variant
        if self._xapprove is not None:
            self._xapprove.method = self._xapprove_method
            self._xapprove.sisu = self._sisu

    async def _async_save_xapprove_method(self, method: AuthMethod) -> None:
        self._xapprove_method = method
        self._xapprove_method_loaded = True
        if self._xapprove is not None:
            self._xapprove.method = method
        saved: dict[str, Any] = {"method": method.name, "found": iso_utc(dt_util.utcnow())}
        variant = (
            self._sisu.xsts_variants.get(xsts_key(method.relying_party, method.sisu_profile))
            if self._sisu is not None and method.token == "xsts" and method.relying_party
            else None
        )
        if variant:
            # The variant of the XSTS step that worked, tried first from now on.
            saved["xsts_variant"] = variant
            self._sisu.preferred_variant = variant
        await self._xapprove_method_store.async_save(saved)

    @property
    def xapprove_enabled(self) -> bool:
        """The option on and an Xbox Family Settings sign-in made (any mode:
        the Xbox never needs the Family web session)."""
        return self._xapprove_enabled and self._xbox_signed_in_option

    def _xapprove_usable(self) -> bool:
        return self.xapprove_enabled and time.monotonic() >= self._xapprove_refused_until

    def _xapprove_issue_id(self) -> str:
        return f"{XAPPROVE_REFUSED_ISSUE}_{self.entry.entry_id}"

    def _xapprove_mark_refused(self, err: XapproveRefused) -> None:
        if self._xapprove_refusal is None:
            _LOGGER.warning(
                "The Xbox Family Settings API (xapprove) refused the integration's Xbox "
                "token (%s). The Xbox lock uses today's Xbox schedule instead; xapprove "
                "is tried again in 6 hours.", err,
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
        if self._sisu is None:
            raise XapproveUnavailable("No Xbox Family Settings sign-in is stored.")
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
        """The Xbox override in force, as xapprove last read it; None before a read."""
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
                # A hold saved by an older version (its web lock extended a parent's
                # earlier block): that block comes back. New holds never carry one.
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
    # The coordinator's Xbox entry points: xapprove, else today's schedule
    # ------------------------------------------------------------------

    async def _async_lock_xbox(
        self, account_id: str, requested: datetime, key: str
    ) -> dict[str, Any]:
        """lock_platform on Xbox: xapprove, else today's Xbox schedule."""
        if self._xapprove_usable():
            async with self._hold_lock(key):
                await self._async_load_platform_holds()
                result = await self._async_xapprove_lock(account_id, requested, key)
            if result is not None:
                return result
        async with self._hold_lock(key):
            await self._async_load_platform_holds()
            hold = self._platform_holds.get(key)
            now = dt_util.utcnow()
            if hold is not None and not self._is_mobile_hold(hold):
                until = parse_utc(hold.get("until"))
                if hold.get("confirmed") and until is not None and until > now:
                    # A lock of ours placed through xapprove still holds; it
                    # cannot be changed while xapprove is out of reach.
                    if until >= dt_util.as_utc(requested) - self._OWN_LOCK_TOLERANCE:
                        return {
                            "prior": dict(hold.get("prior") or make_override(UNKNOWN)),
                            "applied": make_override(BLOCK_UNTIL, until),
                            "lock_path": LOCK_PATH_XAPPROVE,
                        }
                    raise HomeAssistantError(
                        "An Xbox lock placed through the Xbox Family Settings API holds until "
                        f"{dt_util.as_local(until).isoformat()}; that API cannot be reached now, "
                        "so it cannot be extended. Nothing was changed."
                    )
                self._platform_holds.pop(key, None)
            return await self._async_lock_xbox_mobile(account_id, requested, key)

    async def _async_unlock_xbox(self, account_id: str, key: str) -> dict[str, Any]:
        """unlock_platform on Xbox."""
        async with self._hold_lock(key):
            await self._async_load_platform_holds()
            hold = self._platform_holds.get(key)
            if self._is_mobile_hold(hold):
                return await self._async_unlock_xbox_mobile(account_id, key, hold)
            if self._xapprove_usable():
                result = await self._async_xapprove_unlock(account_id, key, hold)
                if result is not None:
                    return result
            if hold is None:
                return await self._async_xbox_not_locked(account_id)
            if hold.get("until") and not self._hold_in_force(hold, dt_util.utcnow()):
                self._platform_holds.pop(key, None)
                await self._async_save_platform_holds()
                self._platform_override_until.pop((str(account_id), "xbox"), None)
                return {
                    "prior": dict(hold.get("prior") or make_override(UNKNOWN)),
                    "applied": "hold_expired",
                    "lock_path": LOCK_PATH_XAPPROVE,
                }
            raise HomeAssistantError(
                "The Xbox lock was placed through the Xbox Family Settings API, which "
                "cannot be reached now; it ends at its time. Try again later."
            )

    async def _async_release_xbox(self, account_id: str, key: str) -> None:
        """The Xbox lock switch turned off: cancel what is in force."""
        async with self._hold_lock(key):
            await self._async_load_platform_holds()
            hold = self._platform_holds.get(key)
            if self._is_mobile_hold(hold):
                await self._async_unlock_xbox_mobile(account_id, key, hold)
                return
            if self._xapprove_usable() and await self._async_xapprove_release(account_id, key):
                return
            if hold is not None and self._hold_in_force(hold, dt_util.utcnow()):
                raise HomeAssistantError(
                    "The Xbox lock was placed through the Xbox Family Settings API, which "
                    "cannot be reached now; it ends at its time. Try again later."
                )

    def _xbox_extra_time_unavailable(self, action: str) -> HomeAssistantError:
        return HomeAssistantError(
            f"Cannot {action} Xbox extra time: it needs the Xbox Family Settings sign-in "
            "and the xapprove option (Configure), and the Xbox Family Settings API must "
            "answer. Nothing was changed."
        )

    async def _async_bonus_xbox(self, account_id: str, until: datetime, key: str) -> dict[str, Any]:
        if self._xapprove_usable():
            async with self._hold_lock(key):
                await self._async_load_platform_holds()
                await self._async_load_bonuses()
                result = await self._async_xapprove_bonus(account_id, until, key)
            if result is not None:
                return result
        raise self._xbox_extra_time_unavailable("give")

    async def _async_cancel_bonus_xbox(self, account_id: str, key: str) -> dict[str, Any]:
        if self._xapprove_usable():
            async with self._hold_lock(key):
                await self._async_load_bonuses()
                result = await self._async_xapprove_cancel_bonus(account_id, key)
            if result is not None:
                return result
        raise self._xbox_extra_time_unavailable("cancel")

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

    async def async_check_xbox_signin(self, sign_in: SignIn) -> dict[str, str]:
        """Options flow, right after an Xbox sign-in: can it read a child's Xbox?

        Read-only, on the new sign-in's tokens only (no Family token, no
        refresh): the Xbox family read when a child's Xbox user id is not
        known yet, then one GET of that child's overrides made the default way
        (xapprove.DEFAULT_METHOD), as probe_xapprove's first attempt.
        ``outcome`` is ``works`` (``method`` names the way), ``refused``
        (xapprove answered 401/403) or ``unchecked`` with a short ``reason``.
        Never raises; nothing secret in the answer.
        """
        try:
            auth = XboxSisuAuth(lambda: async_get_clientsession(self.hass), sign_in.credential, _no_save)
            auth.seed(sign_in)
            client = XapproveClient(
                lambda: async_get_clientsession(self.hass), method=DEFAULT_METHOD, sisu=auth,
            )
            children = self._children()
            if not children:
                return {"outcome": "unchecked", "reason": "no child list yet"}
            xuid: str | None = None
            for account_id in children:
                xuid = self._option_xuid(account_id) or (self._xuids.get(account_id) or (None,))[0]
                if xuid:
                    break
            if xuid is None:
                try:
                    members = await client.async_family_members()
                except XapproveRefused as err:
                    return {"outcome": "unchecked", "reason": f"Xbox family read refused ({err.status})"}
                if members is None:
                    return {"outcome": "unchecked", "reason": "no Xbox family"}
                parent = sign_in.tokens.parent_xuid
                matched = match_children(members, children, {parent} if parent else set())
                if not matched:
                    return {"outcome": "unchecked", "reason": "no child found in the Xbox family"}
                xuid = next(iter(matched.values()))[0]
            facts = await client.async_try(DEFAULT_METHOD, overrides_url(xuid))
        except Exception as err:  # noqa: BLE001 - the sign-in is kept either way
            _LOGGER.debug("Checking the new Xbox sign-in failed: %s", type(err).__name__)
            return {"outcome": "unchecked", "reason": type(err).__name__}
        status = facts.get("status")
        if isinstance(status, int) and 200 <= status < 300:
            return {"outcome": "works", "reason": "", "method": DEFAULT_METHOD.name}
        if status in (401, 403):
            return {"outcome": "refused", "reason": f"HTTP {status}"}
        return {
            "outcome": "unchecked",
            "reason": f"HTTP {status}" if status else str(facts.get("error") or "no answer")[:80],
        }

    async def async_probe_xapprove(self, confirm_risk: bool = False) -> dict[str, Any]:
        """probe_xapprove: the Xbox sign-in's token steps, the child lookup,
        and read-only overrides GETs.

        The GET is tried with each way of signing it in xapprove.PROBE_ATTEMPTS
        until one answers 2xx; that one is saved and used from then on. Uses
        the Xbox Family Settings sign-in only (never the Family Safety one).
        Answers with non-secret facts only: no token, xuid or gamertag.
        """
        if not confirm_risk:
            raise ServiceValidationError(
                "probe_xapprove signs in to Xbox Live as the Xbox Family Settings app "
                "(unofficial use of Microsoft's client id; see the README). Call it again "
                "with confirm_risk: true to accept that."
            )
        await self._async_load_xapprove_method()
        steps: dict[str, Any] = {}
        result: dict[str, Any] = {
            "entry_id": self.entry.entry_id,
            "option_enabled": self._xapprove_enabled,
            "xbox_signed_in": self._sisu is not None,
            "identity": "xbox_family_settings" if self._sisu is not None else None,
            "method_before": self._xapprove_method.name,
            "refused_earlier": dict(self._xapprove_refusal) if self._xapprove_refusal else None,
            "steps": steps,
        }
        if self._sisu is None:
            result["error"] = (
                "No Xbox Family Settings sign-in: sign in under Configure first."
            )
            return result
        session = async_get_clientsession(self.hass)
        client = XapproveClient(lambda: session, sisu=self._sisu)
        # Fresh tokens, so that every step shows up in ``steps``.
        self._sisu.forget_tokens()
        self._sisu.token_facts = steps
        try:
            tokens = await client.async_tokens()
        except XapproveError as err:
            result["xbox_signin_error"] = (
                err.facts() if isinstance(err, XapproveRefused) else {"error": str(err)}
            )
            if isinstance(err, XapproveRefused):
                self._xapprove_mark_refused(err)
        else:
            result["title_token_received"] = bool(tokens.title_token)
            result["title_id"] = redact(tokens.title_id or "") or None
            result["sisu_xui_keys"] = list(tokens.xui_keys)
            # What sisu/authenticate answers (booleans and names only).
            try:
                result["sisu_authenticate"] = await self._sisu.async_authenticate_probe()
            except XapproveError as err:
                result["sisu_authenticate"] = {"error": str(err)}
            await self._async_probe_family_and_read(client, tokens, result)
            # The explicit XSTS step per [profile|]relying party tried: the
            # variant that worked (None: every one was refused), claim names.
            xsts: dict[str, Any] = {}
            for name in steps:
                if not name.startswith("xsts_authorize[") or not name.endswith("]"):
                    continue
                key = name[len("xsts_authorize["):-1].rsplit("|", 1)[0]
                cached = self._sisu._xsts.get(key)
                xsts[key] = {
                    "variant": self._sisu.xsts_variants.get(key) if cached else None,
                    "xui_keys": list(cached.xui_keys) if cached else None,
                }
            result["xsts"] = xsts
            result["xsts_xui_keys"] = {rp: v["xui_keys"] for rp, v in xsts.items()}
        self._sisu.token_facts = {}
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
        urls = {PATH_OVERRIDES: overrides_url(first[1])}
        urls.update({p: screentime_url(first[1]) for _m, p in PROBE_ATTEMPTS if p != PATH_OVERRIDES})
        attempts: list[dict[str, Any]] = []
        working: AuthMethod | None = None
        state: dict[str, Any] | None = None
        settings_ok: list[str] = []
        for method, path in PROBE_ATTEMPTS:
            facts = await client.async_try(method, urls[path], path)
            found = facts.pop("state", None)
            attempts.append(facts)
            status = facts.get("status")
            if isinstance(status, int) and 200 <= status < 300:
                if path != PATH_OVERRIDES:
                    # The settings read alone is not what the integration uses.
                    settings_ok.append(method.name)
                    continue
                working, state = method, found
                break
        result["overrides_attempts"] = attempts
        result["screentime_read_ok"] = settings_ok
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
