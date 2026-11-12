"""Data coordinator for Microsoft Family Safety."""
from __future__ import annotations

from datetime import datetime, timedelta
import asyncio
import functools
import hashlib
import json
import logging
import time
from collections.abc import Callable
from typing import Any

from pyfamilysafety import FamilySafety
from pyfamilysafety.account import Account
from pyfamilysafety.application import Application
from pyfamilysafety.device import Device
from pyfamilysafety.exceptions import HttpException

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from ._pyfamilysafety_compat import apply_patches, is_stale_roster_error
from .api_client import FamilySafetyWebAPI, FamilySafetyWebAPIError
from .app_pause import AppPauser
from .app_usage import EVENT_APP_USAGE, AppUsageGrowthTracker
from .mobile_overrides import (
    MOBILE_OVERRIDE_BLOCK,
    MOBILE_OVERRIDE_CANCEL,
    is_overrides_document,
    mobile_blocked_platforms,
    mobile_lock_of,
    mobile_platform_entry,
    mobile_platform_name,
)
from .screen_time import (
    ALLOW_UNTIL,
    BLOCK_UNTIL,
    NO_OVERRIDE,
    UNKNOWN,
    active_until,
    iso_utc,
    override as make_override,
    parse_mobile_override,
    parse_utc,
    parse_xbox_override,
    platform_allowance,
    xbox_policy_override,
)
from .xbox_family_api import (
    OVERRIDE_ALLOW,
    OVERRIDE_BLOCK,
    OVERRIDE_REVOKE,
    XboxFamilyAPI,
    XboxFamilyAuthError,
    XboxFamilyError,
    overrides_to_policy,
    schedule_to_policy,
    usage_today,
)
from .const import (
    CONF_SIGNED_IN_AT,
    CONF_STRICT_MODE,
    CONF_APP_USAGE_SENSORS,
    CONF_XBOX_FEATURES,
    CONF_XBOX_REFRESH_TOKEN,
    DEFAULT_STRICT_MODE,
    DEFAULT_APP_USAGE_SENSORS,
    DEFAULT_XBOX_FEATURES,
    EVENT_LOCK_REASSERTED,
    CONF_REFRESH_TOKEN,
    CONF_UPDATE_INTERVAL,
    DAY_KEYS,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    ERROR_AUTH_FAILED,
    ERROR_TOKEN_EXPIRED,
    LEGACY_WEB_AUTH_KEYS,
    EVENT_REQUEST,
)

_LOGGER = logging.getLogger(__name__)


def _updates_listeners(func):
    """Push the entities again once a hold change is done.

    The write inside refreshes the entities before the hold it changes is
    saved, and that refresh is put off when a poll ran moments ago, so
    without this they show the hold from before (a lock of ours read as
    ``parent``) until the next poll. The lock state the write left is
    taken into the polled data first, so the push does not pair the new
    hold with a lock state from before the write.
    """

    @functools.wraps(func)
    async def wrapper(self, *args, **kwargs):
        try:
            return await func(self, *args, **kwargs)
        finally:
            self._sync_blocked_platforms()
            self.async_update_listeners()

    return wrapper
STORAGE_KEY = f"{DOMAIN}.saved_screentime"
STORAGE_VERSION = 1
AUTH_STORAGE_VERSION = 1
AUTH_NOTIFICATION_ID = "familysafety_auth_expired"
ROSTER_NOTIFICATION_ID = "familysafety_roster_error"
#: How long a refused Xbox Family Settings token is left alone before retrying.
_XBOX_RETRY_SECONDS = 6 * 60 * 60
#: Keys kept in the runtime credential store (the rotated mobile refresh token).
_RUNTIME_AUTH_KEYS = frozenset({"anchor", CONF_REFRESH_TOKEN, "updated_at"})
#: Polls in a row a pending request must be missing before its id is forgotten.
_REQUEST_FORGET_POLLS = 3
#: Locks this integration holds on a platform, with the state before them.
PLATFORM_HOLD_STORAGE_KEY = f"{DOMAIN}.platform_holds"
#: Strict mode's read of a Windows/Mobile override gives up after this long.
_STRICT_READ_TIMEOUT = 15
#: Strict mode places one lock again at most this often, so a lock Microsoft
#: keeps reporting as cancelled cannot turn into a write on every poll.
_STRICT_REASSERT_COOLDOWN = timedelta(minutes=10)


def _describe_error(err: Exception | None) -> str:
    """An error for a message; a timeout's own text is empty."""
    if isinstance(err, asyncio.TimeoutError):
        return "no answer in time"
    return str(err)


def _range_to_slots(start_hour: int, start_minute: int, end_hour: int, end_minute: int) -> list[bool]:
    intervals = [False] * 48
    start_slot = (start_hour * 60 + start_minute) // 30
    end_slot = -(-(end_hour * 60 + end_minute) // 30)  # ceiling division
    for i in range(start_slot, min(end_slot, 48)):
        intervals[i] = True
    return intervals


def _policy_intervals_to_slots(day_data: dict[str, Any]) -> list[bool] | None:
    """Convert saved Microsoft interval formats to 48 half-hour slots.

    Restore points saved by older builds may hold allowedIntervals objects
    with begin/end values; current ones store a boolean timeline.
    Supporting both is required so account lock/unlock restores the exact
    schedule that was visible before the lock.
    """
    timeline = day_data.get("timeline")
    if (
        isinstance(timeline, list)
        and len(timeline) == 48
        and all(isinstance(item, bool) for item in timeline)
    ):
        return list(timeline)

    raw = day_data.get("allowedIntervals")
    if raw is None:
        raw = day_data.get("AllowedIntervals")
    if not isinstance(raw, list):
        return None

    slots = [False] * 48
    found = False

    def to_minutes(value: object) -> int | None:
        if not isinstance(value, str):
            return None
        try:
            parts = value.split(":")
            hour = int(parts[0])
            minute = int(parts[1]) if len(parts) > 1 else 0
        except (ValueError, IndexError):
            return None
        if hour == 24 and minute == 0:
            return 1440
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            return None
        return hour * 60 + minute

    for interval in raw:
        if not isinstance(interval, dict):
            continue
        begin = to_minutes(
            interval.get("begin")
            or interval.get("Begin")
            or interval.get("start")
            or interval.get("Start")
        )
        end = to_minutes(interval.get("end") or interval.get("End"))
        if begin is None or end is None or end <= begin:
            continue
        start_slot = max(0, min(47, begin // 30))
        end_slot = max(0, min(48, -(-end // 30)))
        for slot in range(start_slot, end_slot):
            slots[slot] = True
        found = True

    # An explicitly empty list means Microsoft allows no time window for that
    # day. Preserve it as 48 disabled slots instead of substituting the legacy
    # 07:00-22:00 default during a later account-unlock restore.
    return slots if found or raw == [] else None


def _ms_to_minutes(milliseconds: int | float | None) -> int:
    if not milliseconds:
        return 0
    return int(milliseconds / 60000)


def _web_enabled(web: Any) -> bool | None:
    """The on/off of a web restrictions answer (Microsoft spells it "enabled")."""
    if not isinstance(web, dict):
        return None
    value = web.get("enabled", web.get("isEnabled", web.get("IsEnabled")))
    return value if isinstance(value, bool) else None


def _stored_site_lists(web: Any) -> tuple[list[str], list[str]] | None:
    """(blocked, allowed) exactly as Microsoft stores them; None if unreadable.

    From ``exceptions`` (``{"website", "allowed"}``), else from
    ``blockedSites`` / ``allowedSites``.
    """
    if not isinstance(web, dict):
        return None
    exceptions = web.get("exceptions", web.get("Exceptions"))
    if isinstance(exceptions, list) and exceptions:
        blocked: list[str] = []
        allowed: list[str] = []
        for item in exceptions:
            if not isinstance(item, dict):
                continue
            site = item.get("website") or item.get("Website")
            if isinstance(site, str) and site:
                (allowed if item.get("allowed", item.get("Allowed")) else blocked).append(site)
        return blocked, allowed
    if isinstance(exceptions, list) or "blockedSites" in web or "allowedSites" in web:
        return (
            [s for s in web.get("blockedSites") or [] if isinstance(s, str)],
            [s for s in web.get("allowedSites") or [] if isinstance(s, str)],
        )
    return None


class FamilySafetyDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Fetch and coordinate Microsoft Family Safety state."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        update_interval = entry.options.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=update_interval),
            config_entry=entry,
        )
        self.entry = entry
        self.api: FamilySafety | None = None
        self.web_api: FamilySafetyWebAPI | None = None
        self._accounts: dict[str, Account] = {}
        self._devices: dict[str, Device] = {}
        self._is_retrying_auth = False
        self._saved_screentime: dict[str, dict[str, Any]] = {}
        #: Accounts whose restore point this coordinator deliberately released
        #: after a fully successful unlock. Only these may be removed from the
        #: on-disk store; anything else missing from the in-memory dict is
        #: simply not loaded yet and must be preserved.
        self._released_screentime: set[str] = set()
        # One lock per child: lock, unlock and the limits switch each read the
        # schedule, write 14 times and update the restore point, and two of
        # them interleaved can leave the child zeroed with no restore point.
        self._account_locks: dict[str, asyncio.Lock] = {}
        # One store per entry: two Microsoft accounts must not share a file.
        self._store: Store = Store(
            hass, STORAGE_VERSION, f"{STORAGE_KEY}.{entry.entry_id}"
        )
        #: Pre-2.0.25 shared file, read once when the per-entry store is empty.
        self._legacy_store: Store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._auth_store: Store = Store(
            hass, AUTH_STORAGE_VERSION, f"{DOMAIN}.auth.{entry.entry_id}"
        )
        self._runtime_auth_state: dict[str, Any] = {}
        self._reauth_requested = False
        self._reauth_reason: str | None = None
        #: Xbox features off: no Xbox entities and no Xbox Family Settings link.
        self._mobile_only = not entry.options.get(
            CONF_XBOX_FEATURES, DEFAULT_XBOX_FEATURES
        )
        self._auth_notification_sent = False
        self._roster_notification_sent = False
        self._platform_override_until: dict[tuple[str, str], datetime] = {}
        #: (child, platform) -> (when, the platform's overrides entry) as our
        #: last mobile write left it; see _MOBILE_WRITE_GRACE.
        self._mobile_writes: dict[tuple[str, str], tuple[datetime, dict]] = {}
        #: Children whose Xbox lock state is known: the Xbox Family Settings
        #: overrides were read, or a lock/unlock from here succeeded. Until
        #: then the Xbox lock switch reports unknown rather than "unlocked".
        self._xbox_lock_known: set[str] = set()
        #: monotonic time of the next web activity read per child (#65); the
        #: report covers 7 days, so every 30 min is plenty.
        self._web_activity_next: dict[str, float] = {}
        #: Xbox settings through the Xbox Family Settings app's API
        #: (xapprove), on its own refresh token.
        self.xbox_family = XboxFamilyAPI(hass, entry.entry_id)
        self._xbox_xuids: dict[str, str] = {}
        #: Roster accounts with no Xbox child of the same name (no Xbox, or
        #: names that differ between the Xbox profile and the Microsoft account).
        self._xbox_unmatched: set[str] = set()
        self._xbox_auth_warned = False
        #: Apps paused by pause_app, with the state resume_app puts back.
        self.app_pauser = AppPauser(hass, entry.entry_id)
        #: monotonic time before which a refused Xbox Family Settings token is
        #: not tried again (a new link from the options retries at once).
        self._xbox_retry_at = 0.0
        #: Whether the first successful update has run (it clears a stale
        #: "Authentication Required" notification left by a reauthentication).
        self._first_update_done = False
        #: "account|platform" -> the lock this integration holds there and
        #: the override in force before it (see async_lock_platform).
        self._hold_store: Store = Store(
            hass, STORAGE_VERSION, f"{PLATFORM_HOLD_STORAGE_KEY}.{entry.entry_id}"
        )
        self._platform_holds: dict[str, dict[str, Any]] = {}
        self._platform_holds_loaded = False
        self._platform_hold_locks: dict[str, asyncio.Lock] = {}
        self._strict_mode = bool(entry.options.get(CONF_STRICT_MODE, DEFAULT_STRICT_MODE))
        #: hold key -> when strict mode last placed that lock again.
        self._last_reassert: dict[str, datetime] = {}
        #: request id -> polls in a row it has been missing; None before the
        #: first poll, which only sets the baseline (a restart is not news).
        #: An id is forgotten only after _REQUEST_FORGET_POLLS misses, so one
        #: empty or failed read does not make every open request "new" again.
        self._seen_request_ids: dict[str, int] | None = None
        #: How many of each child's most used apps get a usage sensor; 0 = off.
        self.app_usage_sensors = int(
            entry.options.get(CONF_APP_USAGE_SENSORS, DEFAULT_APP_USAGE_SENSORS) or 0
        )
        self._app_usage_tracker = AppUsageGrowthTracker()

    @property
    def xbox_features(self) -> bool:
        """Whether Xbox entities (and the Xbox Family Settings link) are wanted."""
        return not self._mobile_only

    @staticmethod
    def _hash_anchor(material: dict[str, Any]) -> str:
        encoded = json.dumps(
            material, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _entry_auth_anchor(self) -> str:
        """Fingerprint the sign-in stored in the config entry.

        Runtime credentials may rotate between restarts, but a new sign-in
        (reauthentication, reconfigure) must supersede the previous runtime
        cache. Microsoft can return the same refresh-token string for a
        renewed login, so the sign-in time is part of it. Values are hashed
        locally and never logged.
        """
        return self._hash_anchor({
            "refresh_token": self.entry.data.get(CONF_REFRESH_TOKEN, ""),
            "signed_in_at": self.entry.data.get(CONF_SIGNED_IN_AT, ""),
        })

    def _legacy_entry_auth_anchor(self) -> str:
        """The anchor written by versions that also stored the Family web session."""
        return self._hash_anchor({
            "refresh_token": self.entry.data.get(CONF_REFRESH_TOKEN, ""),
            "web_family_token": self.entry.data.get("web_family_token", ""),
            "web_family_referer": self.entry.data.get("web_family_referer", ""),
            "web_cookies": self.entry.data.get("web_cookies", []),
        })

    @staticmethod
    def _policy_key(account_id: Any) -> str:
        """Normalize an account id used as a saved-policy key.

        Account ids reach the coordinator as int (from the mobile API roster)
        and as str (from entities and service calls), while JSON object
        keys are always str. Without normalizing, a restore point saved before
        a reload became invisible afterwards: `1234567890123456 in
        {"1234567890123456": ...}` is False, so unlock believed there was
        nothing to restore and overwrote the schedule with its default.
        """
        return str(account_id)

    def has_saved_screentime(self, account_id: Any) -> bool:
        """Whether a restore point (account lock / limits off) is kept for this child."""
        return self._policy_key(account_id) in self._saved_screentime

    async def async_load_saved_screentime(self) -> None:
        data = await self._store.async_load()
        if data is None:
            # First run on the per-entry store: take over the shared file.
            # It is left in place (another entry may still need it); a
            # restore point released later is removed from the new store only.
            data = await self._legacy_store.async_load()
            if isinstance(data, dict) and data:
                await self._store.async_save(data)
        if isinstance(data, dict):
            self._saved_screentime = {
                self._policy_key(key): value for key, value in data.items()
            }
        await self._async_load_platform_holds()
        auth_state = await self._auth_store.async_load()
        self._runtime_auth_state = {}
        if isinstance(auth_state, dict):
            anchors = {self._entry_auth_anchor()}
            # The anchor of earlier versions (it hashed the web session) only
            # counts for an entry that has not signed in since.
            if CONF_SIGNED_IN_AT not in self.entry.data:
                anchors.add(self._legacy_entry_auth_anchor())
            if auth_state.get("anchor") in anchors:
                # Earlier versions also kept the Family web session (cookies,
                # antiforgery token) here: keep only the rotated refresh token,
                # under the current anchor, before the entry drops those keys.
                kept = {k: v for k, v in auth_state.items() if k in _RUNTIME_AUTH_KEYS}
                kept["anchor"] = self._entry_auth_anchor()
                self._runtime_auth_state = kept
                if kept != auth_state:
                    await self._auth_store.async_save(kept)
            elif set(auth_state) - _RUNTIME_AUTH_KEYS:
                # Superseded by a new sign-in, and still holding an old web
                # session: nothing in it is usable any more.
                await self._auth_store.async_remove()
        self._async_drop_legacy_web_auth()

    def _async_drop_legacy_web_auth(self) -> None:
        """Remove the Family web session and add-on settings of earlier versions.

        The integration no longer uses them; an entry carrying them keeps
        working on its mobile refresh token without a new sign-in.
        """
        data = {k: v for k, v in self.entry.data.items() if k not in LEGACY_WEB_AUTH_KEYS}
        options = {
            k: v for k, v in self.entry.options.items() if k not in LEGACY_WEB_AUTH_KEYS
        }
        if len(data) == len(self.entry.data) and len(options) == len(self.entry.options):
            return
        self.hass.config_entries.async_update_entry(self.entry, data=data, options=options)
        _LOGGER.info(
            "Removed the stored Microsoft Family web session from the entry: "
            "the integration no longer uses it"
        )

    async def _async_save_screentime(self) -> None:
        """Persist saved screentime policies to HA storage.

        These policies are the only way back from a lock, so writing must never
        lose one. The in-memory dict is not authoritative: a config-entry reload
        builds a fresh coordinator whose dict starts empty, and any save before
        the store is read back would wipe the file. Merge onto what is on disk,
        and only drop an entry that this coordinator deliberately released.
        """
        stored = await self._store.async_load()
        merged: dict[str, Any] = dict(stored) if isinstance(stored, dict) else {}
        merged.update(self._saved_screentime)
        for account_id in self._released_screentime:
            merged.pop(account_id, None)
        if merged != self._saved_screentime:
            self._saved_screentime = merged
        await self._store.async_save(merged)

    async def _async_persist_runtime_auth(self, *, refresh_token: str | None = None) -> None:
        """Persist a rotated refresh token without modifying/reloading the config entry."""
        next_state = dict(self._runtime_auth_state)
        next_state["anchor"] = self._entry_auth_anchor()
        if refresh_token:
            next_state[CONF_REFRESH_TOKEN] = refresh_token
        next_state["updated_at"] = dt_util.now().isoformat()

        comparable_old = {k: v for k, v in self._runtime_auth_state.items() if k != "updated_at"}
        comparable_new = {k: v for k, v in next_state.items() if k != "updated_at"}
        if comparable_old == comparable_new:
            return
        self._runtime_auth_state = next_state
        await self._auth_store.async_save(next_state)
        _LOGGER.debug("Persisted rotated Microsoft Family Safety credentials")

    async def _async_persist_current_auth(self) -> None:
        if self.api is None:
            return
        authenticator = self.api.api.authenticator
        await self._async_persist_runtime_auth(
            refresh_token=getattr(authenticator, "refresh_token", None)
        )

    def _runtime_refresh_token(self) -> str:
        return str(
            self._runtime_auth_state.get(CONF_REFRESH_TOKEN)
            or self.entry.data[CONF_REFRESH_TOKEN]
        )

    async def _async_setup_api(self) -> None:
        refresh_token = self._runtime_refresh_token()
        apply_patches(self.hass)
        await self.xbox_family.async_load()
        await self._async_take_xbox_sign_in_token()
        try:
            self.api = await FamilySafety.create(
                token=refresh_token,
                use_refresh_token=True,
                experimental=True,
            )
            self.web_api = FamilySafetyWebAPI(
                self.api.api.authenticator,
                session=async_get_clientsession(self.hass),
            )
            # FamilySafety.create(use_refresh_token=True) itself performs a token
            # refresh. Persist a rotated token immediately so even a restart
            # during the first data poll can recover with the newest credential.
            await self._async_persist_current_auth()
        except HttpException as err:
            text = str(err).lower()
            # A roster resolution 404 is NOT an auth problem. Microsoft returns
            # Family.UnableToFindTargetResource / RosterError when a device still
            # listed in the family roster can no longer be resolved on their side
            # (typically a reset or decommissioned machine). Surfacing this as an
            # auth failure sends the user into a pointless reauth/reinstall loop
            # (issue #38); point them at the real fix instead.
            if self._is_stale_roster_error(text):
                raise UpdateFailed(
                    "Microsoft cannot resolve a device still listed in your "
                    "family roster. Remove the old or reset device at "
                    "https://account.microsoft.com/family, then reload the "
                    "integration."
                ) from err
            if "401" in text or "403" in text or "authentication" in text:
                raise ConfigEntryAuthFailed(ERROR_AUTH_FAILED) from err
            raise UpdateFailed(f"Transient API error: {err}") from err
        except Exception as err:
            text = str(err).lower()
            if self._is_stale_roster_error(text):
                raise UpdateFailed(
                    "Microsoft cannot resolve a device still listed in your "
                    "family roster. Remove the old or reset device at "
                    "https://account.microsoft.com/family, then reload the "
                    "integration."
                ) from err
            if "auth" in text or "token" in text or "401" in text or "403" in text:
                raise ConfigEntryAuthFailed(ERROR_AUTH_FAILED) from err
            raise UpdateFailed(f"API setup error: {err}") from err

    @staticmethod
    def _is_stale_roster_error(text: str) -> bool:
        """Return True for Microsoft's "device not found in roster" 404.

        This is distinct from an authentication failure: the token is valid,
        but the roster references a device Microsoft can no longer resolve.
        Since 2.0.7 the per-member requests tolerate it (see
        _pyfamilysafety_compat._patch_account_roster_tolerance); this branch
        only remains for the roster request itself.
        """
        return is_stale_roster_error(text)

    async def _async_take_xbox_sign_in_token(self) -> None:
        """Link the Xbox Family Settings token the sign-in flow obtained.

        The flow cannot write the entry's Xbox store itself (a new entry has
        no id yet), so it hands the token over in the entry data, once.
        """
        token = self.entry.data.get(CONF_XBOX_REFRESH_TOKEN)
        if not token:
            return
        await self.xbox_family.async_link_refresh_token(str(token), "sign_in")
        self.hass.config_entries.async_update_entry(
            self.entry,
            data={k: v for k, v in self.entry.data.items() if k != CONF_XBOX_REFRESH_TOKEN},
        )

    def account_data(self, account_id: Any) -> dict[str, Any] | None:
        """A child's polled data, whether the id comes as int or str.

        The roster keys accounts by Microsoft's id as the mobile API gives it
        (an int), while service calls and the web API pass it as a str.
        """
        accounts = (self.data or {}).get("accounts") or {}
        if account_id in accounts:
            return accounts[account_id]
        key = self._policy_key(account_id)
        return next(
            (value for acc_id, value in accounts.items() if self._policy_key(acc_id) == key),
            None,
        )

    def get_account(self, account_id: Any) -> Account | None:
        """Get the raw pyfamilysafety Account object, whether the id is int or str."""
        account = self._accounts.get(account_id)
        if account is not None:
            return account
        key = self._policy_key(account_id)
        return next(
            (acc for acc_id, acc in self._accounts.items() if self._policy_key(acc_id) == key),
            None,
        )

    def get_device(self, device_id: str) -> Device | None:
        """Get the raw pyfamilysafety Device object."""
        return self._devices.get(device_id)

    def get_application(self, account_id: str, app_id: str) -> Application | None:
        """Get a raw pyfamilysafety Application object."""
        account = self.get_account(account_id)
        if account is None:
            # The roster may key accounts by int while services pass a str.
            account = next(
                (a for key, a in self._accounts.items() if str(key) == str(account_id)),
                None,
            )
        if account is None:
            return None
        try:
            return account.get_application(app_id)
        except (IndexError, ValueError):
            return None

    async def async_pause_app(self, account_id: str, apps: list[str]) -> dict[str, Any]:
        """Block apps, saving each one's policy before its first pause (app_pause.py)."""
        return await self.app_pauser.async_pause(self, account_id, apps)

    async def async_resume_app(
        self, account_id: str, apps: list[str] | None = None
    ) -> dict[str, Any]:
        """Put back what pause_app saved (every paused app of the child by default)."""
        return await self.app_pauser.async_resume(self, account_id, apps)

    async def async_write_app_policy(
        self, account_id: str, app_id: str, platform: str, body: dict[str, Any],
        *, blocked: bool,
    ) -> dict | None:
        """Write a whole app policy through the mobile API, then refresh.

        Returns Microsoft's answer (the policy as it now stands, when given).
        """
        if not self.web_api:
            raise HomeAssistantError("Mobile API not initialized")
        answer = await self.web_api.write_app_policy(str(account_id), app_id, platform, body)
        app = self.get_application(account_id, app_id)
        if app is not None:
            app.blocked = blocked
        await self.async_request_refresh()
        return answer

    def paused_apps(self, account_id: Any) -> dict[str, str]:
        """app id -> ``ours`` (still blocked by our pause) or ``parent`` (unblocked since)."""
        result: dict[str, str] = {}
        for app_id in self.app_pauser.saved(account_id):
            app = self.get_application(str(account_id), app_id)
            result[app_id] = "parent" if app is not None and not app.blocked else "ours"
        return result

    async def async_block_app(self, account_id: str, app_id: str) -> None:
        """Block an application without wiping its limit (a paused one stays paused)."""
        if await self.app_pauser.async_block(self, account_id, app_id):
            return
        await self._async_set_app_blocked(account_id, app_id, True)

    async def async_unblock_app(self, account_id: str, app_id: str) -> None:
        """Unblock an application without wiping its limit (a paused one is resumed)."""
        if await self.app_pauser.async_unblock(self, account_id, app_id):
            await self.async_request_refresh()
            return
        await self._async_set_app_blocked(account_id, app_id, False)

    async def _async_set_app_blocked(
        self, account_id: str, app_id: str, blocked: bool
    ) -> None:
        """Block or unblock one app, failing as a HomeAssistantError.

        A raw pyfamilysafety HttpException escaped the switch and service
        before, so ``continue_on_error`` in an automation could not skip one
        rejected app (#54).
        """
        app = self.get_application(account_id, app_id)
        if app is None:
            raise HomeAssistantError(
                f"Application {app_id} not found for account {account_id}"
            )
        try:
            await (app.block_app() if blocked else app.unblock_app())
        except HttpException as err:
            raise HomeAssistantError(
                f"Microsoft rejected {'blocking' if blocked else 'unblocking'} "
                f"{app_id}: {err}"
            ) from err
        await self.async_request_refresh()

    #: Platforms not locked through the mobile aggregator. It still accepts
    #: and stores an Xbox override, and reports it back, but the console no
    #: longer honours it, so its Xbox value is not evidence of anything: the
    #: Xbox is locked through the Xbox Family Settings service instead.
    _XAPPROVE_LOCK_PLATFORMS = frozenset({"xbox"})

    def _merge_blocked_platforms(self, account: Account) -> list[str]:
        """Report which platforms are locked right now.

        account.blocked_platforms comes from the pyfamilysafety mobile
        aggregator. For Xbox that value is ignored -- a stale entry left there
        by the old mobile-API lock would make the switch read "locked" and stop
        anything from locking the console properly. Xbox is reported from the
        override this integration set (and, once the Xbox Family Settings
        overrides are read, from those; see _apply_xbox_lock_state).
        """
        now = dt_util.utcnow()
        for key, until in list(self._platform_override_until.items()):
            if until <= now:
                self._platform_override_until.pop(key, None)

        blocked = [
            platform
            for platform in (str(p) for p in (account.blocked_platforms or []))
            if platform.lower() not in self._XAPPROVE_LOCK_PLATFORMS
        ]
        for (acct, platform), until in self._platform_override_until.items():
            if (
                platform in self._XAPPROVE_LOCK_PLATFORMS
                and acct == str(account.user_id)
                and until > now
            ):
                pretty = platform.capitalize()
                if pretty not in blocked:
                    blocked.append(pretty)
        for (acct, platform), (written, entry) in list(self._mobile_writes.items()):
            if now - written >= self._MOBILE_WRITE_GRACE:
                self._mobile_writes.pop((acct, platform), None)
            elif acct == str(account.user_id):
                # Microsoft's GET lags behind its writes: our write's answer wins.
                blocked = [p for p in blocked if p.lower() != platform.lower()]
                if mobile_lock_of(entry) is not None:
                    blocked.append(platform)
        return blocked

    def _apply_xbox_lock_state(
        self,
        account_id: str,
        account_data: dict[str, Any],
        policy: dict | None,
        *,
        no_xbox: bool = False,
    ) -> None:
        """Take the Xbox lock state from the Xbox Family Settings overrides.

        They are what the console actually enforces, so the lock survives a
        Home Assistant restart and reflects a block or release made in the
        Xbox Family Settings app.

        Only an answer that can be read settles the state: a "cancel", a
        blockUntil with a readable expiry, or a recognisable policy carrying no
        override at all. A failed read, an unknown override type, or a
        blockUntil whose validUntil is missing or unreadable changes nothing --
        "unknown" never degrades to "not blocked".

        One exception: no_xbox (no child of the Xbox family has this child's
        name) settles a child whose lock state has never been read as not
        blocked, so an account without an Xbox shows the lock switch as off
        rather than unknown. A child whose state is already known keeps it.
        """
        key = (str(account_id), "xbox")
        if isinstance(policy, dict):
            data = policy.get("data") if isinstance(policy.get("data"), dict) else policy
            override = data.get("screenTimeOverride")
            override_type = (
                str(override.get("screenTimeOverrideType") or "").lower()
                if isinstance(override, dict)
                else None
            )
            if override_type == "blockuntil":
                block_until = FamilySafetyWebAPI.parse_block_until(
                    {"screenTimeOverrides": [override]}
                )
                if block_until is None:
                    _LOGGER.debug(
                        "Xbox policy for %s has a blockUntil override without a "
                        "readable validUntil; keeping the current lock state",
                        account_id,
                    )
                else:
                    if block_until > dt_util.utcnow():
                        self._platform_override_until[key] = block_until
                    else:
                        self._platform_override_until.pop(key, None)
                    self._xbox_lock_known.add(key[0])
            elif override_type in ("cancel", "allowuntil") or (
                override is None and "dailyRestrictions" in data
            ):
                # "allowUntil" (extra time granted) explicitly allows the
                # console, so it is a readable "not blocked", not an unknown.
                # Microsoft answered and is not blocking: a stale local entry goes.
                self._platform_override_until.pop(key, None)
                self._xbox_lock_known.add(key[0])
            elif override_type is not None:
                _LOGGER.debug(
                    "Xbox policy for %s has an unrecognised override type %r; "
                    "keeping the current lock state",
                    account_id, override_type,
                )
        elif no_xbox and key[0] not in self._xbox_lock_known:
            self._platform_override_until.pop(key, None)
            self._xbox_lock_known.add(key[0])
        blocked = [p for p in (account_data.get("blocked_platforms") or []) if p != "Xbox"]
        if key in self._platform_override_until:
            blocked.append("Xbox")
        account_data["blocked_platforms"] = blocked
        account_data["xbox_lock_known"] = key[0] in self._xbox_lock_known

    #: Microsoft's GET of the mobile overrides lags behind its writes: a Cancel
    #: whose answer listed nothing was still listed by a GET a second later,
    #: and a block cleared in the app for minutes. For this long after a write
    #: of ours, its answer stands for that platform over what a GET says.
    _MOBILE_WRITE_GRACE = timedelta(minutes=2)
    #: Pauses (seconds) before the reads that judge a write whose own answer
    #: is unusable (an error such as HTTP 500, or no overrides document).
    _MOBILE_READBACK_DELAYS: tuple[float, ...] = (0.0, 3.0, 3.0)
    #: What a mobile overrides request can fail with: an error answer, no
    #: answer in time, or (HttpException) a token refresh that failed after a
    #: 401/403, which the mobile client does not wrap.
    _MOBILE_REQUEST_ERRORS = (FamilySafetyWebAPIError, HttpException, asyncio.TimeoutError)

    def _remember_mobile_write(self, child: str, state: Any, platform: str) -> None:
        """Keep the platform's entry of a state our write left (see above)."""
        entry = mobile_platform_entry(state, mobile_platform_name(platform))
        if entry is not None:
            self._mobile_writes[(child, mobile_platform_name(entry.get("appliesTo")))] = (
                dt_util.utcnow(), entry,
            )

    def _with_recent_writes(self, child: str, state: Any) -> Any:
        """A GET's overrides, with our writes of the last minutes standing over it."""
        if not is_overrides_document(state):
            return state
        now = dt_util.utcnow()
        platforms = []
        for entry in state["lockablePlatforms"]:
            name = mobile_platform_name((entry or {}).get("appliesTo")) if isinstance(entry, dict) else ""
            written = self._mobile_writes.get((child, name))
            if written is not None and now - written[0] < self._MOBILE_WRITE_GRACE:
                if entry.get("overrides") != written[1].get("overrides"):
                    _LOGGER.debug(
                        "The %s overrides read for %s differ from our write %s ago; "
                        "keeping what the write answered",
                        name, child, now - written[0],
                    )
                entry = {**entry, "overrides": written[1].get("overrides") or []}
            platforms.append(entry)
        return {**state, "lockablePlatforms": platforms}

    async def _async_read_mobile_overrides(self, child: str) -> dict:
        """GET the mobile overrides, our recent writes standing over a lagging answer."""
        return self._with_recent_writes(
            child, await self.web_api.get_mobile_device_overrides(child)
        )

    async def _async_post_mobile_override(
        self,
        child: str,
        target: str,
        override_type: str,
        until: datetime,
        settled: Callable[[Any], bool],
    ) -> tuple[Any, Exception | None, Exception | None]:
        """POST one mobile override; return ``(state, write_error, read_error)``.

        ``state`` is the POST's own answer when it is an overrides document.
        Otherwise (an error such as HTTP 500, which Microsoft has answered to
        writes it applied, a timeout, which can also come after the write was
        applied, or no document) the overrides are read up to
        len(_MOBILE_READBACK_DELAYS) times, stopping at the first read for
        which ``settled`` holds; ``state`` is then the last read (None, with
        ``read_error``, when no read succeeded). A read that answers no
        overrides document (``{}``, an error object) counts as a failed read.
        A state from the POST answer,
        or a settled read, is remembered for _MOBILE_WRITE_GRACE.
        """
        write_error: Exception | None = None
        reply: Any = None
        try:
            reply = await self.web_api.set_mobile_device_override(
                child, target, override_type, until
            )
        except self._MOBILE_REQUEST_ERRORS as err:
            write_error = err
        if write_error is None and is_overrides_document(reply):
            self._remember_mobile_write(child, reply, target)
            return reply, None, None
        state: Any = None
        read_error: Exception | None = None
        for delay in self._MOBILE_READBACK_DELAYS:
            if delay:
                await asyncio.sleep(delay)
            try:
                answer = await self.web_api.get_mobile_device_overrides(child)
            except self._MOBILE_REQUEST_ERRORS as err:
                read_error = err
                continue
            if not is_overrides_document(answer):
                # {} or an error object lists no lock: never a settled unlock,
                # nor the state when no read gives the document.
                read_error = FamilySafetyWebAPIError(
                    "the read answered no overrides document"
                )
                continue
            state = answer
            read_error = None
            if settled(state):
                self._remember_mobile_write(child, state, target)
                break
        return state, write_error, (read_error if state is None else None)

    async def _override_platform_mobile(
        self,
        account: Account,
        account_id: str,
        platform: str,
        override_type: str,
        until: datetime | None = None,
    ) -> None:
        """Lock (BlockUntil) or unlock (Cancel) a platform on the mobile API.

        The target name is read from Microsoft's own list first ("Windows" is
        also listed as "Desktop"). The outcome is decided by the overrides
        document the POST answers with; when the POST fails (Microsoft has
        answered HTTP 500 both to overrides it applied and to a cancel it did
        not apply) or answers no document, by reads retried for a few
        seconds (see _async_post_mobile_override). Never by the last poll.
        """
        if self.web_api is None:
            raise HomeAssistantError(
                f"Cannot change {platform}: the Family Safety mobile API is not connected yet."
            )
        child = str(account_id)
        lock = override_type == MOBILE_OVERRIDE_BLOCK
        verb = "lock" if lock else "unlock"
        try:
            entry = mobile_platform_entry(
                await self._async_read_mobile_overrides(child), platform
            )
        except self._MOBILE_REQUEST_ERRORS as err:
            raise HomeAssistantError(
                f"Could not read the {platform} overrides, so nothing was sent: "
                f"{_describe_error(err)}"
            ) from err
        if entry is None:
            raise HomeAssistantError(
                f"Microsoft lists no {platform} device limits for this child, "
                f"so there is nothing to {verb}."
            )

        def locked(state: Any) -> bool:
            return any(p.lower() == platform.lower() for p in mobile_blocked_platforms(state))

        state, write_error, read_error = await self._async_post_mobile_override(
            child,
            str(entry.get("appliesTo")),
            override_type,
            until or dt_util.utcnow(),
            lambda state: locked(state) == lock,
        )
        if state is None:
            # No answer and no read says what happened: never report success.
            await self.async_request_refresh()
            answer = (
                f"answered an error to the {platform} {verb} "
                f"({_describe_error(write_error)})"
                if write_error is not None
                else f"accepted the {platform} {verb} without saying what changed"
            )
            raise HomeAssistantError(
                f"Microsoft {answer}, and the overrides could not be read back "
                f"({_describe_error(read_error)}); check its state before trying again."
            ) from (write_error or read_error)
        account.blocked_platforms = mobile_blocked_platforms(
            self._with_recent_writes(child, state)
        )
        if locked(state) != lock:
            await self.async_request_refresh()
            answer = (
                f"answered an error ({_describe_error(write_error)})"
                if write_error
                else "accepted it"
            )
            listed = "still lists a lock" if not lock else "lists no lock"
            raise HomeAssistantError(
                f"Could not {verb} {platform}: Microsoft {answer}, but its "
                f"overrides {listed}. Check the Family Safety app before trying again."
            ) from write_error
        if write_error is not None:
            _LOGGER.info(
                "Microsoft answered an error but the %s %s was applied: %s",
                platform, verb, _describe_error(write_error),
            )
        await self.async_request_refresh()

    # ------------------------------------------------------------------
    # lock_platform / unlock_platform: save the override in force, put it back
    # ------------------------------------------------------------------
    #
    # The first lock of a child's platform reads the override Microsoft has
    # in force (a parent's own lock, extra time, or nothing) and keeps it as
    # the "prior" in a Store, together with the fact that this integration
    # now holds a lock there. A later lock, also after a restart or after a
    # failed call, finds that hold and never takes its own lock for the prior
    # state. unlock_platform puts the prior back (a blockUntil still in the
    # future) or cancels, then forgets the hold; it only undoes a lock that
    # is still ours.
    #
    # Xbox is read and written through the Xbox Family Settings service
    # (xapprove), Windows and Mobile through the mobile API.

    #: How far Microsoft's blockUntil may sit from the end we recorded and
    #: still be ours (it stores the value sent, give or take rounding).
    _OWN_LOCK_TOLERANCE = timedelta(minutes=2)
    #: A hold whose lock ended is kept this long, so an unlock soon after
    #: still knows the lock ran out (and sends nothing), then pruned.
    _HOLD_KEEP_AFTER_END = timedelta(days=1)

    @staticmethod
    def _hold_key(account_id: Any, platform: str) -> str:
        return f"{account_id}|{platform.lower()}"

    def _hold_lock(self, key: str) -> asyncio.Lock:
        return self._platform_hold_locks.setdefault(key, asyncio.Lock())

    async def _async_load_platform_holds(self) -> None:
        if self._platform_holds_loaded:
            return
        data = await self._hold_store.async_load()
        if isinstance(data, dict):
            self._platform_holds = {
                str(k): v for k, v in data.items() if isinstance(v, dict)
            }
        self._platform_holds_loaded = True

    async def _async_save_platform_holds(self) -> None:
        await self._hold_store.async_save(self._platform_holds)

    async def _async_prune_platform_holds(self) -> None:
        """Forget holds whose lock ended more than a day ago (run on each poll)."""
        await self._async_load_platform_holds()
        limit = dt_util.utcnow() - self._HOLD_KEEP_AFTER_END
        stale = [
            key for key, hold in self._platform_holds.items()
            if (until := parse_utc(hold.get("until"))) is not None and until <= limit
            and not self._hold_lock(key).locked()
        ]
        for key in stale:
            self._platform_holds.pop(key, None)
            self._last_reassert.pop(key, None)
        if stale:
            await self._async_save_platform_holds()

    def platform_hold(self, account_id: Any, platform: str) -> dict[str, Any] | None:
        """The lock this integration holds on a child's platform, if any."""
        hold = self._platform_holds.get(self._hold_key(account_id, platform))
        return dict(hold) if hold else None

    def _roster_id(self, account_id: Any) -> Any:
        """The roster key of a child whose id may come as int or str."""
        if account_id in self._accounts:
            return account_id
        for key in self._accounts:
            if str(key) == str(account_id):
                return key
        raise ValueError(f"Account {account_id} not found")

    def _sync_blocked_platforms(self) -> None:
        """Put the Xbox lock state our writes left into the polled data.

        Only the Xbox entry: it comes from the overrides this integration
        tracks, the other platforms from the last poll.
        """
        now = dt_util.utcnow()
        for key in self._accounts:
            data = self._account_data(key)
            if data is None:
                continue
            blocked = [p for p in (data.get("blocked_platforms") or []) if p != "Xbox"]
            until = self._platform_override_until.get((str(key), "xbox"))
            if until is not None and until > now:
                blocked.append("Xbox")
            data["blocked_platforms"] = blocked

    def _account_data(self, account_id: Any) -> dict[str, Any] | None:
        """A child's polled data, whether the id comes as int or str."""
        return self.account_data(account_id)

    async def _async_read_platform_override(
        self, account_id: Any, platform: str
    ) -> dict[str, Any] | None:
        """The override Microsoft has in force for this platform; None if unknown."""
        try:
            if platform.lower() == "xbox":
                xuid = await self._xapprove_xuid_for(account_id)
                if xuid is None:
                    return None
                return parse_xbox_override(await self.xbox_family.async_get_overrides(xuid))
            account = self._accounts.get(self._roster_id(account_id))
            if self.web_api is None or account is None:
                return None
            # Read fresh from the mobile API every time, never from the last poll;
            # a write of ours from the last minutes stands over a lagging answer.
            response = await self._async_read_mobile_overrides(str(account.user_id))
        except Exception as err:  # noqa: BLE001 - a failed read is "unknown"
            _LOGGER.debug("Could not read the %s override of %s: %r", platform, account_id, err)
            return None
        return parse_mobile_override(response, platform)

    async def _async_read_platform_override_retry(
        self, account_id: Any, platform: str
    ) -> dict[str, Any] | None:
        """The override in force, read a second time when the first read fails."""
        current = await self._async_read_platform_override(account_id, platform)
        if current is None:
            current = await self._async_read_platform_override(account_id, platform)
        return current

    async def _async_write_platform_override(
        self, account_id: Any, platform: str, block: bool, until: datetime | None = None
    ) -> None:
        """Send a blockUntil (until ``until``) or a cancel for one platform.

        Xbox goes through the Xbox Family Settings service only, Windows and
        Mobile through the mobile API; a failure is raised, never retried on
        another path.
        """
        key = (str(account_id), platform.lower())
        if platform.lower() == "xbox":
            await self._async_write_xbox_override(account_id, block, until)
        else:
            account = self._accounts[self._roster_id(account_id)]
            await self._override_platform_mobile(
                account, account_id, platform,
                MOBILE_OVERRIDE_BLOCK if block else MOBILE_OVERRIDE_CANCEL,
                until if block else None,
            )
        if block:
            self._platform_override_until[key] = dt_util.as_utc(until)
        else:
            self._platform_override_until.pop(key, None)

    async def _async_write_xbox_override(
        self, account_id: Any, block: bool, until: datetime | None
    ) -> None:
        """Lock (override 2) or release (override 100) an Xbox through xapprove."""
        action = "lock" if block else "unlock"
        xuid = await self._xapprove_xuid_for(account_id)
        if xuid is None:
            if not self._xapprove_active:
                reason = (
                    "the Xbox Family Settings sign-in is not linked; link it from "
                    "the integration options (Link the Xbox Family Settings sign-in)"
                    if not self.xbox_family.linked
                    else "Microsoft refused the Xbox Family Settings sign-in; link it "
                    "again from the integration options"
                )
            elif str(account_id) in self._xbox_unmatched:
                reason = "no child of the Xbox family has this child's name"
            else:
                reason = "the Xbox Family Settings service did not answer"
            raise HomeAssistantError(f"Cannot {action} the Xbox: {reason}")
        try:
            # The Xbox app's "revoke" (type 100) is the release captured so far.
            result = await self.xbox_family.async_create_override(
                xuid,
                OVERRIDE_BLOCK if block else OVERRIDE_REVOKE,
                until if block else dt_util.utcnow() + timedelta(minutes=10),
            )
        except XboxFamilyError as err:
            raise HomeAssistantError(f"Could not {action} the Xbox: {err}") from err
        blocked = any(
            o.get("overrideType") == OVERRIDE_BLOCK
            for o in (result or {}).get("overrides") or []
            if isinstance(o, dict)
        )
        if blocked != block:
            raise HomeAssistantError(
                f"The Xbox Family Settings service accepted the {action} but "
                f"{'reports no block' if block else 'still reports a block'}."
            )
        self._xbox_lock_known.add(str(account_id))
        await self.async_request_refresh()

    @staticmethod
    def _hold_in_force(hold: dict[str, Any], now: datetime) -> bool:
        """A hold whose lock has not run out yet (or was never sent)."""
        until = parse_utc(hold.get("until"))
        return until is None or until > now

    @staticmethod
    def _holds_the_lock(hold: dict[str, Any]) -> bool:
        """Our lock went through and no one else's lock has replaced it since."""
        return bool(hold.get("confirmed")) and not hold.get("superseded")

    def _is_own_lock(self, hold: dict[str, Any], current: dict[str, Any] | None) -> bool:
        """Microsoft still shows the blockUntil this hold sent."""
        if not isinstance(current, dict) or current.get("type") != BLOCK_UNTIL:
            return False
        ours = parse_utc(hold.get("until"))
        theirs = parse_utc(current.get("valid_until"))
        return (
            ours is not None
            and theirs is not None
            and abs(ours - theirs) <= self._OWN_LOCK_TOLERANCE
        )

    @_updates_listeners
    async def async_lock_platform(
        self, account_id: str, platform: str, valid_until: datetime | None = None
    ) -> dict[str, Any]:
        """Lock a platform until ``valid_until`` (default 24 h).

        Returns ``{"prior": ..., "applied": ...}``: the override saved on the
        first lock (kept unchanged by later ones) and the one now sent. A
        blockUntil already in force that ends later is kept, never shortened.

        On a later lock, a longer block that is not ours (a parent extended
        it in the app) is left in force and not taken over: the hold is
        marked ``superseded``, nothing is sent (``applied:
        "parent_lock_kept"``, with ``current``), and unlock leaves it alone.
        """
        roster_id = self._roster_id(account_id)
        requested = dt_util.as_utc(valid_until or (dt_util.now() + timedelta(hours=24)))
        key = self._hold_key(roster_id, platform)
        async with self._hold_lock(key):
            await self._async_load_platform_holds()
            now = dt_util.utcnow()
            hold = self._platform_holds.get(key)
            if hold is not None and not self._hold_in_force(hold, now):
                # Our earlier lock has run out on its own: a new hold starts.
                self._platform_holds.pop(key, None)
                hold = None
            current = await self._async_read_platform_override_retry(roster_id, platform)
            current_block = (
                active_until(current, now)
                if (current or {}).get("type") == BLOCK_UNTIL
                else None
            )
            if hold is None:
                if current is None:
                    _LOGGER.info(
                        "Could not read the %s override of %s before locking; the lock "
                        "goes ahead, and unlock only cancels a lock it can tell is ours",
                        platform, roster_id,
                    )
                hold = {
                    "account_id": str(roster_id),
                    "platform": platform,
                    "prior": current if current is not None else make_override(UNKNOWN),
                    "until": None,
                    "confirmed": False,
                    "created": iso_utc(now),
                }
                self._platform_holds[key] = hold
                await self._async_save_platform_holds()
            sent = hold.get("until") is not None
            own = sent and self._is_own_lock(hold, current)
            if (
                sent
                and current_block is not None
                and current_block > requested
                and not own
            ):
                # Someone else's longer lock replaced ours: leave it, do not adopt it.
                hold["superseded"] = True
                await self._async_save_platform_holds()
                return {
                    "prior": dict(hold["prior"]),
                    "applied": "parent_lock_kept",
                    "current": self._override_details(current),
                }
            if not sent:
                # First lock: a block already in force that ends later is kept.
                target = max(requested, current_block or requested)
            else:
                # Ours, or unreadable (nothing says otherwise): never shortened.
                ours = parse_utc(hold.get("until")) if (current is None or own) else None
                target = max(requested, ours or requested)
            # Recorded before the write: should Microsoft apply it and the
            # call still fail, a retry knows the blockUntil it reads is ours.
            hold["until"] = iso_utc(target)
            hold.pop("superseded", None)
            await self._async_save_platform_holds()
            await self._async_write_platform_override(roster_id, platform, True, target)
            hold["confirmed"] = True
            hold["confirmed_at"] = iso_utc(dt_util.utcnow())
            await self._async_save_platform_holds()
            return {"prior": dict(hold["prior"]), "applied": make_override(BLOCK_UNTIL, target)}

    @_updates_listeners
    async def async_unlock_platform(self, account_id: str, platform: str) -> dict[str, Any]:
        """Unlock a platform, putting back the override saved by the first lock.

        * No hold: cancel, as before this integration tracked its locks.
        * Our hold has run out: forget it; nothing is sent
          (``applied: "hold_expired"``).
        * Microsoft still shows our blockUntil: put back a saved blockUntil
          still in the future, else cancel. Saved extra time (allowUntil) is
          not written back, so that also cancels.
        * Microsoft shows nothing in force although we hold the lock (a
          release in the app, or a read that misses our lock): cancel anyway,
          which is harmless if nothing is there. The same when the state
          cannot be read at all (``state_unknown: true`` in the answer).
        * Someone else's lock or extra time is in force (or ours was
          replaced): leave it, forget the hold (``applied:
          "left_parent_override"`` with ``current``), or ``"not_locked"``
          when our lock never went through.

        With an unknown prior (it could not be read before the lock), only a
        lock that is identifiably ours is cancelled; the answer then has
        ``prior_unknown: true``. Returns ``{"prior": ..., "applied": ...}``.
        """
        roster_id = self._roster_id(account_id)
        key = self._hold_key(roster_id, platform)
        async with self._hold_lock(key):
            await self._async_load_platform_holds()
            hold = self._platform_holds.get(key)
            prior = (
                dict(hold["prior"])
                if hold and isinstance(hold.get("prior"), dict)
                else None
            )
            flags: dict[str, Any] = {}
            if prior is not None and prior.get("type") == UNKNOWN:
                flags["prior_unknown"] = True
            restore = True
            if hold is not None:
                left: str | None = None
                current: dict[str, Any] | None = None
                if hold.get("until") and not self._hold_in_force(hold, dt_util.utcnow()):
                    left = "hold_expired"
                else:
                    current = await self._async_read_platform_override_retry(
                        roster_id, platform
                    )
                    holding = self._holds_the_lock(hold)
                    if current is None:
                        flags["state_unknown"] = True
                        if not holding:
                            left = "left_parent_override" if hold.get("confirmed") else "not_locked"
                    elif self._is_own_lock(hold, current) and not hold.get("superseded"):
                        pass  # ours: restore or cancel below
                    elif current.get("type") == NO_OVERRIDE and holding:
                        # Released elsewhere, or the read misses our lock: cancel
                        # anyway (harmless) rather than leave a child locked.
                        restore = False
                    else:
                        # An unconfirmed hold is a lock that never went through.
                        left = "left_parent_override" if hold.get("confirmed") else "not_locked"
                        _LOGGER.info(
                            "%s of %s was changed since our lock (%s); left as it is",
                            platform, roster_id, current.get("type"),
                        )
                if left is not None:
                    self._platform_holds.pop(key, None)
                    await self._async_save_platform_holds()
                    if left == "hold_expired":
                        self._platform_override_until.pop(
                            (str(roster_id), platform.lower()), None
                        )
                    result: dict[str, Any] = {"prior": prior, "applied": left, **flags}
                    if left == "left_parent_override" and current is not None:
                        # What is in force now, so a caller can match it.
                        result["current"] = self._override_details(current)
                    return result
            restore_until = active_until(prior) if restore else None
            if restore_until is not None and prior["type"] == BLOCK_UNTIL:
                await self._async_write_platform_override(
                    roster_id, platform, True, restore_until
                )
                applied = make_override(BLOCK_UNTIL, restore_until)
            else:
                await self._async_write_platform_override(roster_id, platform, False)
                applied = make_override(NO_OVERRIDE)
                if restore_until is not None:
                    _LOGGER.info(
                        "%s of %s had extra time (allowUntil) before the lock; it is "
                        "not granted again from here, the lock was cancelled",
                        platform, roster_id,
                    )
                    applied["not_restored"] = prior
            if hold is not None:
                self._platform_holds.pop(key, None)
                await self._async_save_platform_holds()
            return {"prior": prior, "applied": applied, **flags}

    async def async_release_platform(self, account_id: str, platform: str) -> None:
        """Cancel any lock on the platform and forget the hold: the lock switch's off.

        Unlike unlock_platform, nothing saved is put back. Someone turning the
        switch off wants the platform free, and a restored lock would flip the
        switch straight back on.
        """
        roster_id = self._roster_id(account_id)
        key = self._hold_key(roster_id, platform)
        async with self._hold_lock(key):
            await self._async_load_platform_holds()
            await self._async_write_platform_override(roster_id, platform, False)
            if self._platform_holds.pop(key, None) is not None:
                await self._async_save_platform_holds()

    async def _async_enforce_holds(
        self,
        xbox_overrides: dict[str, Any],
        xbox_read_at: dict[str, datetime] | None = None,
    ) -> None:
        """Strict mode: put back a lock of ours that Microsoft reports as cancelled.

        Only a hold this integration confirmed and still holds, whose end is
        still ahead, and only on an explicit "nothing in force" read-back: an
        unreadable answer never triggers a write. The Xbox read-back is the
        xapprove answer this poll already fetched (``xbox_overrides``); a lock
        confirmed after that read (``xbox_read_at``) is not judged by it,
        since the read predates the lock. Other platforms read the mobile API
        now. A lock is placed again at most every 10 minutes, counted from
        our last write of it (the lock itself or a previous re-lock), so a
        read that does not show a fresh lock yet cannot trigger a second one.
        The write runs in the background, after this poll, so its refresh
        does not nest.
        """
        if not self._strict_mode:
            return
        await self._async_load_platform_holds()
        now = dt_util.utcnow()
        candidates: list[tuple[str, str, str, datetime]] = []
        for key, hold in list(self._platform_holds.items()):
            until = parse_utc(hold.get("until"))
            if not self._holds_the_lock(hold) or until is None or until <= now:
                continue
            written = [
                t for t in (self._last_reassert.get(key), parse_utc(hold.get("confirmed_at")))
                if t is not None
            ]
            if written and now - max(written) < _STRICT_REASSERT_COOLDOWN:
                continue
            account_id = str(hold.get("account_id"))
            platform = str(hold.get("platform") or key.split("|", 1)[-1])
            if platform.lower() == "xbox":
                read_at = (xbox_read_at or {}).get(account_id)
                confirmed_at = parse_utc(hold.get("confirmed_at"))
                if read_at is not None and confirmed_at is not None and confirmed_at > read_at:
                    continue
            candidates.append((key, account_id, platform, until))

        async def read_back(account_id: str, platform: str) -> Any:
            if platform.lower() == "xbox":
                return xbox_overrides.get(account_id)
            # All at once, each capped, so a slow Microsoft cannot hold the poll.
            try:
                return await asyncio.wait_for(
                    self._async_read_platform_override(account_id, platform),
                    _STRICT_READ_TIMEOUT,
                )
            except (TimeoutError, asyncio.TimeoutError):
                return None

        states = await asyncio.gather(
            *(read_back(account_id, platform) for _, account_id, platform, _ in candidates)
        )
        for (key, account_id, platform, until), state in zip(candidates, states):
            if not isinstance(state, dict) or state.get("type") != NO_OVERRIDE:
                continue
            self._last_reassert[key] = now
            self.entry.async_create_background_task(
                self.hass,
                self._async_reassert_hold(key, account_id, platform, until),
                f"{DOMAIN} reassert {platform} lock",
            )

    async def _async_reassert_hold(
        self, key: str, account_id: str, platform: str, until: datetime
    ) -> None:
        """Place a held lock again and fire ``microsoft_family_safety_lock_reasserted``."""
        async with self._hold_lock(key):
            hold = self._platform_holds.get(key)
            if hold is None or parse_utc(hold.get("until")) != until:
                return  # unlocked or locked again meanwhile
            try:
                await self._async_write_platform_override(
                    self._roster_id(account_id), platform, True, until
                )
            except Exception as err:  # noqa: BLE001 - retried after the cooldown
                _LOGGER.warning(
                    "Strict mode could not lock %s of %s again: %s", platform, account_id, err
                )
                return
        _LOGGER.info(
            "Strict mode: the %s lock of %s read back as cancelled and was placed again",
            platform, account_id,
        )
        self.hass.bus.async_fire(
            EVENT_LOCK_REASSERTED,
            {
                "entry_id": self.entry.entry_id,
                "account_id": account_id,
                "platform": platform,
                "until": iso_utc(until),
            },
        )

    @staticmethod
    def _local_iso(value: Any) -> str | None:
        parsed = parse_utc(value)
        return dt_util.as_local(parsed).isoformat() if parsed else None

    @classmethod
    def _override_details(cls, current: dict[str, Any] | None) -> dict[str, Any]:
        """``{type, valid_until (UTC), override_until (local), allow_until (local)}``."""
        kind = (current or {}).get("type") or NO_OVERRIDE
        until = (current or {}).get("valid_until") if kind in (BLOCK_UNTIL, ALLOW_UNTIL) else None
        return {
            "type": kind,
            "valid_until": until,
            "override_until": cls._local_iso(until),
            "allow_until": cls._local_iso(until) if kind == ALLOW_UNTIL else None,
        }

    def platform_hold_state(self, account_id: Any, platform: str) -> dict[str, Any]:
        """Who decides a platform's lock now: ``ours``, ``parent`` or ``none``.

        From what the poll already holds, no extra request:

        * ``ours``: we hold a lock (lock_platform, not run out) and Microsoft
          still shows our blockUntil (within 2 minutes of the end we set).
        * ``parent``: an override is in force that is not ours (a lock or
          extra time from the Family Safety app), or we hold a lock but
          Microsoft shows it cancelled or ending at another time.
        * ``none``: nothing in force and no hold of ours.

        The Xbox override is the one the poll read from xapprove (unknown
        without the Xbox Family Settings link). For Windows and Mobile only
        the mobile "locked" flag is polled, without an end, so a lock with a
        hold of ours reads as ``ours``, one without as ``parent``, and extra
        time is not seen. Also returns ``override``, ``override_until`` and
        ``allow_until`` (local ISO).
        """
        account = self._account_data(account_id) or {}
        current: dict[str, Any] | None
        if platform.lower() == "xbox":
            current = account.get("xbox_override")
        elif platform in (account.get("blocked_platforms") or []):
            until = self._platform_override_until.get((str(account_id), platform.lower()))
            current = make_override(BLOCK_UNTIL, until)
        elif account.get("blocked_platforms") is not None:
            current = make_override(NO_OVERRIDE)
        else:
            current = None
        hold = self._platform_holds.get(self._hold_key(account_id, platform))
        if hold is not None and (
            not hold.get("confirmed") or not self._hold_in_force(hold, dt_util.utcnow())
        ):
            hold = None
        kind = (current or {}).get("type")
        if hold is not None and kind == BLOCK_UNTIL and (
            current.get("valid_until") is None or self._is_own_lock(hold, current)
        ):
            who = "ours"
        elif kind in (BLOCK_UNTIL, ALLOW_UNTIL):
            who = "parent"
        elif hold is not None and kind == NO_OVERRIDE:
            who = "parent"
        elif hold is not None:
            who = "ours"  # unreadable: nothing says otherwise
        else:
            who = "none"
        details = self._override_details(current)
        return {
            "hold": who,
            "override": details["type"] if current is not None else None,
            "override_until": details["override_until"],
            "allow_until": details["allow_until"],
        }

    async def async_approve_request(
        self, request_id: str, extension_time: int = 3600
    ) -> bool:
        """Approve a pending screen time request.

        ``extension_time`` is expressed in seconds.

        Workaround for issue #20: pyfamilysafety 1.1.2 converts the value with
        ``extension_time * 100`` while commenting "seconds to ms" — but seconds
        to milliseconds is ``* 1000``. The library therefore grants only a tenth
        of the requested time (e.g. 60 min -> 6 min). We pre-multiply by 10 so
        the library's faulty ``* 100`` yields the correct milliseconds
        (seconds * 10 * 100 == seconds * 1000).
        """
        if self.api is None:
            return False
        try:
            return await self.api.approve_pending_request(
                request_id, extension_time * 10
            )
        except ValueError as err:
            await self._async_request_gone("approve", request_id, err)
        return False

    async def async_deny_request(self, request_id: str) -> bool:
        """Deny a pending screen time request."""
        if self.api is None:
            return False
        try:
            return await self.api.deny_pending_request(request_id)
        except ValueError as err:
            await self._async_request_gone("deny", request_id, err)
        return False

    async def _async_request_gone(
        self, action: str, request_id: str, err: ValueError
    ) -> None:
        """Turn pyfamilysafety's ``ValueError`` into a clear user-facing error.

        The library looks the id up in the list it fetched on the last poll and
        raises ``ValueError("Pending request not found")`` when it is gone. That
        happens when the request was already approved or denied elsewhere
        (typically from the Family Safety app) or expired, while the dashboard
        still showed it. Nothing was sent to Microsoft. Refresh so the stale
        request disappears from the entities, then explain what happened.
        """
        _LOGGER.info(
            "Cannot %s request %s: it is no longer pending (%s)",
            action, request_id, err,
        )
        await self.async_request_refresh()
        raise HomeAssistantError(
            f"Request {request_id} is no longer pending: it was probably already "
            "approved or denied from the Family Safety app, or it expired. "
            "The pending requests have been refreshed."
        ) from err

    async def _fetch_screentime_policy(self, account_id: str) -> dict | None:
        """Read a child's Windows schedule from the mobile API (None when unreadable)."""
        if self.web_api is None:
            return None
        return await self.web_api.get_screentime_policy(account_id)

    async def _set_screentime_allowance(
        self, child_id: str, day_of_week: int, hours: int, minutes: int
    ) -> bool:
        if self.web_api is None:
            raise RuntimeError("API not initialized")
        return await self.web_api.set_mobile_screentime_allowance(
            child_id, day_of_week, hours, minutes
        )

    async def _set_screentime_intervals(
        self, child_id: str, day_of_week: int, allowed_intervals: list[bool]
    ) -> bool:
        if self.web_api is None:
            raise RuntimeError("API not initialized")
        return await self.web_api.set_mobile_screentime_intervals(
            child_id, day_of_week, allowed_intervals
        )

    async def async_set_screentime_limit(
        self,
        child_id: str,
        day_of_week: int,
        hours: int,
        minutes: int,
        platform: str = "Windows",
    ) -> None:
        if platform == "Xbox":
            await self.async_set_xbox_screentime_limit(
                child_id, day_of_week, hours, minutes, require_xapprove=True
            )
            return
        if platform != "Windows":
            raise ValueError("platform must be Windows or Xbox")
        if not 0 <= day_of_week <= 6:
            raise ValueError("day_of_week must be between 0 and 6")
        if hours < 0 or minutes < 0 or minutes > 59 or hours * 60 + minutes > 1440:
            raise ValueError("screen-time allowance must be between 0 and 24 hours")
        await self._set_screentime_allowance(child_id, day_of_week, hours, minutes)
        self._update_cached_day(
            child_id, day_of_week, allowance=f"{hours:02d}:{minutes:02d}:00"
        )
        await self.async_request_refresh()

    async def async_set_screentime_intervals(
        self,
        child_id: str,
        day_of_week: int,
        start_hour: int,
        start_minute: int,
        end_hour: int,
        end_minute: int,
        platform: str = "Windows",
    ) -> None:
        if platform == "Xbox":
            await self.async_set_xbox_allowed_hours(
                child_id, day_of_week, start_hour, start_minute, end_hour, end_minute
            )
            return
        if platform != "Windows":
            raise ValueError("platform must be Windows or Xbox")
        if not 0 <= day_of_week <= 6:
            raise ValueError("day_of_week must be between 0 and 6")
        start_total = start_hour * 60 + start_minute
        end_total = end_hour * 60 + end_minute
        if not 0 <= start_total < end_total <= 1440:
            raise ValueError("screen-time interval end must be later than start")
        slots = _range_to_slots(start_hour, start_minute, end_hour, end_minute)
        await self._set_screentime_intervals(child_id, day_of_week, slots)
        self._update_cached_day(child_id, day_of_week, slots=slots)
        await self.async_request_refresh()

    def _update_cached_day(
        self,
        child_id: str,
        day_of_week: int,
        *,
        allowance: str | None = None,
        slots: list[bool] | None = None,
    ) -> None:
        """Mirror a write Microsoft accepted into the cached schedule.

        The refresh requested after a write is debounced. Until it runs, the
        number and time entities read the previous poll: setting a day's end
        right after its start would send the old start back to Microsoft and
        undo the first write (seen with a script setting both in a row).
        """
        account = self.account_data(child_id)
        policy = account.get("screentime_policy") if isinstance(account, dict) else None
        daily = policy.get("dailyRestrictions") if isinstance(policy, dict) else None
        if not isinstance(daily, dict):
            return
        day = daily.get(DAY_KEYS[day_of_week])
        if not isinstance(day, dict):
            return
        if allowance is not None:
            day["allowance"] = allowance
        if slots is not None:
            day["timeline"] = list(slots)
            day["allowedIntervals"] = list(slots)
            day.pop("allottedIntervals", None)
        self.async_update_listeners()

    async def async_set_app_time_limit(
        self,
        child_id: str,
        app_id: str,
        display_name: str,
        platform: str,
        allowance: str,
        start_time: str = "07:00:00",
        end_time: str = "22:00:00",
    ) -> None:
        if not self.web_api:
            raise RuntimeError("API not initialized")
        await self.web_api.set_app_time_limit(
            child_id, app_id, display_name, platform, allowance, start_time, end_time
        )
        await self.async_request_refresh()

    async def async_remove_app_time_limit(
        self, child_id: str, app_id: str, display_name: str, platform: str
    ) -> None:
        if not self.web_api:
            raise RuntimeError("API not initialized")
        await self.web_api.remove_app_time_limit(
            child_id, app_id, display_name, platform
        )
        await self.async_request_refresh()

    # --- Xbox extra time (add_time_bonus / cancel_time_bonus) -----------------
    #: How far the extra time Microsoft reads back may be from what was asked.
    _BONUS_TOLERANCE = timedelta(minutes=2)

    @staticmethod
    def _xapprove_until(value: Any) -> datetime | None:
        """An xapprove validUntil as UTC; one without an offset is UTC too."""
        parsed = dt_util.parse_datetime(str(value or ""))
        if parsed is None:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt_util.UTC)
        return dt_util.as_utc(parsed)

    @classmethod
    def _xapprove_override_of(
        cls, answer: dict[str, Any] | None, override_type: int
    ) -> dict | None:
        """The override of override_type in force in an xapprove answer, if any.

        Entries whose validUntil has passed are ignored; among several, the
        one ending last is returned (one without a readable end counts as in
        force and ending last).
        """
        now = dt_util.utcnow()
        best: dict | None = None
        best_until: datetime | None = None
        for override in (answer or {}).get("overrides") or []:
            if not isinstance(override, dict) or override.get("overrideType") != override_type:
                continue
            until = cls._xapprove_until(override.get("validUntil"))
            if until is not None and until <= now:
                continue
            if best is None or (
                best_until is not None and (until is None or until > best_until)
            ):
                best, best_until = override, until
        return best

    async def _async_bonus_xuid(self, account_id: str, platform: str) -> str:
        """The child's Xbox user id for extra time, or a clear error.

        Extra time goes through the Xbox Family Settings service only; Windows
        has no request for it that could be confirmed.
        """
        if str(platform).lower() != "xbox":
            raise HomeAssistantError("Extra time can only be given on the Xbox.")
        if not any(str(key) == str(account_id) for key in self._accounts):
            raise ValueError(f"Account {account_id} not found")
        xuid = await self._xapprove_xuid_for(account_id)
        if xuid is None:
            raise HomeAssistantError(
                "Xbox extra time needs Xbox among the monitored platforms and the Xbox "
                "Family Settings sign-in linked (Connection sensor: xbox_family), and "
                "the child must be on the Xbox family."
            )
        return xuid

    async def _async_xbox_overrides(self, xuid: str, what: str) -> dict[str, Any]:
        try:
            return await self.xbox_family.async_get_overrides(xuid)
        except XboxFamilyError as err:
            raise HomeAssistantError(
                f"Could not read the Xbox overrides, so {what}: {err}"
            ) from err

    async def async_add_time_bonus(
        self,
        account_id: str,
        platform: str,
        until: datetime,
        replace_lock: bool = False,
    ) -> dict[str, Any]:
        """Give a child extra time on the Xbox until ``until``.

        The Xbox Family Settings app's own "add time" (xapprove override 1).
        Microsoft keeps one Xbox override, so extra time replaces a lock: while
        one is in force (ours or a parent's) this is refused unless
        ``replace_lock``. It succeeds only when Microsoft's answer lists the
        extra time ending when asked and no lock left in force.
        """
        xuid = await self._async_bonus_xuid(account_id, platform)
        until = dt_util.as_utc(until)
        current = await self._async_xbox_overrides(xuid, "no extra time was given")
        lock = self._xapprove_override_of(current, OVERRIDE_BLOCK)
        if lock is not None and not replace_lock:
            raise HomeAssistantError(
                f"The Xbox is locked (until {lock.get('validUntil')}); extra time would "
                "lift that lock. Pass replace_lock: true to replace it."
            )
        try:
            answer = await self.xbox_family.async_create_override(xuid, OVERRIDE_ALLOW, until)
        except XboxFamilyError as err:
            raise HomeAssistantError(f"Could not give Xbox extra time: {err}") from err
        allow = self._xapprove_override_of(answer, OVERRIDE_ALLOW)
        valid_until = self._xapprove_until((allow or {}).get("validUntil"))
        if valid_until is None or abs(valid_until - until) > self._BONUS_TOLERANCE:
            _LOGGER.debug("Xbox extra time not confirmed by xapprove: answer %s", answer)
            raise HomeAssistantError(
                "Microsoft did not confirm the Xbox extra time "
                f"(asked until {until.isoformat()}, answer: {allow or 'no extra time'})."
            )
        key = (str(account_id), "xbox")
        self._xbox_lock_known.add(key[0])
        if self._xapprove_override_of(answer, OVERRIDE_BLOCK) is not None:
            await self.async_request_refresh()
            raise HomeAssistantError(
                "Microsoft recorded the Xbox extra time but the Xbox is still locked; "
                "unlock it with unlock_platform first."
            )
        # The extra time replaced any lock.
        self._platform_override_until.pop(key, None)
        await self.async_request_refresh()
        return {
            "added": True,
            "until": dt_util.as_local(valid_until).isoformat(),
            "replaced_lock": lock is not None,
        }

    async def async_cancel_time_bonus(self, account_id: str, platform: str) -> dict[str, Any]:
        """Cancel the Xbox extra time in force, and nothing else.

        The Xbox Family Settings app's revoke (override 100) is sent only when
        extra time is in force and no lock is: the revoke would lift a lock too,
        and a lock (from this integration or from a parent) is never touched
        here. With a lock in force this fails; with no extra time in force it
        does nothing and says so.
        """
        xuid = await self._async_bonus_xuid(account_id, platform)
        current = await self._async_xbox_overrides(xuid, "nothing was cancelled")
        if self._xapprove_override_of(current, OVERRIDE_BLOCK) is not None:
            raise HomeAssistantError(
                "The Xbox is locked, so nothing was cancelled: the revoke would lift "
                "the lock as well. Use unlock_platform to end the lock."
            )
        if self._xapprove_override_of(current, OVERRIDE_ALLOW) is None:
            _LOGGER.info("No Xbox extra time in force for account %s; nothing to cancel", account_id)
            return {"cancelled": False, "reason": "no_extra_time"}
        try:
            answer = await self.xbox_family.async_create_override(
                xuid, OVERRIDE_REVOKE, dt_util.utcnow() + timedelta(minutes=10)
            )
        except XboxFamilyError as err:
            raise HomeAssistantError(f"Could not cancel the Xbox extra time: {err}") from err
        await self.async_request_refresh()
        if self._xapprove_override_of(answer, OVERRIDE_ALLOW) is not None:
            raise HomeAssistantError("Microsoft left the Xbox extra time in place.")
        lock = self._xapprove_override_of(answer, OVERRIDE_BLOCK)
        if lock is not None:
            _LOGGER.warning(
                "After cancelling the Xbox extra time of account %s, Microsoft lists a "
                "lock until %s",
                account_id, lock.get("validUntil"),
            )
        return {"cancelled": True, "lock_in_force": lock is not None}

    # --- Xbox Family Settings API (xapprove) ---------------------------------
    @property
    def _xapprove_active(self) -> bool:
        """Xbox features on and an Xbox Family Settings token Microsoft has not refused."""
        return self.xbox_features and self.xbox_family.usable

    async def _async_xbox_xuid(self, account: Account) -> str | None:
        """The child's Xbox user id, matched by name on the Xbox family roster."""
        key = str(account.user_id)
        if key in self._xbox_xuids:
            return self._xbox_xuids[key]
        xuid = await self.xbox_family.async_child_xuid(
            getattr(account, "first_name", None), getattr(account, "surname", None)
        )
        if xuid:
            self._xbox_xuids[key] = xuid
            _LOGGER.info("Xbox Family Settings: a child was matched to their Xbox account")
            _LOGGER.debug("Xbox Family Settings: account %s is Xbox user %s", key, xuid)
        else:
            self._xbox_unmatched.add(key)
        return xuid

    async def _xapprove_xuid_for(self, child_id: str) -> str | None:
        """xuid for a write, or None when xapprove is not in use for this child."""
        if not self._xapprove_active:
            return None
        # Roster ids are int, entity and service ids may be str: compare as str.
        account = next(
            (a for key, a in self._accounts.items() if str(key) == str(child_id)), None
        )
        if account is None:
            _LOGGER.warning(
                "Xbox Family Settings: no roster account %s",
                child_id,
            )
            return None
        try:
            return await self._async_xbox_xuid(account)
        except XboxFamilyError as err:
            _LOGGER.warning("Xbox Family Settings lookup failed: %s", err)
            return None

    async def _xapprove_xuid_required(self, child_id: str) -> str:
        """xuid for a write that must go through xapprove, or a clear refusal.

        Used where no other path reaches the console: the Xbox schedule on the
        Family Safety mobile API accepts writes the console does not enforce.
        """
        if not self.xbox_features:
            raise HomeAssistantError(
                "Xbox is not enabled for this Microsoft Family Safety entry: tick Xbox "
                "in the integration options and link the Xbox Family Settings sign-in"
            )
        if not self.xbox_family.linked:
            raise HomeAssistantError(
                "The Xbox Family Settings sign-in is not linked: link it from the "
                "integration options"
            )
        if not self.xbox_family.usable:
            raise HomeAssistantError(
                "Microsoft refused the Xbox Family Settings sign-in: link it again "
                "from the integration options"
            )
        account = next(
            (a for key, a in self._accounts.items() if str(key) == str(child_id)), None
        )
        if account is None:
            raise HomeAssistantError(f"No child account {child_id} in this family")
        try:
            xuid = await self._async_xbox_xuid(account)
        except XboxFamilyAuthError as err:
            raise HomeAssistantError(
                "Microsoft refused the Xbox Family Settings sign-in: link it again "
                f"from the integration options ({err})"
            ) from err
        except XboxFamilyError as err:
            raise HomeAssistantError(f"Could not look up the child's Xbox account: {err}") from err
        if xuid is None:
            raise HomeAssistantError(
                f"Child account {child_id} could not be matched to an Xbox account: the "
                "first and last name must match exactly one child in the Xbox family"
            )
        return xuid

    async def _async_fetch_xapprove(
        self, account: Account, account_data: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Read the Xbox schedule, override and usage from xapprove.

        Puts the schedule on ``xbox_screentime_policy`` (what the Xbox numbers
        and switch read) and returns the usage and the override as the policy
        _apply_xbox_lock_state reads, or None when xapprove is not in use or
        did not answer. A refused token is retried every
        _XBOX_RETRY_SECONDS; the repair issue (see _async_sync_xbox_issue)
        asks for a new link, never for a new Microsoft sign-in.
        """
        if not self.xbox_features or not self.xbox_family.linked:
            return None
        if not self.xbox_family.usable and time.monotonic() < self._xbox_retry_at:
            return None
        try:
            xuid = await self._async_xbox_xuid(account)
            if xuid is None:
                return None
            schedule = await self.xbox_family.async_get_schedule(xuid)
            overrides = await self.xbox_family.async_get_overrides(xuid)
            try:
                usage = usage_today(
                    await self.xbox_family.async_get_usage(xuid),
                    dt_util.now().date().isoformat(),
                )
            except XboxFamilyError as err:
                _LOGGER.debug("Could not read Xbox usage from xapprove: %s", err)
                usage = None
        except XboxFamilyAuthError as err:
            # The mobile token still works: a full reauthentication would not
            # necessarily fix this (the Xbox link from the sign-in can need
            # consent) and would only loop. Ask for a new Xbox link instead.
            self._xbox_retry_at = time.monotonic() + _XBOX_RETRY_SECONDS
            if not self._xbox_auth_warned:
                self._xbox_auth_warned = True
                _LOGGER.warning(
                    "Xbox Family Settings sign-in no longer works (%s); link it again "
                    "from the integration options",
                    err,
                )
            return None
        except XboxFamilyError as err:
            _LOGGER.debug("Xbox Family Settings read failed this poll: %s", err)
            return None
        self._xbox_auth_warned = False
        account_data["xbox_screentime_policy"] = schedule_to_policy(schedule)
        account_data["xbox_source"] = "xapprove"
        return {
            "usage": usage,
            "lock_policy": overrides_to_policy(overrides),
            "override": parse_xbox_override(overrides),
        }

    def async_xbox_linked(self) -> None:
        """A manual link just stored a new token in this instance: use it now."""
        self._xbox_xuids.clear()
        self._xbox_unmatched.clear()
        self._xbox_auth_warned = False
        self._xbox_retry_at = 0.0
        self.hass.async_create_task(self.async_request_refresh())

    def _set_xbox_policy_field(self, child_id: str, update) -> None:
        account = self.account_data(child_id)
        policy = account.get("xbox_screentime_policy") if isinstance(account, dict) else None
        if isinstance(policy, dict):
            update(policy)
            self.async_update_listeners()

    async def async_set_xbox_screentime_limit(
        self,
        child_id: str,
        day_of_week: int,
        hours: int,
        minutes: int,
        *,
        require_xapprove: bool = False,
    ) -> None:
        """Set one day of the Xbox schedule (the one the console enforces).

        With ``require_xapprove`` the write is refused instead of falling back
        to the mobile Xbox schedule when xapprove is not available.
        """
        if not 0 <= day_of_week <= 6:
            raise ValueError("day_of_week must be between 0 and 6")
        if hours < 0 or minutes < 0 or minutes > 59 or hours * 60 + minutes > 1440:
            raise ValueError("screen-time allowance must be between 0 and 24 hours")
        if require_xapprove:
            xuid: str | None = await self._xapprove_xuid_required(child_id)
        else:
            if not self.web_api:
                raise RuntimeError("Web API not initialized")
            xuid = await self._xapprove_xuid_for(child_id)
        if xuid is not None:
            try:
                await self.xbox_family.async_set_day(
                    xuid, day_of_week, minutes=hours * 60 + minutes
                )
            except XboxFamilyError as err:
                raise HomeAssistantError(f"Could not set the Xbox limit: {err}") from err
        else:
            await self.web_api.set_mobile_xbox_screentime_allowance(
                child_id, day_of_week, hours, minutes
            )
        day = DAY_KEYS[day_of_week]

        def _update(policy: dict) -> None:
            entry = (policy.get("dailyRestrictions") or {}).get(day)
            if isinstance(entry, dict):
                entry["allowance"] = f"{hours:02d}:{minutes:02d}:00"

        self._set_xbox_policy_field(child_id, _update)
        await self.async_request_refresh()

    async def async_set_xbox_allowed_hours(
        self,
        child_id: str,
        day_of_week: int,
        start_hour: int,
        start_minute: int,
        end_hour: int,
        end_minute: int,
    ) -> None:
        """Set one day's Xbox allowed hours, keeping the day's allowance.

        Only through xapprove: the console does not enforce the Xbox schedule
        of the Family Safety mobile API.
        """
        if not 0 <= day_of_week <= 6:
            raise ValueError("day_of_week must be between 0 and 6")
        if start_minute not in (0, 30) or end_minute not in (0, 30):
            raise ValueError("Xbox allowed hours start and end on the hour or half hour")
        start_total = start_hour * 60 + start_minute
        end_total = end_hour * 60 + end_minute
        if not 0 <= start_total < end_total <= 1440:
            raise ValueError(
                "Xbox allowed hours must end later the same day than they start "
                "(24:00 at the latest)"
            )
        xuid = await self._xapprove_xuid_required(child_id)
        try:
            await self.xbox_family.async_set_day(
                xuid, day_of_week, intervals=[(start_total, end_total)]
            )
        except XboxFamilyError as err:
            raise HomeAssistantError(f"Could not set the Xbox allowed hours: {err}") from err
        day = DAY_KEYS[day_of_week]

        def _update(policy: dict) -> None:
            entry = (policy.get("dailyRestrictions") or {}).get(day)
            if isinstance(entry, dict):
                entry["allowedIntervals"] = [
                    {
                        "begin": f"{start_total // 60:02d}:{start_total % 60:02d}",
                        "end": f"{end_total // 60:02d}:{end_total % 60:02d}",
                    }
                ]

        self._set_xbox_policy_field(child_id, _update)
        await self.async_request_refresh()

    async def async_set_xbox_schedule_enabled(self, child_id: str, enabled: bool) -> None:
        """Turn the Xbox schedule on or off, keeping its days as they are."""
        if not self.web_api:
            raise RuntimeError("API not initialized")
        xuid = await self._xapprove_xuid_for(child_id)
        if xuid is not None:
            # xapprove takes isEnabled with one day; send today unchanged.
            today = (dt_util.now().weekday() + 1) % 7  # DAY_KEYS: 0 = Sunday
            try:
                await self.xbox_family.async_set_day(xuid, today, enabled=enabled)
            except XboxFamilyError as err:
                raise HomeAssistantError(f"Could not switch the Xbox limits: {err}") from err
        else:
            await self.web_api.set_mobile_schedule_enabled(child_id, enabled, platform="Xbox")

        def _update(policy: dict) -> None:
            policy["isEnabled"] = bool(enabled)

        self._set_xbox_policy_field(child_id, _update)
        await self.async_request_refresh()

    async def async_set_app_limits_enabled(self, child_id: str, enabled: bool) -> None:
        if not self.web_api:
            raise RuntimeError("API not initialized")
        await self.web_api.set_app_limits_enabled(child_id, enabled)
        # Microsoft accepted it: show the new state now. The refresh below is
        # debounced, and until it runs the switch would fall back to the value
        # of the previous poll.
        account = self.account_data(child_id)
        if isinstance(account, dict):
            account["app_limits_enabled"] = bool(enabled)
            self.async_update_listeners()
        await self.async_request_refresh()

    async def _async_change_websites(
        self, service: str, child_id: str, websites: str | list[str]
    ) -> dict[str, list[str]]:
        """Send one website change per site (each is its own PATCH).

        Every site is tried, also after one fails, and a failure fails the
        call naming the sites done and the sites failed, so a caller knows
        exactly which ones to retry. On success the answer lists the sites
        done (``failed`` is then empty).
        """
        if not self.web_api:
            raise RuntimeError("Web API not initialized")
        write = getattr(self.web_api, service)
        given = [websites] if isinstance(websites, str) else list(websites)
        # One change per site as Microsoft stores it: "example.com",
        # "https://example.com" and "EXAMPLE.com" are the same site, and the
        # first spelling given is the one sent.
        sites: dict[str, str] = {}
        for site in given:
            site = str(site or "").strip()
            if site:
                sites.setdefault(FamilySafetyWebAPI._website_url(site).lower(), site)
        if not sites:
            raise ServiceValidationError(f"{service}: give at least one website.")
        done: list[str] = []
        failed: dict[str, str] = {}
        for site in sites.values():
            try:
                await write(child_id, site)
            except Exception as err:  # noqa: BLE001 - reported with the others
                failed[site] = str(err)[:200]
            else:
                done.append(site)
        if done:
            await self.async_request_refresh()
        if failed:
            raise HomeAssistantError(
                f"{service}: {len(failed)} of {len(done) + len(failed)} site(s) failed. "
                f"Done: {', '.join(done) or 'none'}. Failed: "
                + "; ".join(f"{site} ({error})" for site, error in failed.items())
            )
        return {"done": done, "failed": []}

    async def async_block_website(
        self, child_id: str, website: str | list[str]
    ) -> dict[str, list[str]]:
        return await self._async_change_websites("block_website", child_id, website)

    async def async_remove_website(
        self, child_id: str, website: str | list[str]
    ) -> dict[str, list[str]]:
        return await self._async_change_websites("remove_website", child_id, website)

    async def async_allow_website(
        self, child_id: str, website: str | list[str]
    ) -> dict[str, list[str]]:
        return await self._async_change_websites("allow_website", child_id, website)

    async def async_get_sites(self, account_id: str) -> dict[str, Any]:
        """The child's blocked and allowed websites as Microsoft stores them."""
        if not self.web_api:
            raise HomeAssistantError("Web API not initialized")
        account_id = str(account_id)
        try:
            web = await self.web_api.get_web_restrictions(account_id)
        except Exception as err:  # noqa: BLE001 - reported as the service error
            raise HomeAssistantError(
                f"Could not read the child's websites from Microsoft: {err}"
            ) from err
        lists = _stored_site_lists(web)
        if lists is None:
            raise HomeAssistantError(
                "Microsoft's answer has no list of blocked and allowed websites."
            )
        return {
            "account_id": account_id,
            "blocked": lists[0],
            "allowed": lists[1],
            "filter_enabled": _web_enabled(web),
        }

    def web_filter_enabled(self, account_id: Any) -> bool | None:
        """Whether the child's web filter (Microsoft Edge) is on; None if unknown."""
        return _web_enabled((self.account_data(account_id) or {}).get("web_browsing"))

    async def async_toggle_web_filter(self, child_id: str, enabled: bool) -> None:
        if not self.web_api:
            raise RuntimeError("API not initialized")
        await self.web_api.toggle_web_filter(child_id, enabled)
        # Show the new state at once; the next read confirms it.
        account = self.account_data(child_id)
        if isinstance(account, dict) and isinstance(account.get("web_browsing"), dict):
            account["web_browsing"] = {**account["web_browsing"], "enabled": bool(enabled)}
            self.async_update_listeners()
        await self.async_request_refresh()

    async def async_set_age_rating(self, child_id: str, age: int) -> None:
        if not self.web_api:
            raise RuntimeError("API not initialized")
        await self.web_api.set_age_rating(child_id, age)
        await self.async_request_refresh()

    async def async_set_acquisition_policy(self, child_id: str, require_approval: bool) -> None:
        if not self.web_api:
            raise RuntimeError("API not initialized")
        await self.web_api.set_acquisition_policy(child_id, require_approval)
        await self.async_request_refresh()

    @staticmethod
    def _windows_usage(data: dict[str, Any], account: dict[str, Any]) -> int | None:
        """Today's minutes on the child's Windows devices; None without one."""
        devices = data.get("devices") or {}
        total: int | None = None
        for device_id in account.get("devices") or []:
            device = devices.get(device_id) or {}
            kind = f"{device.get('os_name') or ''} {device.get('device_class') or ''}".lower()
            if "windows" in kind and "xbox" not in kind:
                total = (total or 0) + int(device.get("today_time_used") or 0)
        return total

    def platform_status(
        self, account_id: Any, platform: str, now: datetime | None = None
    ) -> dict[str, Any]:
        """Whether ``platform`` (Windows or Xbox) is allowed now, and for how long.

        Only reads what the last update holds (see
        screen_time.platform_allowance). The Xbox uses its own schedule, the
        Xbox play time and the override in the Xbox policy; Windows uses the
        Windows schedule and the time of the child's Windows devices. A lock
        listed in ``blocked_platforms`` counts for both.
        """
        data = self.data or {}
        account = (data.get("accounts") or {}).get(account_id) or {}
        if platform.lower() == "xbox":
            policy = account.get("xbox_screentime_policy")
            used = account.get("xbox_today_usage")
            current = xbox_policy_override(account.get("xbox_policy"))
        else:
            policy = account.get("screentime_policy")
            used = self._windows_usage(data, account)
            current = None
        if platform in (account.get("blocked_platforms") or []) and (
            current is None or current.get("type") != BLOCK_UNTIL
        ):
            until = self._platform_override_until.get((str(account_id), platform.lower()))
            current = make_override(BLOCK_UNTIL, until)
        return platform_allowance(
            policy if isinstance(policy, dict) else None,
            used if isinstance(used, int) else None,
            now or dt_util.now(),
            current,
        )

    def is_account_locked(self, account_id: str) -> bool | None:
        account = self.account_data(account_id)
        if not account:
            return None
        policy = account.get("screentime_policy")
        if not isinstance(policy, dict):
            return None
        daily = policy.get("dailyRestrictions") or policy.get("DailyRestrictions")
        if not isinstance(daily, dict):
            return None
        for day_key in DAY_KEYS:
            day_data = daily.get(day_key) or daily.get(day_key.capitalize())
            if not isinstance(day_data, dict):
                return None
            allowance = day_data.get("allowance") or day_data.get("Allowance") or "00:00:00"
            if allowance != "00:00:00":
                return False
        return True

    def _read_failure_hint(self) -> str:
        """Explain a failed schedule read in terms the user can act on."""
        status = self.web_api.screentime_policy_status if self.web_api else None
        if status == "network_error":
            return "Microsoft did not answer (network error or timeout): try again in a few minutes."
        return "Check the Microsoft Family Safety connection sensor for details."

    def _account_lock(self, account_id: str) -> asyncio.Lock:
        return self._account_locks.setdefault(self._policy_key(account_id), asyncio.Lock())

    @staticmethod
    def _has_nonzero_allowance(policy: dict[str, Any]) -> bool:
        """True when at least one day of the policy grants time.

        Use `or` chains rather than dict.get defaults: Microsoft may send an
        explicit {"allowance": None}, and dict.get would then return None
        instead of the default, making an unusable policy look non-zero and
        saving it as the restore point.
        """
        daily = policy.get("dailyRestrictions") or policy.get("DailyRestrictions") or {}
        return any(
            (
                (daily.get(key) or daily.get(key.capitalize()) or {}).get("allowance")
                or (daily.get(key) or daily.get(key.capitalize()) or {}).get("Allowance")
                or "00:00:00"
            )
            != "00:00:00"
            for key in DAY_KEYS
        )

    async def _save_restore_point(self, account_id: str, operation: str) -> None:
        """Read the current schedule and keep it as the restore point.

        Refuses, before any write, when the schedule cannot be read and
        nothing was saved earlier, or when it is already all zeros: the
        restore point is the only way back from the rewrite that follows, and
        an invented or zeroed one would wipe the child's real schedule
        unrecoverably (issue #23).
        """
        key = self._policy_key(account_id)
        current_policy = await self._fetch_screentime_policy(account_id)
        # A restore point is only worth something if `_saved_week` can read
        # it back; otherwise the unlock would refuse and the child stay at 0.
        saved_ok = self._saved_week(self._saved_screentime.get(key) or {}) is not None
        if current_policy:
            if self._has_nonzero_allowance(current_policy):
                if self._saved_week(current_policy) is None:
                    raise UpdateFailed(
                        f"Cannot {operation} account {account_id}: the current schedule "
                        "has a day this integration cannot read back, so it could not "
                        "be restored afterwards."
                    )
                self._saved_screentime[key] = current_policy
                # A new restore point supersedes any earlier release.
                self._released_screentime.discard(key)
                await self._async_save_screentime()
            elif not saved_ok:
                raise UpdateFailed(
                    f"Account {account_id} already has every day set to zero and no "
                    "readable saved schedule exists, so there would be nothing to "
                    "restore later. Set the daily limits first, from Home Assistant "
                    "or at account.microsoft.com/family."
                )
        elif not saved_ok:
            raise UpdateFailed(
                f"Cannot {operation} account {account_id}: current schedule unreadable "
                f"and no readable saved policy exists. {self._read_failure_hint()}"
            )
        else:
            _LOGGER.warning(
                "%s account %s from a previously saved schedule: the current "
                "schedule could not be read, so the restore point may be stale",
                operation.capitalize(), account_id,
            )

    async def _write_week(
        self,
        account_id: str,
        days: list[tuple[int, int, list[bool]]],
        operation: str,
    ) -> None:
        """Write (hours, minutes, slots) for the 7 days, Sunday first.

        Each day is two writes that can each wait out the full read timeout.
        When a failure is a network one, Microsoft is not answering at all:
        the remaining days would only burn minutes, so stop there.
        The caller keeps its restore point on failure so it can retry.
        """
        failures = 0
        for day_index, (hours, minutes, slots) in enumerate(days):
            try:
                await self._set_screentime_allowance(account_id, day_index, hours, minutes)
                await self._set_screentime_intervals(account_id, day_index, slots)
            except Exception as err:
                failures += 1
                _LOGGER.warning("Could not %s day %d: %s", operation, day_index, err)
                if getattr(err, "transient", False):
                    failures = 7 - day_index
                    _LOGGER.warning(
                        "Stopping %s for account %s after day %d: Microsoft is not "
                        "answering; the saved schedule is kept for retry",
                        operation, account_id, day_index,
                    )
                    break
        if failures:
            raise UpdateFailed(
                f"Screen-time {operation} updated {7 - failures}/7 weekdays; "
                "saved policy retained for retry"
            )

    async def _set_schedule_flag(self, account_id: str, enabled: bool) -> None:
        """Set the weekly schedule's own "enabled" flag when it differs.

        Microsoft only enforces the days while that flag is on, so a lock
        written while it is off would show as applied and do nothing; a
        restore puts the flag back as it was before the lock.
        """
        account = self.account_data(account_id) or {}
        policy = account.get("screentime_policy")
        current = policy.get("isEnabled") if isinstance(policy, dict) else None
        if current is enabled or self.web_api is None:
            return
        try:
            await self.web_api.set_mobile_schedule_enabled(account_id, enabled)
        except Exception as err:  # noqa: BLE001 - the week was written already
            _LOGGER.warning(
                "Schedule written for %s but its enabled flag could not be set to %s: %s",
                account_id, enabled, err,
            )

    async def async_lock_account(self, account_id: str) -> None:
        """Lock an account by zeroing every day's allowance.

        Microsoft exposes no real "lock" endpoint, so a lock is a destructive
        rewrite of the whole weekly schedule; see `_save_restore_point`.
        """
        async with self._account_lock(account_id):
            await self._save_restore_point(account_id, "lock")
            await self._write_week(account_id, [(0, 0, [False] * 48)] * 7, "account lock")
            await self._set_schedule_flag(account_id, True)
        await self.async_request_refresh()

    @staticmethod
    def _saved_week(saved: dict[str, Any]) -> list[tuple[int, int, list[bool]]] | None:
        """Parse a restore point into 7 (hours, minutes, slots); None if any day is unreadable."""
        daily = saved.get("dailyRestrictions") or saved.get("DailyRestrictions") or {}
        days = []
        for day_key in DAY_KEYS:
            day_data = daily.get(day_key) or daily.get(day_key.capitalize()) or {}
            if not isinstance(day_data, dict):
                return None
            allowance = day_data.get("allowance") or day_data.get("Allowance")
            slots = _policy_intervals_to_slots(day_data)
            if not isinstance(allowance, str) or slots is None:
                return None
            # "HH:MM:SS", or the .NET TimeSpan "D.HH:MM:SS" ("1.00:00:00" = 24 h).
            days_part = "0"
            if "." in allowance.split(":")[0]:
                days_part, _, allowance = allowance.partition(".")
            try:
                hours, minutes = (int(part) for part in allowance.split(":")[:2])
                hours += 24 * int(days_part)
            except ValueError:
                return None
            days.append((hours, minutes, slots))
        return days

    async def async_unlock_account(
        self, account_id: str, *, operation: str = "unlock"
    ) -> None:
        """Restore the saved schedule.

        ``operation`` only shapes the error message: the same code path
        serves the account-lock switch ("unlock") and the screen-time
        limits switch ("re-enable screen time limits for"), and telling a
        user they cannot "unlock" a toggle they never locked is confusing.
        """
        async with self._account_lock(account_id):
            await self._restore_saved(account_id, operation)
        await self.async_request_refresh()

    async def _restore_saved(
        self, account_id: str, operation: str, *, enabled: bool | None = None
    ) -> None:
        """Write the restore point back; ``enabled`` forces the flag, else the saved one."""
        key = self._policy_key(account_id)
        saved = self._saved_screentime.get(key)
        # No restore point, or one with a day this build cannot read: writing
        # a default in its place would silently replace a schedule nobody
        # asked to change, and the real one would be gone for good. That is
        # the same destructive write the lock guard refuses (issue #23).
        days = self._saved_week(saved) if saved else None
        if days is None:
            raise UpdateFailed(
                f"Cannot {operation} account {account_id}: "
                + ("the saved schedule is unreadable" if saved else "no saved schedule to restore")
                + ". Set the daily limits again from Home Assistant or at "
                "account.microsoft.com/family; the integration will not guess a "
                "schedule for you."
            )
        # Keeps the only copy of the pre-lock schedule after a partial restore.
        await self._write_week(account_id, days, "restore")
        self._saved_screentime.pop(key, None)
        self._released_screentime.add(key)
        await self._async_save_screentime()
        if enabled is None:
            enabled = saved.get("isEnabled") is not False
        await self._set_schedule_flag(account_id, enabled)

    def is_policy_enabled(self, account_id: str) -> bool | None:
        locked = self.is_account_locked(account_id)
        if locked is None:
            return None
        if locked:
            # All days at 0 → fully locked, which is still "limits enabled".
            return True
        account = self.account_data(account_id)
        policy = (account or {}).get("screentime_policy")
        if not isinstance(policy, dict):
            return None
        if policy.get("isEnabled") is False:
            return False
        daily = policy.get("dailyRestrictions") or policy.get("DailyRestrictions")
        if not isinstance(daily, dict):
            return None
        for day_key in DAY_KEYS:
            day_data = daily.get(day_key) or daily.get(day_key.capitalize()) or {}
            allowance = day_data.get("allowance") or day_data.get("Allowance") or "24:00:00"
            if allowance != "24:00:00":
                return True
        return False

    async def async_set_policy_enabled(self, account_id: str, enabled: bool) -> None:
        # The app's weekly switch is the schedule's own "enabled" flag: Microsoft
        # keeps every day as it is. The 24 h rewrite below is the fallback when
        # Microsoft refuses the flag, and a restore point it left behind is
        # still restored on enable.
        async with self._account_lock(account_id):
            mobile_ok = False
            if self.web_api is not None:
                try:
                    await self.web_api.set_mobile_schedule_enabled(account_id, enabled)
                    mobile_ok = True
                except Exception as err:  # noqa: BLE001 - fall back to the 24 h rewrite
                    _LOGGER.warning("Mobile schedule on/off failed, rewriting the days: %s", err)
            has_saved = self._policy_key(account_id) in self._saved_screentime
            if enabled:
                # The flag is on; a restore point left by the fallback (or a
                # lock) is restored too. Without one, the flag alone
                # was the whole job.
                if has_saved:
                    await self._restore_saved(
                        account_id, "re-enable screen time limits for", enabled=True
                    )
                elif not mobile_ok:
                    raise UpdateFailed(
                        f"Cannot re-enable screen time limits for account {account_id}: "
                        "Microsoft refused the mobile request and no saved schedule exists"
                    )
            elif not mobile_ok:
                await self._save_restore_point(account_id, "disable screen-time limits for")
                await self._write_week(
                    account_id, [(24, 0, [True] * 48)] * 7, "policy disable"
                )
        await self.async_request_refresh()

    def connection_state(self) -> dict[str, Any]:
        mobile_ok = self.api is not None and self.last_update_success
        # A missing or refused Xbox Family Settings link only takes the Xbox
        # features down; everything else keeps running on the mobile token.
        xbox_down = self.xbox_features and not self.xbox_family.usable
        state = "disconnected" if not mobile_ok else "degraded" if xbox_down else "connected"
        return {
            "state": state,
            "mobile_api": "ok" if mobile_ok else "error",
            "roster_errors": self._roster_errors_by_account(),
            "screentime_policy_status": (
                self.web_api.screentime_policy_status if self.web_api else "unavailable"
            ),
            "xbox_features": self.xbox_features,
            **self.xbox_family.diagnostics(),
            "xbox_family_matched_children": len(self._xbox_xuids),
            "xbox_family_unmatched_children": len(self._xbox_unmatched - set(self._xbox_xuids)),
            "runtime_credentials_persisted": bool(self._runtime_auth_state),
            "reauth_requested": self._reauth_requested,
            "reauth_required": self._reauth_requested,
            "reauth_reason": self._reauth_reason,
            "last_update_success": self.last_update_success,
        }

    async def _fetch_web_api_data(self, account_id: str) -> dict[str, Any]:
        """Per-child settings beyond pyfamilysafety, all from the mobile API."""
        result: dict[str, Any] = {
            "web_browsing": None,
            "screentime_policy": None,
        }
        if not self.web_api:
            return result
        try:
            result["web_browsing"] = await self.web_api.get_web_browsing_settings(account_id)
        except Exception as err:
            _LOGGER.debug("Could not fetch web browsing settings: %r", err)
        try:
            # Fetched separately so age-rating and acquisition-policy state are
            # visible instead of exposing write-only services with no readback.
            result["content_settings"] = await self.web_api.get_content_settings(account_id)
        except Exception as err:
            _LOGGER.debug("Could not fetch content settings: %r", err)
        try:
            result["app_limits_enabled"] = await self.web_api.get_app_limits_enabled(
                account_id
            )
        except Exception as err:
            _LOGGER.debug("Could not fetch the app limits switch: %r", err)
        if time.monotonic() >= self._web_activity_next.get(account_id, 0.0):
            self._web_activity_next[account_id] = time.monotonic() + 1800
            try:
                result["web_activity"] = await self.web_api.get_web_activity(account_id)
            except Exception as err:
                _LOGGER.debug("Could not fetch web activity: %r", err)
        result["screentime_policy"] = await self._fetch_screentime_policy(account_id)
        if self._mobile_only:
            return result
        try:
            result["xbox_screentime_policy"] = (
                await self.web_api.get_mobile_xbox_screentime_policy(account_id)
            )
        except Exception as err:
            _LOGGER.debug("Could not fetch the Xbox schedule: %r", err)
        return result

    def _request_reauth(self, reason: str) -> None:
        """Ask Home Assistant to start a linked reauthentication flow.

        ConfigEntry.async_start_reauth() de-duplicates active flows itself. Calling
        it again on later polls means a deliberately aborted/expired repair can be
        offered again without requiring a Home Assistant restart.
        """
        if not self._reauth_requested:
            _LOGGER.warning(
                "Microsoft Family Safety authentication requires renewal (%s)", reason
            )
        self._reauth_requested = True
        self._reauth_reason = reason
        self.entry.async_start_reauth(self.hass, data={"reason": reason})

    async def async_request_reauth(self) -> None:
        """Service entry point: open the Microsoft sign-in flow now."""
        await self._create_auth_notification()
        self._request_reauth("manual")

    def _transform_account_data(self, account: Account) -> tuple[str, dict[str, Any]]:
        account_id = account.user_id
        today = dt_util.now().date().isoformat()
        raw_ms = int(account.today_screentime_usage or 0)
        raw_minutes = _ms_to_minutes(raw_ms)
        polled_at = dt_util.now().isoformat()

        # Expose Microsoft's current aggregate directly.  Do not clamp the
        # value to a locally remembered maximum: Home Assistant's recorder and
        # history UI are responsible for historical presentation, while the
        # entity state should reflect the latest API response verbatim.
        data = {
            "user_id": account.user_id,
            "first_name": account.first_name,
            "surname": account.surname,
            "profile_picture": account.profile_picture,
            "today_screentime_usage": raw_minutes,
            "raw_today_screentime_usage": raw_minutes,
            "raw_today_screentime_ms": raw_ms,
            "screen_time_polled_at": polled_at,
            "screen_time_date": today,
            "average_screentime_usage": _ms_to_minutes(account.average_screentime_usage),
            "account_balance": account.account_balance,
            "account_currency": account.account_currency,
            "blocked_platforms": self._merge_blocked_platforms(account),
            # Endpoints Microsoft could not resolve for this member on the
            # last poll (see _pyfamilysafety_compat._patch_account_roster_tolerance).
            "roster_errors": dict(getattr(account, "roster_errors", None) or {}),
            "devices": [],
            "applications": [
                {
                    "app_id": app.app_id,
                    "app_name": app.name,
                    "blocked": app.blocked,
                    "icon": app.icon,
                    "usage_minutes": round(app.usage, 1) if app.usage else 0,
                }
                for app in account.applications
            ],
        }
        return account_id, data

    def _transform_device_data(self, device: Device, account_id: str) -> tuple[str, dict[str, Any]]:
        return device.device_id, {
            "device_id": device.device_id,
            "device_name": device.device_name,
            "device_class": device.device_class,
            "device_make": device.device_make,
            "device_model": device.device_model,
            "os_name": device.os_name,
            "today_time_used": _ms_to_minutes(device.today_time_used),
            "last_seen": device.last_seen,
            "blocked": device.blocked,
            "account_id": account_id,
        }

    async def _async_update_data(self) -> dict[str, Any]:
        if self.api is None:
            await self._async_setup_api()
        assert self.api is not None
        try:
            await self.api.update()
            if not getattr(self.api, "accounts", None):
                self.api.accounts = []
            accounts_data: dict[str, Any] = {}
            devices_data: dict[str, Any] = {}
            #: account id -> Xbox override read from xapprove this poll, and
            #: when that read started (strict mode).
            xbox_overrides: dict[str, Any] = {}
            xbox_read_at: dict[str, datetime] = {}
            new_accounts: dict[str, Account] = {}
            new_devices: dict[str, Device] = {}
            for account in self.api.accounts:
                account_id, account_data = self._transform_account_data(account)
                accounts_data[account_id] = account_data
                new_accounts[account_id] = account
                for device in account.devices:
                    device_id, device_data = self._transform_device_data(device, account_id)
                    devices_data[device_id] = device_data
                    accounts_data[account_id]["devices"].append(device_id)
                    new_devices[device_id] = device
                web_data = await self._fetch_web_api_data(account_id)
                # A transient failure of one endpoint must not turn an
                # otherwise healthy entity into `unknown` for the next poll.
                # Keep the previous successful value in memory until Microsoft
                # returns a replacement.  This is not a screen-time usage guard:
                # it applies only to configuration/policy payloads and never
                # modifies measured usage values.
                previous = ((self.data or {}).get("accounts", {}).get(account_id) or {})
                for key in ("web_browsing", "content_settings", "screentime_policy", "app_limits_enabled", "xbox_screentime_policy", "web_activity"):
                    if web_data.get(key) is None and previous.get(key) is not None:
                        web_data[key] = previous.get(key)
                accounts_data[account_id].update(web_data)
                # With the Xbox Family Settings sign-in linked, the Xbox
                # schedule, lock state and play time come from xapprove.
                xapprove_read_at = dt_util.utcnow()
                xapprove = await self._async_fetch_xapprove(account, accounts_data[account_id])
                accounts_data[account_id]["xbox_override"] = (
                    xapprove.get("override") if xapprove else None
                )
                if xapprove and xapprove.get("override") is not None:
                    xbox_overrides[str(account_id)] = xapprove["override"]
                    xbox_read_at[str(account_id)] = xapprove_read_at
                # Xbox usage: since about 2026-08-20 the mobile aggregator
                # reports 0 (or a stale figure) for Xbox play on some accounts.
                today_iso = dt_util.now().date().isoformat()
                xbox_usage: int | None = xapprove.get("usage") if xapprove else None
                if xbox_usage is None:
                    # Keep the last known Xbox figure across a transient miss,
                    # but only within the same day (avoid carrying yesterday's
                    # total past midnight before today's row appears).
                    prev_xbox = previous.get("xbox_today_usage")
                    if (
                        isinstance(prev_xbox, int)
                        and previous.get("screen_time_date") == today_iso
                    ):
                        xbox_usage = prev_xbox
                accounts_data[account_id]["xbox_today_usage"] = xbox_usage
                # The account figure covers every platform, the Xbox report only
                # Xbox, so it replaces the account figure only when it is
                # higher: a child who also uses Windows keeps the mobile total,
                # and an account the aggregator now reports as 0 gets its Xbox
                # time back. Never lower a reported usage figure. The raw_*
                # fields keep exactly what the mobile aggregator returned.
                mobile_usage = accounts_data[account_id].get("today_screentime_usage")
                if xbox_usage is not None and xbox_usage > (mobile_usage or 0):
                    accounts_data[account_id]["today_screentime_usage"] = xbox_usage
                # The lock state comes from a fresh read only. The payload is
                # kept on the account data (last good one on a miss).
                xbox_policy: dict | None = xapprove.get("lock_policy") if xapprove else None
                child_key = str(account_id)
                self._apply_xbox_lock_state(
                    account_id,
                    accounts_data[account_id],
                    xbox_policy,
                    no_xbox=child_key in self._xbox_unmatched
                    and child_key not in self._xbox_xuids,
                )
                accounts_data[account_id]["xbox_policy"] = (
                    xbox_policy if xbox_policy is not None else previous.get("xbox_policy")
                )
            await self._async_sync_roster_notification(accounts_data)
            self._async_sync_xbox_issue()
            if not self._first_update_done:
                self._first_update_done = True
                # A reauthentication reloads the entry: the notification it
                # answered was raised by the previous coordinator.
                self._dismiss_auth_notification()
            self._accounts = new_accounts
            try:
                await self._async_prune_platform_holds()
                await self._async_enforce_holds(xbox_overrides, xbox_read_at)
            except Exception as err:  # noqa: BLE001 - never fail the poll for it
                _LOGGER.debug("Platform hold check failed: %r", err)
            self._devices = new_devices
            if self.app_usage_sensors > 0:
                self._fire_app_usage_events(accounts_data)
            pending = getattr(self.api, "pending_requests", None) or []
            self._fire_request_events(pending, accounts_data)
            self._is_retrying_auth = False
            return {"accounts": accounts_data, "devices": devices_data, "pending_requests": pending}
        except HttpException as err:
            if "401" in str(err) or "authentication" in str(err).lower():
                if not self._is_retrying_auth:
                    self._is_retrying_auth = True
                    self.web_api = None
                    raise ConfigEntryAuthFailed(ERROR_TOKEN_EXPIRED) from err
                raise UpdateFailed(f"Authentication failed: {err}") from err
            raise UpdateFailed(f"Error communicating with API: {err}") from err
        except ConfigEntryAuthFailed:
            raise
        except Exception as err:
            _LOGGER.exception("Unexpected error fetching Family Safety data")
            raise UpdateFailed(f"Unexpected error: {err}") from err
        finally:
            try:
                await self._async_persist_current_auth()
            except Exception as persist_err:
                _LOGGER.debug(
                    "Could not persist rotated Family Safety credentials: %s",
                    persist_err,
                )

    def _fire_request_events(
        self, pending: list[Any], accounts_data: dict[str, Any]
    ) -> None:
        """Fire microsoft_family_safety_request for each request new since the last poll.

        The fields are Microsoft's as pyfamilysafety returns them (puid, id,
        type, platform, requestedTime, appId / displayName when present);
        requested_time is passed on as Microsoft gives it, since its unit is
        not documented.
        """
        requests = [r for r in pending if isinstance(r, dict) and r.get("id") is not None]
        ids = {str(r["id"]) for r in requests}
        seen = self._seen_request_ids
        tracked: dict[str, int] = {request_id: 0 for request_id in ids}
        for request_id, misses in (seen or {}).items():
            if request_id not in ids and misses + 1 < _REQUEST_FORGET_POLLS:
                tracked[request_id] = misses + 1
        self._seen_request_ids = tracked
        if seen is None:
            return
        for request in requests:
            if str(request["id"]) in seen:
                continue
            puid = request.get("puid")
            account_id = str(puid) if puid is not None else None
            account = accounts_data.get(account_id) or accounts_data.get(puid) or {}
            self.hass.bus.async_fire(
                EVENT_REQUEST,
                {
                    "entry_id": self.entry.entry_id,
                    "account_id": account_id,
                    "first_name": account.get("first_name"),
                    "request_id": str(request["id"]),
                    "type": request.get("type"),
                    "platform": request.get("platform"),
                    "app_id": request.get("appId"),
                    "app_name": request.get("displayName") or request.get("appName"),
                    "requested_time": request.get("requestedTime"),
                },
            )

    def _fire_app_usage_events(self, accounts_data: dict[str, Any]) -> None:
        """Fire microsoft_family_safety_app_usage for each app that grew since the last poll."""
        for account_id, account in accounts_data.items():
            for grew in self._app_usage_tracker.update(
                account_id, account.get("screen_time_date") or "", account.get("applications")
            ):
                self.hass.bus.async_fire(
                    EVENT_APP_USAGE,
                    {
                        "entry_id": self.entry.entry_id,
                        "account_id": str(account_id),
                        "first_name": account.get("first_name"),
                        **grew,
                    },
                )

    async def _create_auth_notification(self) -> None:
        if self._auth_notification_sent:
            return
        message = (
            "Home Assistant has started a reauthentication flow. Open the integration "
            "or the Repairs page and complete the Microsoft sign-in to renew the "
            "integration's sign-in."
        )
        await self.hass.services.async_call(
            "persistent_notification",
            "create",
            {"title": "Microsoft Family Safety - Authentication Required", "message": message,
             "notification_id": AUTH_NOTIFICATION_ID},
        )
        self._auth_notification_sent = True

    def _dismiss_auth_notification(self) -> None:
        persistent_notification.async_dismiss(self.hass, AUTH_NOTIFICATION_ID)
        self._auth_notification_sent = False

    @property
    def xbox_issue_id(self) -> str:
        return f"xbox_link_{self.entry.entry_id}"

    def _async_sync_xbox_issue(self) -> None:
        """Raise a repair issue while the Xbox features lack a working Xbox link.

        Without it the Xbox lock fails and Xbox play time is not read, which
        an upgraded entry that never got the link would otherwise not notice.
        """
        if not self.xbox_features or self.xbox_family.usable:
            ir.async_delete_issue(self.hass, DOMAIN, self.xbox_issue_id)
            return
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            self.xbox_issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=(
                "xbox_link_refused" if self.xbox_family.linked else "xbox_not_linked"
            ),
            translation_placeholders={"entry_title": self.entry.title},
        )

    def _roster_errors_by_account(self) -> dict[str, list[str]]:
        """Return {account_id: [endpoint, ...]} for members Microsoft cannot fully resolve."""
        accounts = (self.data or {}).get("accounts", {}) if self.data else {}
        return {
            str(account_id): sorted((account.get("roster_errors") or {}).keys())
            for account_id, account in accounts.items()
            if account.get("roster_errors")
        }

    async def _async_sync_roster_notification(self, accounts_data: dict[str, Any]) -> None:
        """Raise one persistent notification while a member is partially unresolvable.

        The tolerance patch keeps the integration running with empty data for
        the failing endpoint; the notification tells the user why some values
        stay empty and what usually fixes it.  It is dismissed as soon as
        Microsoft resolves the member again.
        """
        affected = [
            (account.get("first_name") or str(account_id), sorted(account["roster_errors"]))
            for account_id, account in accounts_data.items()
            if account.get("roster_errors")
        ]
        if not affected:
            if self._roster_notification_sent:
                await self.hass.services.async_call(
                    "persistent_notification",
                    "dismiss",
                    {"notification_id": ROSTER_NOTIFICATION_ID},
                )
                self._roster_notification_sent = False
            return
        if self._roster_notification_sent:
            return
        lines = "\n".join(
            f"- {name}: {', '.join(endpoints)}" for name, endpoints in affected
        )
        message = (
            "Microsoft cannot resolve part of the family roster, so some values "
            "stay empty for:\n\n"
            f"{lines}\n\n"
            "The integration keeps working with everything Microsoft still "
            "returns. This usually happens when a reset or decommissioned device "
            "is still listed at https://account.microsoft.com/family, or when a "
            "school/work (Entra ID) account is enrolled on the child's device. "
            "Removing the stale device from the family, when that is possible, "
            "clears it."
        )
        await self.hass.services.async_call(
            "persistent_notification",
            "create",
            {
                "title": "Microsoft Family Safety - Roster partially unavailable",
                "message": message,
                "notification_id": ROSTER_NOTIFICATION_ID,
            },
        )
        self._roster_notification_sent = True

    async def async_cleanup(self) -> None:
        self._accounts.clear()
        self._devices.clear()
        if self.web_api:
            await self.web_api.close()
        self.web_api = None
        self.api = None
