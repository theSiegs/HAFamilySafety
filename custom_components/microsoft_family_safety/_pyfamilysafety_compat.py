"""Runtime compatibility patches for pyfamilysafety 1.1.2.

The pinned PyPI release of ``pyfamilysafety`` (1.1.2) has several problems
that surface on Home Assistant, especially on Python 3.14:

1. ``Authenticator._request_handler`` creates a brand new
   ``aiohttp.ClientSession()`` on *every* auth/refresh request and never
   reuses Home Assistant's shared session. Beyond leaking sessions, this is
   the line that throws ``TypeError: 'ClientSession' object is not callable``
   when another component in the same process has replaced the
   ``aiohttp.ClientSession`` symbol with an instance (observed in the wild
   together with the Family Link integration — see issue #22). It is also the
   root cause of the cascading 400/401 failures in issues #20 and #23.

2. ``_request_handler`` calls ``await resp.json()`` unconditionally. When
   Microsoft answers an expired/invalid session with an HTML error page
   (the recurring 400 in issue #23), that call raises and crashes the whole
   update cycle instead of surfacing a clean status code.

3. ``Authenticator.perform_refresh`` silently returns when Microsoft rejects
   a refresh-token grant. The stale access token is then reused until the
   mobile API answers ``HTTP Unauthorized``.

4. In 1.1.2 ``Authenticator.access_token`` is the raw token value. Newer
   releases expose the complete ``MSAuth1.0`` Authorization value instead.
   Code written against the newer API can therefore send a raw token to the
   mobile aggregator and receive ``Authentication.BadCompactTicket``;
   ``authenticator_authorization_header`` builds the right value for both.

5. ``Account.update`` lets a single Microsoft 404
   ``Family.UnableToFindTargetResource`` / ``RosterError`` abort the whole
   roster load, so one child whose node Microsoft cannot resolve (a reset
   device, or a school/work Entra ID account on the same machine, issue #42)
   blocks every entity of every child. The tolerant replacement keeps that
   child with empty data for the failing endpoint and records the failure in
   ``Account.roster_errors`` for the coordinator to surface.

6. ``Account._get_overrides`` reads the device-limits overrides from the old
   ``/v1`` route, counts extra time as a lock and knows Windows only by the
   name "Windows" (Microsoft now also lists it as "Desktop"). The replacement
   reads the ``/v4`` route with ``Plat-Info: ALL`` that the Family Safety app
   and current pyfamilysafety use, and decides what is locked with
   ``mobile_overrides``.

This module patches those paths while keeping token values out of the log.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import inspect
import logging
from typing import Any

import aiohttp
from pyfamilysafety import application as _pfs_application
from pyfamilysafety import const as _pfs_const
from pyfamilysafety.account import Account
from pyfamilysafety.api import FamilySafetyAPI
from pyfamilysafety.authenticator import Authenticator
from pyfamilysafety.authenticator.const import (
    CLIENT_ID,
    SCOPE,
    TOKEN_ENDPOINT,
    USER_AGENT,
)
from pyfamilysafety.exceptions import HttpException, Unauthorized

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .mobile_overrides import (
    MOBILE_OVERRIDES_PATH,
    MOBILE_OVERRIDES_PLAT_INFO,
    mobile_blocked_platforms,
    mobile_lock_of,
)

_LOGGER = logging.getLogger(__name__)

# Markers so we never double-patch.
_REQUEST_PATCH_MARKER = "_hafs_shared_session_patch"
_REFRESH_PATCH_MARKER = "_hafs_refresh_validation_patch"
_API_PATCH_MARKER = "_hafs_unauthorized_retry_patch"
_ROSTER_TOLERANCE_PATCH_MARKER = "_hafs_roster_tolerance_patch"
_APP_PLATFORM_PATCH_MARKER = "_hafs_app_platform_patch"
_V4_OVERRIDES_PATCH_MARKER = "_hafs_v4_overrides_read_patch"
_APP_NAME_PATCH_MARKER = "_hafs_app_display_name_patch"

# pyfamilysafety endpoint key for the /v4 overrides read.
_V4_OVERRIDES_ENDPOINT = "hafs_get_override_device_restrictions_v4"

#: Fragments of Microsoft's "node not found" answer on the mobile aggregator.
#: The token is valid; Microsoft simply cannot resolve one family member's
#: resource (typically a reset or decommissioned device still listed in the
#: roster, or a device enrolled in a school/work Entra ID tenant).
_STALE_ROSTER_MARKERS = (
    "unabletofindtargetresource",
    "rostererror",
    "unable to find the node",
)

#: (user_id, endpoint) pairs already reported at WARNING level, so a roster
#: error that persists across polls is logged once and then only at DEBUG.
_ROSTER_WARNED: set[tuple[str, str]] = set()


def is_stale_roster_error(text: str) -> bool:
    """Return True for Microsoft's "device/node not found in roster" 404."""
    lowered = (text or "").lower()
    return any(marker in lowered for marker in _STALE_ROSTER_MARKERS)


def _empty_screentime_report() -> dict[str, Any]:
    """Shape ``Device.from_dict`` and the usage aggregates can consume safely."""
    return {
        "deviceUsageAggregates": {
            "deviceAggregates": [],
            "totalScreenTime": 0,
            "dailyAverage": 0,
        }
    }

# The HA-managed session, injected at setup time.
_shared_session: aiohttp.ClientSession | None = None


def set_shared_session(session: aiohttp.ClientSession) -> None:
    """Register the Home Assistant shared aiohttp session for the patch."""
    global _shared_session
    _shared_session = session


async def _patched_request_handler(
    self: Authenticator,
    method: str,
    url: str,
    body: Any = None,
    headers: dict | None = None,
    data: Any = None,
) -> dict:
    """Drop-in replacement for ``Authenticator._request_handler``.

    Reuses the shared HA session instead of creating a new ClientSession,
    and decodes the response body defensively.
    """
    response: dict = {"status": 0, "text": "", "json": "", "headers": ""}

    session = _shared_session
    if session is None or session.closed:
        _LOGGER.debug("Shared session unavailable, using a temporary session")
        session_cm = aiohttp.ClientSession()
    else:
        session_cm = None

    active_session = session_cm if session_cm is not None else session

    req_headers = {
        "user-agent": USER_AGENT,
        "X-Requested-With": "com.microsoft.familysafety",
    }
    if headers:
        req_headers.update(headers)

    try:
        async with active_session.request(
            method=method,
            url=url,
            json=body,
            headers=req_headers,
            data=data,
        ) as resp:
            response["status"] = resp.status
            response["text"] = await resp.text()
            response["headers"] = resp.headers
            try:
                response["json"] = await resp.json(content_type=None)
            except (aiohttp.ContentTypeError, ValueError):
                _LOGGER.debug(
                    "Auth response from %s was not JSON (status %s)",
                    url, resp.status,
                )
                response["json"] = {}
    finally:
        if session_cm is not None:
            await session_cm.close()

    return response


def _set_access_token(authenticator: Authenticator, token: str) -> None:
    """Set the raw access token across pyfamilysafety API versions."""
    if hasattr(authenticator, "_access_token"):
        authenticator._access_token = token
    else:
        authenticator.access_token = token


def authenticator_authorization_header(authenticator: Authenticator) -> str:
    """Return the mobile API Authorization value across library versions."""
    value = str(getattr(authenticator, "access_token", "") or "")
    if not value:
        return value
    if value.lstrip().lower().startswith("msauth1.0 "):
        return value
    return f'MSAuth1.0 usertoken="{value}", type="MSACT"'


def _oauth_error(payload: object) -> str | None:
    """Extract a non-secret OAuth error code."""
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if isinstance(error, str):
        return error
    if isinstance(error, dict) and error.get("code"):
        return str(error["code"])
    return None


async def _patched_perform_refresh(self: Authenticator) -> None:
    """Refresh the mobile token and fail when Microsoft rejects the grant."""
    lock = self._login_lock
    if lock.locked():
        _LOGGER.debug("Mobile token refresh already in progress; waiting")
        async with lock:
            pass
        if authenticator_access_token_expired(self):
            raise HttpException(
                "authentication failed: concurrent mobile token refresh "
                "did not produce a valid access token"
            )
        return

    async with lock:
        old_refresh_token = self.refresh_token
        _LOGGER.info(
            "Refreshing Microsoft Family Safety mobile access token "
            "(previous_expiry=%s)",
            self.expires.isoformat() if isinstance(self.expires, datetime) else None,
        )

        form = aiohttp.FormData()
        form.add_field("client_id", CLIENT_ID)
        form.add_field("refresh_token", old_refresh_token or "")
        form.add_field("grant_type", "refresh_token")
        form.add_field("scope", SCOPE)

        try:
            tokens = await self._request_handler(
                method="POST", url=TOKEN_ENDPOINT, data=form
            )
        except Exception as err:
            _LOGGER.warning(
                "Microsoft Family Safety mobile token refresh request failed: %s",
                type(err).__name__,
            )
            raise

        status = tokens.get("status")
        payload = tokens.get("json")
        error = _oauth_error(payload)
        if status != 200 or not isinstance(payload, dict):
            _LOGGER.error(
                "Microsoft Family Safety mobile token refresh rejected: "
                "status=%s error=%s",
                status, error,
            )
            raise HttpException(
                "authentication failed during mobile token refresh",
                status,
                error or "token_endpoint_rejected",
            )

        access_token = payload.get("access_token")
        refresh_token = payload.get("refresh_token")
        expires_in = payload.get("expires_in")
        if not access_token or not refresh_token or expires_in is None:
            _LOGGER.error(
                "Microsoft Family Safety mobile token refresh returned an "
                "incomplete response (status=%s)",
                status,
            )
            raise HttpException(
                "authentication failed: incomplete mobile token refresh response"
            )

        try:
            expires_seconds = int(expires_in)
        except (TypeError, ValueError) as err:
            raise HttpException(
                "authentication failed: invalid mobile token expiry"
            ) from err

        _set_access_token(self, str(access_token))
        self.expires = datetime.now() + timedelta(seconds=expires_seconds)
        self.refresh_token = str(refresh_token)
        if payload.get("user_id") is not None:
            self.user_id = payload["user_id"]

        _LOGGER.info(
            "Microsoft Family Safety mobile token refresh succeeded "
            "(expires_in=%ss refresh_token_rotated=%s)",
            expires_seconds,
            bool(old_refresh_token and self.refresh_token != old_refresh_token),
        )


_original_send_request = FamilySafetyAPI.send_request


async def _patched_send_request(
    self: FamilySafetyAPI,
    endpoint: str,
    body: object = None,
    headers: dict | None = None,
    platform: str | None = None,
    **kwargs: Any,
):
    """Retry one mobile API 401 after forcing a fresh access token."""
    try:
        return await _original_send_request(
            self,
            endpoint,
            body=body,
            headers=headers,
            platform=platform,
            **kwargs,
        )
    except Unauthorized as err:
        authenticator = getattr(self, "authenticator", None)
        if authenticator is None:
            raise HttpException(
                "authentication failed: mobile API returned HTTP Unauthorized"
            ) from err

        _LOGGER.warning(
            "Microsoft Family Safety mobile API returned HTTP Unauthorized "
            "for endpoint=%s; forcing one token refresh and retry",
            endpoint,
        )
        await authenticator.perform_refresh()

        session = getattr(self, "_session", None)
        if session is not None:
            try:
                session.headers.pop("Authorization")
            except KeyError:
                pass

        try:
            result = await _original_send_request(
                self,
                endpoint,
                body=body,
                headers=headers,
                platform=platform,
                **kwargs,
            )
        except Unauthorized as retry_err:
            _LOGGER.error(
                "Microsoft Family Safety mobile API still returned HTTP "
                "Unauthorized after token refresh for endpoint=%s",
                endpoint,
            )
            raise HttpException(
                "authentication failed: HTTP Unauthorized after mobile token refresh"
            ) from retry_err

        _LOGGER.info(
            "Microsoft Family Safety mobile API recovered after forced token "
            "refresh for endpoint=%s",
            endpoint,
        )
        return result


def _patch_app_platform() -> bool:
    """Send a Plat-Info header for classic desktop apps (``win32:`` ids).

    pyfamilysafety 1.1.2's ``application.get_platform`` only knows the ``x:``,
    ``appx:`` and ``a:`` prefixes, so blocking or unblocking a ``win32:`` app
    (Steam, CurseForge, javaw...) sent no Plat-Info and Microsoft answered
    400 "The platform field is required" (#54). ``Application.block_app`` and
    ``unblock_app`` look the helper up as a module global at call time, so
    replacing it there is enough.
    """
    current = _pfs_application.get_platform
    if getattr(current, _APP_PLATFORM_PATCH_MARKER, False):
        return False
    original = current

    def _patched_get_platform(app_id: str):
        if isinstance(app_id, str) and app_id.lower().startswith("win32:"):
            return "WINDOWS"
        return original(app_id)

    setattr(_patched_get_platform, _APP_PLATFORM_PATCH_MARKER, True)
    _pfs_application.get_platform = _patched_get_platform
    return True


def _patch_v4_overrides_read() -> bool:
    """Read the device-limits overrides from the /v4 route (see 6. above)."""
    current = Account._get_overrides
    if getattr(current, _V4_OVERRIDES_PATCH_MARKER, False):
        return False
    _pfs_const.ENDPOINTS[_V4_OVERRIDES_ENDPOINT] = {
        "url": "{BASE_URL}" + MOBILE_OVERRIDES_PATH.replace("{child_id}", "{USER_ID}"),
        "method": "GET",
    }

    async def _patched_get_overrides(self: Account) -> None:
        response = await self._api.send_request(
            endpoint=_V4_OVERRIDES_ENDPOINT,
            platform=MOBILE_OVERRIDES_PLAT_INFO,
            USER_ID=self.user_id,
        )
        state = response.get("json") if isinstance(response, dict) else None
        if not isinstance(state, dict):
            state = {}
        self.blocked_platforms = mobile_blocked_platforms(state)
        for entry in state.get("lockablePlatforms") or []:
            if not isinstance(entry, dict):
                continue
            locked = mobile_lock_of(entry) is not None
            for raw in entry.get("devices") or []:
                device_id = str((raw or {}).get("deviceId") or "").replace("g:", "")
                for device in self.devices or []:
                    if getattr(device, "device_id", None) == device_id:
                        device.update_blocked_status(locked)

    setattr(_patched_get_overrides, _V4_OVERRIDES_PATCH_MARKER, True)
    Account._get_overrides = _patched_get_overrides
    return True


def _app_block_body(app: Any, block_state: str) -> dict[str, Any]:
    """pyfamilysafety's block/unblock body, with the app's name kept."""
    return {
        "appId": app.app_id,
        "appTimeEnforcementPolicy": "WeekendAndWeekday",
        "blockState": block_state,
        "blocked": False,
        "displayName": app.name or app.app_id,
        "enabled": True,
    }


def _patch_app_display_name() -> bool:
    """Keep an app's name when blocking or unblocking it.

    pyfamilysafety 1.1.2's ``Application.block_app`` and ``unblock_app`` send
    the app id as ``displayName``. Microsoft stores it, so every block or
    unblock renamed the app to its id (``x:xboxplt:...``) in the activity
    report and in this integration's Applications sensor. The body is
    otherwise unchanged.
    """
    application = _pfs_application.Application
    if getattr(application.block_app, _APP_NAME_PATCH_MARKER, False):
        return False

    async def _send(app: Any, block_state: str) -> None:
        await app._api.send_request(
            endpoint="set_app_policy",
            body=_app_block_body(app, block_state),
            USER_ID=app._user_id,
            APP_ID=app.app_id,
            platform=_pfs_application.get_platform(app.app_id),
        )

    async def _patched_block_app(self) -> None:
        await _send(self, "BlockedAlways")
        self.blocked = True

    async def _patched_unblock_app(self) -> None:
        await _send(self, "NotBlocked")
        self.blocked = False

    for func in (_patched_block_app, _patched_unblock_app):
        setattr(func, _APP_NAME_PATCH_MARKER, True)
    application.block_app = _patched_block_app
    application.unblock_app = _patched_unblock_app
    return True


def _patch_account_roster_tolerance() -> bool:
    """Keep loading a family member when Microsoft cannot resolve part of it.

    ``Account.update`` (called by ``FamilySafety.create`` for every member and
    again on every ``FamilySafety.update``) issues five per-member requests.
    In 1.1.2 the first ``HttpException`` aborts the whole roster, so one
    ``Family.UnableToFindTargetResource`` 404 blocks every child (issue #42).
    The replacement runs each request on its own, swallows only that specific
    404, leaves the member with empty data for the failing endpoint and lists
    the failures in ``account.roster_errors`` ({endpoint: message}).  Any other
    error still propagates unchanged.
    """
    current = Account.update
    if getattr(current, _ROSTER_TOLERANCE_PATCH_MARKER, False):
        return False

    async def _guarded(account: Account, endpoint: str, coro) -> None:
        try:
            await coro
        except HttpException as err:
            text = str(err)
            if not is_stale_roster_error(text):
                raise
            account.roster_errors[endpoint] = text[:200]
            key = (str(account.user_id), endpoint)
            _LOGGER.log(
                logging.DEBUG if key in _ROSTER_WARNED else logging.WARNING,
                "Microsoft cannot resolve the %s resource of family member %s "
                "(%s); keeping the member with empty data for that endpoint. "
                "This usually means a reset or decommissioned device still "
                "listed in the family roster, or a device enrolled in a "
                "school/work Entra ID tenant. Check "
                "https://account.microsoft.com/family",
                endpoint,
                account.user_id,
                text[:120],
            )
            _ROSTER_WARNED.add(key)

    async def _patched_update(self: Account) -> None:
        self.roster_errors = {}
        await _guarded(self, "screentime_usage", self.get_screentime_usage())
        # get_screentime_usage() assigns both reports only after its second
        # request succeeded; without them Device.from_dict() and
        # _get_applications() fail on None, so give them harmless shapes.
        if self.screentime_usage is None:
            self.screentime_usage = _empty_screentime_report()
        if self.application_usage is None:
            self.application_usage = {"appActivity": []}
        await asyncio.gather(
            _guarded(self, "devices", self._get_devices()),
            _guarded(self, "overrides", self._get_overrides()),
            _guarded(self, "applications", self._get_applications()),
            _guarded(self, "spending", self._get_account_balance()),
        )
        if self.devices is None:
            self.devices = []
        if self.blocked_platforms is None:
            self.blocked_platforms = []

    setattr(_patched_update, _ROSTER_TOLERANCE_PATCH_MARKER, True)
    Account.update = _patched_update
    return True


def apply_patches(hass: HomeAssistant) -> None:
    """Apply the pyfamilysafety compatibility patches (idempotent)."""
    set_shared_session(async_get_clientsession(hass))
    applied: list[str] = []

    if not getattr(Authenticator._request_handler, _REQUEST_PATCH_MARKER, False):
        setattr(_patched_request_handler, _REQUEST_PATCH_MARKER, True)
        Authenticator._request_handler = _patched_request_handler
        applied.append("shared session + tolerant JSON decode")

    if not getattr(Authenticator.perform_refresh, _REFRESH_PATCH_MARKER, False):
        setattr(_patched_perform_refresh, _REFRESH_PATCH_MARKER, True)
        Authenticator.perform_refresh = _patched_perform_refresh
        applied.append("validated mobile token refresh")

    if not getattr(FamilySafetyAPI.send_request, _API_PATCH_MARKER, False):
        setattr(_patched_send_request, _API_PATCH_MARKER, True)
        FamilySafetyAPI.send_request = _patched_send_request
        applied.append("single Unauthorized refresh retry")

    if _patch_app_platform():
        applied.append("Plat-Info for win32 desktop apps")

    if _patch_app_display_name():
        applied.append("app name kept on block/unblock")

    if _patch_account_roster_tolerance():
        applied.append("per-member roster error tolerance")

    if _patch_v4_overrides_read():
        applied.append("v4 device-limits overrides read")

    if applied:
        _LOGGER.debug(
            "Applied pyfamilysafety compatibility patches (%s)",
            "; ".join(applied),
        )


def authenticator_access_token_expired(authenticator: Authenticator) -> bool:
    """Return token-expiry state across pyfamilysafety API versions."""
    try:
        value = getattr(authenticator, "access_token_expired")
    except AttributeError:
        value = None
    if value is not None:
        return bool(value() if callable(value) else value)

    if not getattr(authenticator, "access_token", None):
        return True
    expires = getattr(authenticator, "expires", None)
    if expires is None:
        return True
    now = (
        datetime.now(tz=expires.tzinfo)
        if getattr(expires, "tzinfo", None)
        else datetime.now()
    )
    return expires <= now + timedelta(seconds=60)


async def create_authenticator(
    hass: HomeAssistant,
    token: str,
    *,
    use_refresh_token: bool,
) -> Authenticator:
    """Create an Authenticator across pyfamilysafety API versions."""
    apply_patches(hass)
    create = Authenticator.create
    parameters = inspect.signature(create).parameters
    kwargs: dict[str, Any] = {
        "token": token,
        "use_refresh_token": use_refresh_token,
    }
    if "client_session" in parameters:
        kwargs["client_session"] = async_get_clientsession(hass)
        _LOGGER.debug(
            "Creating pyfamilysafety Authenticator with Home Assistant client session"
        )
    else:
        _LOGGER.debug(
            "Creating pyfamilysafety Authenticator using legacy create() signature"
        )
    return await create(**kwargs)
