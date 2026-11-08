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
import json as jsonlib
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
# Tokens
# ----------------------------------------------------------------------

XBOXLIVE_RP = "http://xboxlive.com"
#: An MSA scope for xapprove itself (tried by probe_xapprove).
XAPPROVE_SCOPE = "service::xapprove.xboxlive.com::MBI_SSL"
#: Not an MSA scope: the integration's own Family access token (scope
#: service::familymobile.microsoft.com::MBI_SSL), which it refreshes anyway.
FAMILY_TOKEN = "family_token"


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


@dataclass(frozen=True)
class AuthMethod:
    """How the Authorization header for xapprove is made.

    ``scheme``:

    * ``xbl3``: ``XBL3.0 x=<uhs>;<xsts>``, the XSTS token for
      ``relying_party`` from an Xbox user token whose RPS ticket is an MSA
      access token for ``msa_scope``;
    * ``wlid_t`` / ``wlid_d``: ``WLID1.0 t=<token>`` / ``d=<token>``;
    * ``bearer``: ``Bearer <token>``;
    * ``msauth``: ``MSAuth1.0 usertoken="<token>", type="MSACT"`` (what the
      Family Safety mobile API takes);

    the last four with the MSA access token for ``msa_scope`` itself;
    * ``sisu``: ``XBL3.0 x=<uhs>;<xsts>`` with the XSTS of the Xbox Family
      Settings sign-in (xbox_sisu.py), the request signed with its ProofKey
      (``Signature`` header); ``sisu_unsigned`` the same without signing;
    * ``sisu_wlid``: ``WLID1.0 t=<token>``, an MSA token of the Xbox Family
      Settings client for ``msa_scope``, signed.

    ``contract``: an ``x-xbl-contract-version`` header, or none.
    """

    name: str
    scheme: str = "xbl3"
    msa_scope: str = XBOX_SCOPE
    relying_party: str = XBOXLIVE_RP
    contract: str | None = None


def _contracts(name: str, **kwargs: Any) -> list[AuthMethod]:
    return [AuthMethod(name, **kwargs)] + [
        AuthMethod(f"{name}_contract_{n}", contract=str(n), **kwargs) for n in (1, 2, 3)
    ]


#: The ways that need the Xbox Family Settings sign-in (SISU).
SISU_SCHEMES = frozenset({"sisu", "sisu_unsigned", "sisu_wlid"})

#: Every way probe_xapprove tries, in order: the Xbox Family Settings sign-in
#: first (live 2026-11-08, every other way got 401 token_required), then the
#: ways from the Family Safety sign-in, kept as diagnostics.
PROBE_METHODS: list[AuthMethod] = [
    AuthMethod("sisu", scheme="sisu"),
    AuthMethod("sisu_unsigned", scheme="sisu_unsigned"),
    AuthMethod("sisu_contract_1", scheme="sisu", contract="1"),
    AuthMethod("sisu_xapprove_scope_wlid", scheme="sisu_wlid", msa_scope=XAPPROVE_SCOPE),
    # a. The XSTS for http://xboxlive.com, with and without a contract version.
    *_contracts("xbl3"),
    # b. An XSTS for xapprove's own relying party.
    *_contracts("xbl3_rp_http_xapprove", relying_party="http://xapprove.xboxlive.com"),
    *_contracts("xbl3_rp_https_xapprove", relying_party="https://xapprove.xboxlive.com/"),
    # c. An MSA token for xapprove's scope: directly, then as the user ticket.
    AuthMethod("wlid_t_xapprove_scope", scheme="wlid_t", msa_scope=XAPPROVE_SCOPE),
    AuthMethod("wlid_d_xapprove_scope", scheme="wlid_d", msa_scope=XAPPROVE_SCOPE),
    AuthMethod("bearer_xapprove_scope", scheme="bearer", msa_scope=XAPPROVE_SCOPE),
    AuthMethod("msauth_xapprove_scope", scheme="msauth", msa_scope=XAPPROVE_SCOPE),
    AuthMethod("xbl3_xapprove_scope_ticket", msa_scope=XAPPROVE_SCOPE),
    AuthMethod(
        "xbl3_xapprove_scope_ticket_rp_http_xapprove",
        msa_scope=XAPPROVE_SCOPE, relying_party="http://xapprove.xboxlive.com",
    ),
    # d. Other relying-party spelling; the Xbox user-auth token itself; the
    # integration's own Family token, which costs no extra use of the sign-in.
    AuthMethod("xbl3_rp_rp_xapprove", relying_party="rp://xapprove.xboxlive.com/"),
    AuthMethod("wlid_t_userauth_scope", scheme="wlid_t"),
    AuthMethod("msauth_family_token", scheme="msauth", msa_scope=FAMILY_TOKEN),
    AuthMethod("wlid_t_family_token", scheme="wlid_t", msa_scope=FAMILY_TOKEN),
    AuthMethod("xbl3_family_token_ticket", msa_scope=FAMILY_TOKEN),
]
METHODS_BY_NAME: dict[str, AuthMethod] = {m.name: m for m in PROBE_METHODS}
#: Without the Xbox Family Settings sign-in.
DEFAULT_METHOD = METHODS_BY_NAME["xbl3"]
#: With it.
SISU_METHOD = METHODS_BY_NAME["sisu"]


def _record(steps: dict | None, name: str, value: Any) -> None:
    if steps is not None:
        steps[name] = value


def _suffix(scope: str, rp: str | None = None) -> str:
    """Step-name suffix for anything but the base chain (keeps the old names)."""
    parts = []
    if scope != XBOX_SCOPE:
        parts.append(scope)
    if rp is not None and rp != XBOXLIVE_RP:
        parts.append(rp)
    return f"[{' '.join(parts)}]" if parts else ""


async def async_msa_access_token(
    session: aiohttp.ClientSession,
    refresh_token: str,
    steps: dict | None = None,
    scope: str = XBOX_SCOPE,
) -> tuple[str, int | None]:
    """Step 1: (access token, expires_in). A refresh token in the answer is dropped unread."""
    status, payload, _headers = await _call(
        session, "POST", TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "client_id": CLIENT_ID,
            "scope": scope,
            "refresh_token": refresh_token,
        },
    )
    access = payload.get("access_token") if isinstance(payload, dict) else None
    error = payload.get("error") if isinstance(payload, dict) and isinstance(payload.get("error"), str) else None
    expires_in = payload.get("expires_in") if isinstance(payload, dict) else None
    _record(steps, f"msa_refresh{_suffix(scope)}", {
        "status": status,
        "error": error,
        "scope": payload.get("scope") if isinstance(payload, dict) and isinstance(payload.get("scope"), str) else None,
        "access_token_received": isinstance(access, str),
        "refresh_token_returned": bool(isinstance(payload, dict) and payload.get("refresh_token")),
        "refresh_token_discarded": True,
    })
    payload = None
    if status == 200 and isinstance(access, str):
        return access, expires_in if isinstance(expires_in, int) else None
    if status is not None and 400 <= status < 500:
        raise XapproveRefused("msa_refresh", status, error=error)
    raise XapproveError(f"msa_refresh: HTTP {status}")


async def async_user_token(
    session: aiohttp.ClientSession, access: str, steps: dict | None = None, label: str = ""
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
            _record(steps, f"user_authenticate{label}", attempts)
            return body["Token"], _not_after(body)
        last = (status, _xerr(body, headers))
    _record(steps, f"user_authenticate{label}", attempts)
    if last[0] and 400 <= last[0] < 500:
        raise XapproveRefused("user_authenticate", last[0], last[1])
    raise XapproveError(f"user_authenticate: HTTP {last[0]}")


async def async_xsts(
    session: aiohttp.ClientSession,
    user_token: str,
    steps: dict | None = None,
    relying_party: str = XBOXLIVE_RP,
    label: str = "",
) -> tuple[str, str, datetime | None, str | None]:
    """Step 3: (uhs, token, NotAfter, parent xuid or None)."""
    status, body, headers = await _call(
        session, "POST", XSTS_URL,
        json={
            "RelyingParty": relying_party,
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
    _record(steps, f"xsts_authorize{label}", {
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
    access, _expires = await async_msa_access_token(session, refresh_token, steps)
    user_token, user_until = await async_user_token(session, access, steps)
    access = None
    uhs, xsts, xsts_until, xid = await async_xsts(session, user_token, steps)
    return XboxTokens(uhs, xsts, xsts_until, user_token, user_until, xid)


def _www_authenticate(headers: dict[str, str]) -> dict[str, str] | None:
    for name, value in headers.items():
        if str(name).lower() == "www-authenticate":
            return {"name": str(name), "value": str(value)[:300]}
    return None


# ----------------------------------------------------------------------
# The client
# ----------------------------------------------------------------------


class XapproveClient:
    """xapprove reads and writes with cached Xbox tokens.

    ``refresh_token_source`` is an async context manager factory yielding the
    stored mobile refresh token while the integration's own token refresh is
    held off (the authenticator's login lock). ``family_token_getter`` gives
    the integration's current Family access token (for the methods that use
    it). ``sisu`` is the Xbox Family Settings sign-in (xbox_sisu.XboxSisuAuth)
    when there is one: its XSTS then also serves the Xbox family read, and its
    ProofKey signs the requests. ``method`` says how xapprove's Authorization
    header is made.

    Tokens are cached in memory per MSA scope (access token, Xbox user token)
    and per scope and relying party (XSTS) until shortly before they expire.
    ``token_facts`` keeps the non-secret facts of each token step.
    """

    def __init__(
        self,
        session_getter: Callable[[], aiohttp.ClientSession],
        refresh_token_source: Callable[[], Any],
        *,
        method: AuthMethod = DEFAULT_METHOD,
        family_token_getter: Callable[[], str | None] | None = None,
        sisu: Any = None,
    ) -> None:
        self.sisu = sisu
        self._session_getter = session_getter
        self._refresh_token_source = refresh_token_source
        self._family_token_getter = family_token_getter
        self.method = method
        self._access: dict[str, tuple[str, datetime | None]] = {}
        self._user: dict[str, tuple[str, datetime | None]] = {}
        self._xsts: dict[tuple[str, str], XboxTokens] = {}
        #: Token steps that failed, so another method needing the same one does
        #: not ask again (each MSA ask is a use of the stored sign-in). Cleared
        #: with the tokens.
        self._failed: dict[Any, XapproveError] = {}
        self._token_lock = asyncio.Lock()
        self.token_facts: dict[str, Any] = {}
        #: xuid -> lock: one write at a time per child.
        self._write_locks: dict[str, asyncio.Lock] = {}

    def forget_tokens(self) -> None:
        self._access.clear()
        self._user.clear()
        self._xsts.clear()
        self._failed.clear()
        if self.sisu is not None:
            self.sisu.forget_tokens()

    def seed(self, tokens: XboxTokens) -> None:
        """Start from a base chain already run (scope MBI_SSL, http://xboxlive.com)."""
        self._user[XBOX_SCOPE] = (tokens.user_token, tokens.user_not_after)
        self._xsts[(XBOX_SCOPE, XBOXLIVE_RP)] = tokens

    @property
    def parent_xuid(self) -> str | None:
        tokens = (
            getattr(self.sisu, "_tokens", None) if self.sisu is not None
            else self._xsts.get((XBOX_SCOPE, XBOXLIVE_RP))
        )
        return tokens.parent_xuid if tokens else None

    # Token steps (callers hold _token_lock) -----------------------------------

    async def _msa_access(self, scope: str) -> str:
        if scope == FAMILY_TOKEN:
            token = self._family_token_getter() if self._family_token_getter else None
            if not token:
                raise XapproveUnavailable("No Family access token at hand.")
            return token
        cached = self._access.get(scope)
        if cached is not None and _valid(cached[1]):
            return cached[0]
        if ("msa", scope) in self._failed:
            raise self._failed[("msa", scope)]
        try:
            async with self._refresh_token_source() as refresh_token:
                access, expires_in = await async_msa_access_token(
                    self._session_getter(), refresh_token, self.token_facts, scope
                )
        except XapproveRefused as err:
            self._failed[("msa", scope)] = err
            raise
        until = dt_util.utcnow() + timedelta(seconds=expires_in) if expires_in else None
        self._access[scope] = (access, until)
        return access

    async def _user_token(self, scope: str, *, fresh: bool = False) -> tuple[str, datetime | None]:
        cached = self._user.get(scope)
        if cached is not None and not fresh and _valid(cached[1]):
            return cached
        if fresh:
            self._access.pop(scope, None)
        elif ("user", scope) in self._failed:
            raise self._failed[("user", scope)]
        access = await self._msa_access(scope)
        try:
            token, until = await async_user_token(
                self._session_getter(), access, self.token_facts, _suffix(scope)
            )
        except XapproveRefused as err:
            self._failed[("user", scope)] = err
            raise
        self._user[scope] = (token, until)
        return token, until

    async def _xsts_tokens(self, scope: str, rp: str, force: bool = False) -> XboxTokens:
        cached = self._xsts.get((scope, rp))
        if cached is not None and not force and _valid(cached.xsts_not_after):
            return cached
        if not force and ("xsts", scope, rp) in self._failed:
            raise self._failed[("xsts", scope, rp)]
        had_user = scope in self._user and _valid(self._user[scope][1])
        user, user_until = await self._user_token(scope)
        session = self._session_getter()
        label = _suffix(scope, rp)
        try:
            uhs, xsts, until, xid = await async_xsts(session, user, self.token_facts, rp, label)
        except XapproveRefused as err:
            if not had_user or rp != XBOXLIVE_RP:
                # Another relying party refusing says nothing about the user token.
                self._failed[("xsts", scope, rp)] = err
                raise
            # The cached user token may have gone stale: once more, from fresh.
            user, user_until = await self._user_token(scope, fresh=True)
            uhs, xsts, until, xid = await async_xsts(session, user, self.token_facts, rp, label)
        tokens = XboxTokens(uhs, xsts, until, user, user_until, xid)
        self._xsts[(scope, rp)] = tokens
        return tokens

    # Public token entry points ------------------------------------------------

    async def async_tokens(self, *, force: bool = False) -> XboxTokens:
        """The XSTS for http://xboxlive.com used for the family read and the
        parent's xuid: the Xbox Family Settings sign-in's when there is one,
        else the one from the Family Safety sign-in."""
        if self.sisu is not None:
            return await self.sisu.async_xsts(force=force)
        async with self._token_lock:
            return await self._xsts_tokens(XBOX_SCOPE, XBOXLIVE_RP, force)

    async def async_auth_headers(
        self,
        method: AuthMethod | None = None,
        *,
        force: bool = False,
        http_method: str = "GET",
        url: str = "",
        body: bytes = b"",
    ) -> dict[str, str]:
        """The Authorization header (and Signature, for a signed way) of one request."""
        method = method or self.method
        if method.scheme in SISU_SCHEMES:
            if self.sisu is None:
                raise XapproveUnavailable("No Xbox Family Settings sign-in.")
            if method.scheme == "sisu_wlid":
                if force:
                    self.sisu.forget_tokens()
                value = f"WLID1.0 t={await self.sisu.async_access_token(method.msa_scope)}"
            else:
                value = (await self.sisu.async_xsts(force=force)).header()
            headers = {"Authorization": value}
            if method.scheme != "sisu_unsigned":
                headers["Signature"] = self.sisu.sign(http_method, url, value, body)
            if method.contract:
                headers["x-xbl-contract-version"] = method.contract
            return headers
        async with self._token_lock:
            if method.scheme == "xbl3":
                tokens = await self._xsts_tokens(method.msa_scope, method.relying_party, force)
                value = tokens.header()
            else:
                if force and method.msa_scope != FAMILY_TOKEN:
                    self._access.pop(method.msa_scope, None)
                token = await self._msa_access(method.msa_scope)
                value = {
                    "wlid_t": f"WLID1.0 t={token}",
                    "wlid_d": f"WLID1.0 d={token}",
                    "bearer": f"Bearer {token}",
                    "msauth": f'MSAuth1.0 usertoken="{token}", type="MSACT"',
                }[method.scheme]
        headers = {"Authorization": value}
        if method.contract:
            headers["x-xbl-contract-version"] = method.contract
        return headers

    async def _request(
        self, method: str, url: str, *, json: Any = None, extra_headers: dict | None = None,
        step: str = "xapprove",
    ) -> Any:
        """One authorised request; a 401/403 is retried once with fresh tokens.

        xapprove uses ``self.method``; the family read the base XSTS.
        """
        body = jsonlib.dumps(json, separators=(",", ":")).encode() if json is not None else b""
        for attempt in (0, 1):
            if step == "family":
                tokens = await self.async_tokens(force=attempt == 1)
                headers = {"Authorization": tokens.header()}
                if self.sisu is not None:
                    headers["Signature"] = self.sisu.sign(method, url, headers["Authorization"], body)
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
