"""Xbox Family Settings API ("xapprove") client.

The Xbox Family Settings app reads and writes a child's Xbox screen-time
override on ``https://xapprove.xboxlive.com`` with an Xbox Live XSTS token
(``Authorization: XBL3.0 x=<uhs>;<token>``) from its own sign-in. This client
uses the integration's Xbox Family Settings sign-in for that (xbox_sisu.py):
by default the XSTS of its explicit XSTS step (SISU's user, device and title
tokens, relying party http://xboxlive.com), the app's request headers, and
its ProofKey to sign each request; probe_xapprove tries the other ways
(``PROBE_METHODS``). (Every token the Family Safety sign-in can produce was
refused, 401 token_required, live 2026-11-08; SISU's own XSTS got a 401 too.)

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
import base64
import json as jsonlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging
import secrets
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

_LOGGER = logging.getLogger(__name__)

XAPPROVE_HOST = "https://xapprove.xboxlive.com"
FAMILY_URL = "https://accounts.xboxlive.com/family/memberXuid({xuid})"

OVERRIDE_ALLOW = 1
OVERRIDE_BLOCK = 2
OVERRIDE_CANCEL = 100
TARGET_XBOX_SCREEN_TIME = 1

_TYPE_NAMES = {OVERRIDE_ALLOW: ALLOW_UNTIL, OVERRIDE_BLOCK: BLOCK_UNTIL}

_TIMEOUT = aiohttp.ClientTimeout(total=30)
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
#: Fields that may say whether a member is an adult (seen live: none of them
#: was isAdult; probe_xapprove reports the role values it finds).
_ROLE_KEYS = ("role", "memberRole", "familyRole", "userRole", "ageGroup", "accountType")
_ADULT_WORDS = ("adult", "parent", "organizer", "organiser", "admin", "owner", "guardian")
_CHILD_WORDS = ("child", "teen", "kid", "minor")


def member_roles(member: dict) -> list[str]:
    """The role-like values of a member (short categorical text, not personal)."""
    found = []
    for key in _ROLE_KEYS:
        value = member.get(key)
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            found.append(str(value)[:40])
    return found


def is_adult(member: dict) -> bool:
    """A member who is an adult: ``isAdult``, else a role naming one.

    A role naming a child wins over one naming an adult; unknown is not adult.
    """
    flag = member.get("isAdult")
    if isinstance(flag, bool):
        return flag
    roles = [r.casefold() for r in member_roles(member)]
    if any(word in r for r in roles for word in _CHILD_WORDS):
        return False
    return any(word in r for r in roles for word in _ADULT_WORDS)


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
    members: list[dict[str, Any]],
    children: dict[str, dict[str, Any]],
    exclude: set[str] | frozenset[str] = frozenset(),
) -> dict[str, tuple[str, str]]:
    """Family Safety child -> (xuid, how), from the Xbox family's members.

    ``children`` maps account id -> {first_name, surname}. In order:
    ``family_id`` (a member field equal to the Microsoft account id),
    ``family_name`` (first and last name, both unique on each side), then
    ``elimination`` (exactly one child left on each side: the Xbox family is
    the Microsoft family). Adults (``is_adult``) and the xuids in
    ``exclude`` (the signed-in parent) are never matched.
    """
    candidates = [
        m for m in members
        if member_xuid(m) is not None and member_xuid(m) not in exclude and not is_adult(m)
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
# Tokens and ways of signing a request
# ----------------------------------------------------------------------

#: An MSA scope for xapprove itself (tried by probe_xapprove).
XAPPROVE_SCOPE = "service::xapprove.xboxlive.com::MBI_SSL"

#: Relying parties for the explicit XSTS step (xsts.auth.xboxlive.com) that
#: probe_xapprove tries. The app's capture shows http://xboxlive.com.
RP_XBOXLIVE = "http://xboxlive.com"
RP_XAPPROVE = "http://xapprove.xboxlive.com"
RP_XAPPROVE_HTTPS = "https://xapprove.xboxlive.com/"

#: Headers the Xbox Family Settings app sends to xapprove (capture,
#: 2026-11): okhttp's user agent, a JSON content type, a correlation vector
#: (``MS-CV``) and no ``x-xbl-contract-version``.
APP_USER_AGENT = "okhttp/4.9.2"


def new_ms_cv() -> str:
    """A fresh correlation vector: 16 base64 characters and ``.1``."""
    return base64.b64encode(secrets.token_bytes(12)).decode() + ".1"


def app_headers() -> dict[str, str]:
    return {
        "User-Agent": APP_USER_AGENT,
        "Content-Type": "application/json",
        "MS-CV": new_ms_cv(),
    }


@dataclass
class XboxTokens:
    """An XSTS token and its claims (plus SISU's other tokens). Never logged."""

    uhs: str
    xsts: str = field(repr=False)
    xsts_not_after: datetime | None
    user_token: str = field(repr=False)
    user_not_after: datetime | None
    parent_xuid: str | None = field(default=None, repr=False)
    #: SISU's TitleToken (the Xbox Family Settings title), for the XSTS step.
    title_token: str = field(default="", repr=False)
    title_not_after: datetime | None = None
    #: The names (only) of the XSTS's ``xui`` claims.
    xui_keys: tuple[str, ...] = ()

    def header(self) -> str:
        return f"XBL3.0 x={self.uhs};{self.xsts}"


@dataclass(frozen=True)
class AuthMethod:
    """How xapprove's Authorization header is made, from the Xbox sign-in.

    ``token``:

    * ``xsts``: ``XBL3.0 x=<uhs>;<xsts>`` with an XSTS from the explicit
      ``xsts.auth.xboxlive.com/xsts/authorize`` step for ``relying_party``
      (SISU's user, device and title tokens), as the app does;
    * ``sisu``: the same with SISU's own ``AuthorizationToken``;
    * ``msa_wlid``: ``WLID1.0 t=<token>``, an MSA token of the Xbox Family
      Settings client for ``msa_scope``;
    * ``msa_xbl``: ``XBL3.0 x=<uhs>;<token>`` with that MSA token.

    ``signed``: a ``Signature`` header made with the ProofKey.
    ``contract``: an ``x-xbl-contract-version`` header, or none (the app
    sends none). ``app_like``: the app's other headers (``app_headers``).
    """

    name: str
    token: str = "xsts"
    relying_party: str | None = RP_XBOXLIVE
    signed: bool = True
    msa_scope: str | None = None
    contract: str | None = None
    app_like: bool = True


def _methods() -> list[AuthMethod]:
    found: list[AuthMethod] = []
    for prefix, token, rp in (
        ("xsts", "xsts", RP_XBOXLIVE),
        ("xsts_xapprove", "xsts", RP_XAPPROVE),
        ("xsts_xapprove_https", "xsts", RP_XAPPROVE_HTTPS),
        ("sisu", "sisu", None),
    ):
        for signed in (True, False):
            found.append(AuthMethod(
                f"{prefix}_{'signed' if signed else 'unsigned'}", token=token,
                relying_party=rp, signed=signed,
            ))
    for prefix, token in (("msa_wlid", "msa_wlid"), ("msa_xbl", "msa_xbl")):
        for signed in (True, False):
            found.append(AuthMethod(
                f"{prefix}_{'signed' if signed else 'unsigned'}", token=token,
                relying_party=None, signed=signed, msa_scope=XAPPROVE_SCOPE,
            ))
    return found


#: Every way probe_xapprove tries, in order, the most app-like first: the
#: XSTS from the explicit step per relying party, then SISU's own XSTS, then
#: the xapprove-scope MSA token, each signed and unsigned, all with the
#: app's headers. (Live 2026-11-08 the first read with SISU's own XSTS got
#: 401; every token the Family Safety sign-in can produce got 401
#: token_required.)
PROBE_METHODS: list[AuthMethod] = _methods()
METHODS_BY_NAME: dict[str, AuthMethod] = {m.name: m for m in PROBE_METHODS}
#: The way the integration uses until probe_xapprove saves another.
DEFAULT_METHOD = METHODS_BY_NAME["xsts_signed"]
#: The Xbox family read (accounts.xboxlive.com): SISU's XSTS, signed.
FAMILY_METHOD = AuthMethod("family", token="sisu", relying_party=None, app_like=False)
#: Names saved by earlier versions -> today's way.
_LEGACY_NAMES = {
    "sisu": "sisu_signed",
    "sisu_xapprove_scope_wlid": "msa_wlid_signed",
}


def method_by_name(name: Any) -> AuthMethod | None:
    """A saved way by name (earlier versions' names included); None if unknown."""
    if not isinstance(name, str):
        return None
    return METHODS_BY_NAME.get(_LEGACY_NAMES.get(name, name))


def _www_authenticate(headers: dict[str, str]) -> dict[str, str] | None:
    for name, value in headers.items():
        if str(name).lower() == "www-authenticate":
            return {"name": str(name), "value": str(value)[:300]}
    return None


# ----------------------------------------------------------------------
# The client
# ----------------------------------------------------------------------


class XapproveClient:
    """xapprove reads and writes, signed with the Xbox Family Settings sign-in.

    ``sisu`` is the sign-in (xbox_sisu.XboxSisuAuth): its XSTS serves xapprove
    and the Xbox family read, and its ProofKey signs the requests. Without it
    every call raises XapproveUnavailable. ``method`` says how xapprove's
    Authorization header is made.
    """

    def __init__(
        self,
        session_getter: Callable[[], aiohttp.ClientSession],
        *,
        method: AuthMethod = DEFAULT_METHOD,
        sisu: Any = None,
    ) -> None:
        self.sisu = sisu
        self._session_getter = session_getter
        self.method = method
        #: xuid -> lock: one write at a time per child.
        self._write_locks: dict[str, asyncio.Lock] = {}

    def forget_tokens(self) -> None:
        if self.sisu is not None:
            self.sisu.forget_tokens()

    def _need_sisu(self) -> Any:
        if self.sisu is None:
            raise XapproveUnavailable("No Xbox Family Settings sign-in.")
        return self.sisu

    @property
    def parent_xuid(self) -> str | None:
        tokens = getattr(self.sisu, "_tokens", None) if self.sisu is not None else None
        return tokens.parent_xuid if tokens else None

    async def async_tokens(self, *, force: bool = False) -> XboxTokens:
        """SISU's XSTS for http://xboxlive.com (family read, parent xuid)."""
        return await self._need_sisu().async_xsts(force=force)

    async def async_auth_headers(
        self,
        method: AuthMethod | None = None,
        *,
        force: bool = False,
        http_method: str = "GET",
        url: str = "",
        body: bytes = b"",
    ) -> dict[str, str]:
        """The headers of one request made ``method``'s way: Authorization,
        Signature for a signed way, the contract header, the app's headers."""
        method = method or self.method
        sisu = self._need_sisu()
        if method.token in ("msa_wlid", "msa_xbl"):
            if force:
                sisu.forget_tokens()
            token = await sisu.async_access_token(method.msa_scope or XAPPROVE_SCOPE)
            if method.token == "msa_wlid":
                value = f"WLID1.0 t={token}"
            else:
                value = f"XBL3.0 x={(await sisu.async_xsts()).uhs};{token}"
        elif method.token == "xsts":
            value = (await sisu.async_title_xsts(method.relying_party or RP_XBOXLIVE, force=force)).header()
        else:
            value = (await sisu.async_xsts(force=force)).header()
        headers = app_headers() if method.app_like else {}
        headers["Authorization"] = value
        if method.signed:
            headers["Signature"] = sisu.sign(http_method, url, value, body)
        if method.contract:
            headers["x-xbl-contract-version"] = method.contract
        return headers

    async def _request(
        self, method: str, url: str, *, json: Any = None, extra_headers: dict | None = None,
        step: str = "xapprove",
    ) -> Any:
        """One authorised request; a 401/403 is retried once with fresh tokens.

        xapprove uses ``self.method``; the family read SISU's XSTS, signed.
        """
        body = jsonlib.dumps(json, separators=(",", ":")).encode() if json is not None else b""
        for attempt in (0, 1):
            if step == "family":
                headers = await self.async_auth_headers(
                    FAMILY_METHOD, force=attempt == 1, http_method=method, url=url, body=body
                )
            else:
                headers = await self.async_auth_headers(
                    force=attempt == 1, http_method=method, url=url, body=body
                )
            headers["Accept"] = "application/json"
            headers.update(extra_headers or {})
            kwargs: dict[str, Any] = {"headers": headers}
            if json is not None:
                # The exact bytes that were signed.
                headers["Content-Type"] = "application/json"
                kwargs["data"] = body
            status, payload, resp_headers = await _call(self._session_getter(), method, url, **kwargs)
            if status in (401, 403):
                if attempt == 0:
                    continue
                raise XapproveRefused(step, status, _xerr(payload, resp_headers))
            if not 200 <= status < 300:
                raise XapproveError(f"{step}: HTTP {status}, XErr {_xerr(payload, resp_headers)}")
            return payload
        raise XapproveError(step)  # not reached

    async def async_try(self, method: AuthMethod, url: str) -> dict[str, Any]:
        """probe_xapprove: one read-only GET with ``method``, no retry; facts only.

        ``state`` (the parsed overrides) is added on a 2xx answer.
        """
        facts: dict[str, Any] = {"name": method.name}
        try:
            headers = await self.async_auth_headers(method, http_method="GET", url=url)
        except XapproveRefused as err:
            facts.update({"stage": "token", **err.facts()})
            return facts
        except XapproveError as err:
            facts.update({"stage": "token", "error": str(err)})
            return facts
        headers["Accept"] = "application/json"
        try:
            status, payload, resp_headers = await _call(
                self._session_getter(), "GET", url, headers=headers
            )
        except XapproveError as err:
            facts.update({"stage": "xapprove", "error": str(err)})
            return facts
        facts.update({"stage": "xapprove", "status": status, "xerr": _xerr(payload, resp_headers)})
        www = _www_authenticate(resp_headers)
        if www is not None:
            facts["www_authenticate"] = www
        if 200 <= status < 300:
            facts["state"] = parse_overrides(payload)
        return facts

    # Reads ------------------------------------------------------------------

    async def async_get_overrides(self, xuid: str) -> dict[str, Any]:
        payload = await self._request("GET", overrides_url(xuid))
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
            f"{overrides_url(xuid)}/create",
            json={
                "overrideType": override_type,
                "target": TARGET_XBOX_SCREEN_TIME,
                "validUntil": iso_utc(valid_until),
            },
        )
        return parse_overrides(payload)

    async def async_cancel(self, xuid: str) -> dict[str, Any] | None:
        return await self.async_create(xuid, OVERRIDE_CANCEL, dt_util.utcnow())


def overrides_url(xuid: str) -> str:
    return f"{XAPPROVE_HOST}/api/v1/user/{xuid}/settings/screentime/overrides"
