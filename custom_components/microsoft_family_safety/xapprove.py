"""Xbox Family Settings API ("xapprove") client.

The Xbox Family Settings app reads and writes a child's Xbox screen-time
override on ``https://xapprove.xboxlive.com`` with an Xbox Live XSTS token
(``Authorization: XBL3.0 x=<uhs>;<token>``). This client gets that token from
the sign-in the integration already holds, the same steps as
``probe_xbox_token`` (xbox_token_probe.py):

1. ``login.live.com``: refresh_token grant for the Family Safety client id,
   scope ``service::user.auth.xboxlive.com::MBI_SSL``. Any refresh token in the
   answer is discarded: the integration keeps its own, untouched.
2. Xbox user authentication (RPS ticket ``t=``, then ``d=``).
3. XSTS for ``http://xboxlive.com`` (sandbox RETAIL).

The Xbox user token (about two weeks) and the XSTS token (about 16 hours) are
kept in memory only until shortly before their ``NotAfter``: step 1 runs again
only when the user token has run out, or after a restart.

The override model (captured from the Xbox Family Settings app, 2026-11-08):

* ``overrideType`` 1 = allow until ``validUntil``, 2 = block until
  ``validUntil``, 100 = cancel (``validUntil`` = now);
* ``target`` 1 = screen time on all of the child's Xbox devices;
* one override at a time. The app never puts an allow on top of a block or
  the other way round: it cancels first. This client's callers do the same.

Nothing secret is logged or returned: no token, gamertag or Xbox user id.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging
from typing import Any, Callable

import aiohttp
from yarl import URL

from homeassistant.util import dt as dt_util

from .screen_time import (
    ALLOW_UNTIL,
    BLOCK_UNTIL,
    NO_OVERRIDE,
    iso_utc,
    override as make_override,
    parse_utc,
)
from .xbox_token_probe import (
    CLIENT_ID,
    TOKEN_URL,
    USER_AUTH_URL,
    XBOX_SCOPE,
    XSTS_URL,
)

_LOGGER = logging.getLogger(__name__)

XAPPROVE_HOST = "https://xapprove.xboxlive.com"
FAMILY_URL = "https://accounts.xboxlive.com/family/memberXuid({xuid})"

OVERRIDE_ALLOW = 1
OVERRIDE_BLOCK = 2
OVERRIDE_CANCEL = 100
TARGET_XBOX_SCREEN_TIME = 1

_TYPE_NAMES = {OVERRIDE_ALLOW: ALLOW_UNTIL, OVERRIDE_BLOCK: BLOCK_UNTIL}

_TIMEOUT = aiohttp.ClientTimeout(total=30)
_XBL_HEADERS = {"x-xbl-contract-version": "1", "Accept": "application/json"}
#: A token is renewed this long before its NotAfter.
_TOKEN_MARGIN = timedelta(minutes=5)


class XapproveError(Exception):
    """xapprove could not be used for this call (network, 5xx, odd answer)."""


class XapproveRefused(XapproveError):
    """Microsoft refused our token: a token step or xapprove answered 401/403.

    ``step`` names where (msa_refresh, user_authenticate, xsts_authorize,
    xapprove, family), ``status`` and ``xerr`` say how. No secret inside.
    """

    def __init__(self, step: str, status: int | None, xerr: Any = None, error: str | None = None) -> None:
        self.step = step
        self.status = status
        self.xerr = xerr
        self.error = error
        detail = f"{step}: HTTP {status}"
        if xerr is not None:
            detail += f", XErr {xerr}"
        if error:
            detail += f", {error}"
        super().__init__(detail)

    def facts(self) -> dict[str, Any]:
        return {"step": self.step, "status": self.status, "xerr": self.xerr, "error": self.error}


class XapproveUnavailable(XapproveError):
    """Nothing was asked of Microsoft: e.g. no Xbox user id known for the child."""


# ----------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------


def _xerr(payload: Any, headers: Any) -> Any:
    if isinstance(payload, dict) and payload.get("XErr") is not None:
        return payload["XErr"]
    for name, value in dict(headers or {}).items():
        if str(name).lower() == "x-err":
            return value
    return None


async def _call(
    session: aiohttp.ClientSession, method: str, url: str, **kwargs: Any
) -> tuple[int, Any, dict[str, str]]:
    """One request: (status, JSON or None, headers). Network errors raise XapproveError."""
    try:
        async with session.request(method, url, timeout=_TIMEOUT, **kwargs) as resp:
            try:
                payload = await resp.json(content_type=None)
            except (ValueError, aiohttp.ContentTypeError):
                payload = None
            return resp.status, payload, dict(resp.headers)
    except (aiohttp.ClientError, asyncio.TimeoutError, TimeoutError) as err:
        # The host only: paths carry Xbox user ids.
        raise XapproveError(f"{type(err).__name__} on {URL(url).host}") from err


def _not_after(payload: dict) -> datetime | None:
    return parse_utc(payload.get("NotAfter"))


def _valid(until: datetime | None) -> bool:
    return until is not None and until - _TOKEN_MARGIN > dt_util.utcnow()


# ----------------------------------------------------------------------
# Overrides
# ----------------------------------------------------------------------


def _type_code(entry: dict) -> int | None:
    raw = entry.get("overrideType")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _applies_to_screen_time(entry: dict) -> bool:
    target = entry.get("target")
    applies = entry.get("appliesTo")
    if target is not None:
        return str(target) == str(TARGET_XBOX_SCREEN_TIME)
    if isinstance(applies, list) and applies:
        return TARGET_XBOX_SCREEN_TIME in applies or str(TARGET_XBOX_SCREEN_TIME) in map(str, applies)
    return True


def parse_overrides(payload: Any, now: datetime | None = None) -> dict[str, Any] | None:
    """The screen-time override in force in an xapprove answer.

    ``{"type": blockUntil|allowUntil|none, "valid_until": UTC ISO or None,
    "raw_type": 1|2|None, "actions": {...}}``; None when the answer cannot be
    read (unreadable never means "nothing in force"). An override whose
    ``validUntil`` has passed counts as none.
    """
    now = now or dt_util.utcnow()
    if isinstance(payload, list):
        payload = {"overrides": payload}
    if not isinstance(payload, dict) or not isinstance(payload.get("overrides"), list):
        return None
    found: dict[str, Any] = {**make_override(NO_OVERRIDE), "raw_type": None}
    for entry in payload["overrides"]:
        if not isinstance(entry, dict) or not _applies_to_screen_time(entry):
            continue
        code = _type_code(entry)
        if code == OVERRIDE_CANCEL:
            continue
        kind = _TYPE_NAMES.get(code)
        until = parse_utc(entry.get("validUntil"))
        if kind is None or until is None:
            return None
        if until <= now:
            continue
        candidate = {**make_override(kind, until), "raw_type": code}
        # One at a time in practice; should two come back, a block wins.
        if found["type"] == NO_OVERRIDE or kind == BLOCK_UNTIL:
            found = candidate
    actions = payload.get("actions")
    found["actions"] = _actions(actions)
    return found


def _actions(actions: Any) -> dict[str, Any] | None:
    """What the server offers next (non-secret), e.g. ``{"create": {"allow": [1]}}``."""
    if not isinstance(actions, dict):
        return None
    out: dict[str, Any] = {}
    for verb, value in actions.items():
        if isinstance(value, dict):
            out[str(verb)] = {
                str(k): [x for x in v if isinstance(x, (int, str))] if isinstance(v, list) else None
                for k, v in value.items()
            }
    return out


# ----------------------------------------------------------------------
# Family lookup (child xuid)
# ----------------------------------------------------------------------

_LIST_KEYS = ("familyUsers", "familyMembers", "members", "users")
_XUID_KEYS = ("xuid", "xid", "memberXuid")
_MSA_KEYS = ("msaUserId", "puid", "msaId", "userId", "cid", "id")


def family_members(payload: Any) -> list[dict[str, Any]] | None:
    """The member list of a family answer, or None when there is none."""
    if isinstance(payload, list):
        return [m for m in payload if isinstance(m, dict)]
    if not isinstance(payload, dict):
        return None
    for key in _LIST_KEYS:
        if isinstance(payload.get(key), list):
            return [m for m in payload[key] if isinstance(m, dict)]
    return None


def member_xuid(member: dict) -> str | None:
    for key in _XUID_KEYS:
        value = member.get(key)
        if value is not None and str(value).strip().isdigit():
            return str(value).strip()
    return None


def _same_msa_id(value: Any, account_id: str) -> bool:
    text = str(value).strip()
    if not text:
        return False
    if text == account_id:
        return True
    try:
        wanted = int(account_id)
    except ValueError:
        return False
    for base in (10, 16):
        try:
            if int(text, base) == wanted:
                return True
        except ValueError:
            continue
    return False


def _norm(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def match_children(
    members: list[dict[str, Any]], children: dict[str, dict[str, Any]]
) -> dict[str, tuple[str, str]]:
    """Family Safety child -> (xuid, how), from the Xbox family's members.

    ``children`` maps account id -> {first_name, surname}. In order:
    ``family_id`` (a member field equal to the Microsoft account id),
    ``family_name`` (first and last name, both unique on each side), then
    ``elimination`` (exactly one child left on each side: the Xbox family is
    the Microsoft family). Adults are never matched.
    """
    candidates = [
        m for m in members
        if member_xuid(m) is not None and m.get("isAdult") is not True
    ]
    found: dict[str, tuple[str, str]] = {}
    used: set[str] = set()
    for account_id in children:
        for m in candidates:
            if any(m.get(k) is not None and _same_msa_id(m[k], account_id) for k in _MSA_KEYS):
                xuid = member_xuid(m)
                if xuid not in used:
                    found[account_id] = (xuid, "family_id")
                    used.add(xuid)
                break
    for account_id, child in children.items():
        if account_id in found:
            continue
        name = (_norm(child.get("first_name")), _norm(child.get("surname")))
        if not name[0]:
            continue
        same_name = [
            a for a, c in children.items()
            if (_norm(c.get("first_name")), _norm(c.get("surname"))) == name
        ]
        hits = [
            m for m in candidates
            if member_xuid(m) not in used
            and (_norm(m.get("firstName")), _norm(m.get("lastName"))) == name
        ]
        if len(same_name) == 1 and len(hits) == 1:
            xuid = member_xuid(hits[0])
            found[account_id] = (xuid, "family_name")
            used.add(xuid)
    left_children = [a for a in children if a not in found]
    left_members = [m for m in candidates if member_xuid(m) not in used]
    if len(left_children) == 1 and len(left_members) == 1:
        found[left_children[0]] = (member_xuid(left_members[0]), "elimination")
    return found


# ----------------------------------------------------------------------
# Tokens
# ----------------------------------------------------------------------


@dataclass
class XboxTokens:
    """What the token chain produced. Never logged."""

    uhs: str
    xsts: str
    xsts_not_after: datetime | None
    user_token: str
    user_not_after: datetime | None
    parent_xuid: str | None = field(default=None, repr=False)

    def header(self) -> str:
        return f"XBL3.0 x={self.uhs};{self.xsts}"


def _record(steps: dict | None, name: str, value: Any) -> None:
    if steps is not None:
        steps[name] = value


async def async_msa_access_token(
    session: aiohttp.ClientSession, refresh_token: str, steps: dict | None = None
) -> str:
    """Step 1. The refresh token Microsoft may send back is dropped unread."""
    status, payload, _headers = await _call(
        session, "POST", TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "client_id": CLIENT_ID,
            "scope": XBOX_SCOPE,
            "refresh_token": refresh_token,
        },
    )
    access = payload.get("access_token") if isinstance(payload, dict) else None
    error = payload.get("error") if isinstance(payload, dict) and isinstance(payload.get("error"), str) else None
    _record(steps, "msa_refresh", {
        "status": status,
        "error": error,
        "access_token_received": isinstance(access, str),
        "refresh_token_returned": bool(isinstance(payload, dict) and payload.get("refresh_token")),
        "refresh_token_discarded": True,
    })
    payload = None
    if status == 200 and isinstance(access, str):
        return access
    if status is not None and 400 <= status < 500:
        raise XapproveRefused("msa_refresh", status, error=error)
    raise XapproveError(f"msa_refresh: HTTP {status}")


async def async_user_token(
    session: aiohttp.ClientSession, access: str, steps: dict | None = None
) -> tuple[str, datetime | None]:
    """Step 2: RPS ticket ``t=``, then ``d=``."""
    attempts = []
    last: tuple[int, Any] = (0, None)
    for prefix in ("t", "d"):
        status, body, headers = await _call(
            session, "POST", USER_AUTH_URL,
            json={
                "RelyingParty": "http://auth.xboxlive.com",
                "TokenType": "JWT",
                "Properties": {
                    "AuthMethod": "RPS",
                    "SiteName": "user.auth.xboxlive.com",
                    "RpsTicket": f"{prefix}={access}",
                },
            },
            headers=_XBL_HEADERS,
        )
        attempts.append({"ticket_prefix": prefix, "status": status, "xerr": _xerr(body, headers)})
        if status == 200 and isinstance(body, dict) and isinstance(body.get("Token"), str):
            _record(steps, "user_authenticate", attempts)
            return body["Token"], _not_after(body)
        last = (status, _xerr(body, headers))
    _record(steps, "user_authenticate", attempts)
    if last[0] and 400 <= last[0] < 500:
        raise XapproveRefused("user_authenticate", last[0], last[1])
    raise XapproveError(f"user_authenticate: HTTP {last[0]}")


async def async_xsts(
    session: aiohttp.ClientSession, user_token: str, steps: dict | None = None
) -> tuple[str, str, datetime | None, str | None]:
    """Step 3: (uhs, token, NotAfter, parent xuid or None)."""
    status, body, headers = await _call(
        session, "POST", XSTS_URL,
        json={
            "RelyingParty": "http://xboxlive.com",
            "TokenType": "JWT",
            "Properties": {"UserTokens": [user_token], "SandboxId": "RETAIL"},
        },
        headers=_XBL_HEADERS,
    )
    xui: dict[str, Any] = {}
    if isinstance(body, dict):
        claims = body.get("DisplayClaims")
        items = claims.get("xui") if isinstance(claims, dict) else None
        if isinstance(items, list) and items and isinstance(items[0], dict):
            xui = items[0]
    _record(steps, "xsts_authorize", {
        "status": status,
        "xerr": _xerr(body, headers),
        "token_received": bool(isinstance(body, dict) and body.get("Token")),
        "display_claims_xui_keys": sorted(str(k) for k in xui),
    })
    if status == 200 and isinstance(body, dict) and isinstance(body.get("Token"), str) and xui.get("uhs"):
        xid = xui.get("xid")
        return str(xui["uhs"]), body["Token"], _not_after(body), (str(xid) if xid else None)
    if status and 400 <= status < 500:
        raise XapproveRefused("xsts_authorize", status, _xerr(body, headers))
    raise XapproveError(f"xsts_authorize: HTTP {status}")


async def async_token_chain(
    session: aiohttp.ClientSession, refresh_token: str, steps: dict | None = None
) -> XboxTokens:
    access = await async_msa_access_token(session, refresh_token, steps)
    user_token, user_until = await async_user_token(session, access, steps)
    access = None
    uhs, xsts, xsts_until, xid = await async_xsts(session, user_token, steps)
    return XboxTokens(uhs, xsts, xsts_until, user_token, user_until, xid)


# ----------------------------------------------------------------------
# The client
# ----------------------------------------------------------------------


class XapproveClient:
    """xapprove reads and writes with cached Xbox tokens.

    ``refresh_token_source`` is an async context manager factory yielding the
    stored mobile refresh token while the integration's own token refresh is
    held off (the authenticator's login lock).
    """

    def __init__(
        self,
        session_getter: Callable[[], aiohttp.ClientSession],
        refresh_token_source: Callable[[], Any],
    ) -> None:
        self._session_getter = session_getter
        self._refresh_token_source = refresh_token_source
        self._tokens: XboxTokens | None = None
        self._token_lock = asyncio.Lock()
        #: xuid -> lock: one write at a time per child.
        self._write_locks: dict[str, asyncio.Lock] = {}

    def forget_tokens(self) -> None:
        self._tokens = None

    @property
    def parent_xuid(self) -> str | None:
        return self._tokens.parent_xuid if self._tokens else None

    async def async_tokens(self, *, force: bool = False) -> XboxTokens:
        async with self._token_lock:
            tokens = self._tokens
            if tokens is not None and not force and _valid(tokens.xsts_not_after):
                return tokens
            session = self._session_getter()
            if tokens is not None and _valid(tokens.user_not_after):
                try:
                    uhs, xsts, until, xid = await async_xsts(session, tokens.user_token)
                except XapproveRefused:
                    self._tokens = tokens = None  # the user token went stale: start over
                else:
                    tokens.uhs, tokens.xsts, tokens.xsts_not_after = uhs, xsts, until
                    tokens.parent_xuid = xid or tokens.parent_xuid
                    return tokens
            async with self._refresh_token_source() as refresh_token:
                self._tokens = await async_token_chain(session, refresh_token)
            return self._tokens

    async def _request(
        self, method: str, url: str, *, json: Any = None, extra_headers: dict | None = None,
        step: str = "xapprove",
    ) -> Any:
        """One authorised request; a 401/403 is retried once with fresh tokens."""
        for attempt in (0, 1):
            tokens = await self.async_tokens(force=attempt == 1)
            headers = {"Authorization": tokens.header(), "Accept": "application/json"}
            headers.update(extra_headers or {})
            kwargs: dict[str, Any] = {"headers": headers}
            if json is not None:
                kwargs["json"] = json
            status, payload, resp_headers = await _call(self._session_getter(), method, url, **kwargs)
            if status in (401, 403):
                if attempt == 0:
                    continue
                raise XapproveRefused(step, status, _xerr(payload, resp_headers))
            if not 200 <= status < 300:
                raise XapproveError(f"{step}: HTTP {status}, XErr {_xerr(payload, resp_headers)}")
            return payload
        raise XapproveError(step)  # not reached

    # Reads ------------------------------------------------------------------

    async def async_get_overrides(self, xuid: str) -> dict[str, Any]:
        payload = await self._request(
            "GET", f"{XAPPROVE_HOST}/api/v1/user/{xuid}/settings/screentime/overrides"
        )
        state = parse_overrides(payload)
        if state is None:
            raise XapproveError("xapprove: unreadable overrides answer")
        return state

    async def async_family_members(self) -> list[dict[str, Any]] | None:
        """The parent's Xbox family; None without a parent xuid claim."""
        tokens = await self.async_tokens()
        if not tokens.parent_xuid:
            return None
        payload = await self._request(
            "GET", FAMILY_URL.format(xuid=tokens.parent_xuid),
            extra_headers={"x-xbl-contract-version": "3"}, step="family",
        )
        return family_members(payload)

    # Writes -----------------------------------------------------------------

    def write_lock(self, xuid: str) -> asyncio.Lock:
        return self._write_locks.setdefault(xuid, asyncio.Lock())

    async def async_create(self, xuid: str, override_type: int, valid_until: datetime) -> dict[str, Any] | None:
        """POST overrides/create; the parsed state of the reply (None if unreadable).

        The caller holds ``write_lock(xuid)``.
        """
        payload = await self._request(
            "POST",
            f"{XAPPROVE_HOST}/api/v1/user/{xuid}/settings/screentime/overrides/create",
            json={
                "overrideType": override_type,
                "target": TARGET_XBOX_SCREEN_TIME,
                "validUntil": iso_utc(valid_until),
            },
        )
        return parse_overrides(payload)

    async def async_cancel(self, xuid: str) -> dict[str, Any] | None:
        return await self.async_create(xuid, OVERRIDE_CANCEL, dt_util.utcnow())

