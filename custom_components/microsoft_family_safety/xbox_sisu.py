"""Signing in as the Xbox Family Settings app (Xbox SISU), for xapprove.

xapprove (the Xbox Family Settings API) answered 401 ``token_required`` to
every token the Family Safety sign-in can produce: it wants the token the
Xbox Family Settings app itself gets through the Xbox Authentication Library
(XAL) "SISU" flow. This module does that flow with the app's public client id,
as the open-source XAL implementations do (xal-node, the XAL code of the
Python xbox-webapi). It is **unofficial use of a Microsoft first-party client
id**: Microsoft can change or block it at any time, and the integration then
falls back to its other Xbox lock paths.

The flow:

1. The user signs in at ``login.live.com`` (authorization code with PKCE,
   client ``00000000482C8F49``, scope ``service::user.auth.xboxlive.com::MBI_SSL``,
   redirect ``https://login.live.com/oauth20_desktop.srf``) and pastes the
   redirect URL back into the options flow; the code is exchanged for MSA
   tokens. Only the refresh token is kept.
2. An EC P-256 key (the "ProofKey") is made per entry and kept. Every Xbox
   auth request is signed with it (the ``Signature`` header below).
3. ``device.auth.xboxlive.com/device/authenticate`` gives a device token.
4. ``sisu.xboxlive.com/authorize`` with the MSA access token, the device
   token and the ProofKey gives the user, title and device tokens and an
   XSTS token for ``http://xboxlive.com`` (``AuthorizationToken``) with the
   ``uhs`` claim: ``Authorization: XBL3.0 x=<uhs>;<xsts>``.
5. As the app does next (capture, 2026-11): ``xsts.auth.xboxlive.com/xsts/
   authorize``, signed, with SISU's user token, the device token and SISU's
   title token, for a relying party (``http://xboxlive.com``; probe_xapprove
   also tries xapprove's own), gives the title-bound XSTS sent to xapprove.
6. When an XSTS runs out, the MSA token is refreshed (the new refresh token
   replaces the old one) and steps 4 and 5 run again. Only the refresh
   token, the ProofKey and a device id are stored.

The ``Signature`` header (Xbox request signing, policy version 1): base64 of
``version (uint32 BE) | timestamp (uint64 BE, Windows FILETIME) | r | s``,
where ``r|s`` (32 bytes each) is the ECDSA P-256/SHA-256 signature of::

    version(uint32 BE) 0x00 timestamp(uint64 BE) 0x00 METHOD 0x00
    path-and-query 0x00 authorization 0x00 body[:8192] 0x00

Nothing secret is logged or returned.
"""
from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import hashlib
import json
import logging
import re
import secrets
import struct
import time
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit
import uuid

import aiohttp
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from homeassistant.util import dt as dt_util

from .xapprove import (
    XapproveError,
    XapproveRefused,
    RP_XBOXLIVE,
    XboxTokens,
    _call,
    _not_after,
    _call_text,
    _valid,
    redact,
    _xerr,
)

_LOGGER = logging.getLogger(__name__)

CLIENT_ID = "00000000482C8F49"
SCOPE = "service::user.auth.xboxlive.com::MBI_SSL"
XAPPROVE_SCOPE = "service::xapprove.xboxlive.com::MBI_SSL"
REDIRECT_URI = "https://login.live.com/oauth20_desktop.srf"
AUTHORIZE_URL = "https://login.live.com/oauth20_authorize.srf"
TOKEN_URL = "https://login.live.com/oauth20_token.srf"
DEVICE_URL = "https://device.auth.xboxlive.com/device/authenticate"
SISU_AUTHORIZE_URL = "https://sisu.xboxlive.com/authorize"
XSTS_AUTHORIZE_URL = "https://xsts.auth.xboxlive.com/xsts/authorize"
#: The device-code sign-in ("go to microsoft.com/link and enter ABCD-1234").
DEVICE_CODE_URL = "https://login.live.com/oauth20_connect.srf"
GRANT_DEVICE_CODE = "urn:ietf:params:oauth:grant-type:device_code"
RELYING_PARTY = "http://xboxlive.com"
#: The XAL user agent of the app's XSTS step (capture, 2026-11).
XAL_USER_AGENT = "XAL Android 2026.05.20260812.001"
#: The optional ``xui`` claims the app asks the XSTS step for.
OPTIONAL_DISPLAY_CLAIMS = ["mgt", "umg", "agg", "prv", "ugc"]

SIGNATURE_VERSION = 1
SIGNATURE_MAX_BODY = 8192
_FILETIME_EPOCH = 11644473600  # seconds from 1601-01-01 to 1970-01-01


class XboxSignInExpired(XapproveRefused):
    """The Xbox sign-in's refresh token no longer works: sign in again."""


# ----------------------------------------------------------------------
# PKCE, the authorize URL, the redirect
# ----------------------------------------------------------------------


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def new_pkce() -> tuple[str, str]:
    """(code_verifier, S256 code_challenge)."""
    verifier = _b64url(secrets.token_bytes(48))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


def authorize_url(challenge: str, state: str) -> str:
    return f"{AUTHORIZE_URL}?" + urlencode({
        "client_id": CLIENT_ID,
        "response_type": "code",
        "scope": SCOPE,
        "redirect_uri": REDIRECT_URI,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "display": "android_phone",
        "prompt": "select_account",
    })


#: A bare authorization code from login.live.com ("M.C5xx_BAY.2.U.1234-...").
_BARE_CODE = re.compile(r"M\.[A-Za-z0-9._!*$-]{8,}")


def code_from_redirect(url: str, state: str) -> str:
    """The code in the pasted redirect URL. ValueError names what is wrong.

    Forgiving about how it was copied: spaces and line breaks, quotes or angle
    brackets around it, a missing ``https://``, or only the code itself (the
    PKCE verifier still binds the code to this sign-in, so the state is only
    compared when it was pasted).
    """
    text = re.sub(r"\s+", "", url or "").strip("\"'<>`")
    if _BARE_CODE.fullmatch(text):
        return text
    if text.lower().startswith("login.live.com/"):
        text = f"https://{text}"
    parts = urlsplit(text)
    query = parse_qs(parts.query)
    query.update({k: v for k, v in parse_qs(parts.fragment).items() if k not in query})
    if query.get("error"):
        raise ValueError(f"signin_error:{query['error'][0]}")
    if parts.netloc.lower() == "login.live.com" and parts.path.endswith("oauth20_authorize.srf"):
        raise ValueError("pasted_link")
    if "removed" in query and not query.get("code"):
        # The blank page replaces its own address with "?removed=true" once
        # it has loaded: the code has to be taken from the browser's network
        # log instead of the address bar.
        raise ValueError("removed_address")
    if parts.scheme != "https" or parts.netloc.lower() != "login.live.com" or not query.get("code"):
        raise ValueError("invalid_redirect")
    pasted_state = query.get("state", [None])[0]
    if pasted_state is not None and pasted_state != state:
        raise ValueError("state_mismatch")
    return query["code"][0]


# ----------------------------------------------------------------------
# The ProofKey and request signing
# ----------------------------------------------------------------------


def _filetime(moment: float | None = None) -> int:
    return int(((moment if moment is not None else time.time()) + _FILETIME_EPOCH) * 10_000_000)


def signed_payload(
    method: str, path_and_query: str, authorization: str, body: bytes, timestamp: int
) -> bytes:
    """The bytes the Signature header signs (policy version 1)."""
    ts = struct.pack(">Q", timestamp)
    return b"".join([
        struct.pack(">I", SIGNATURE_VERSION), b"\x00",
        ts, b"\x00",
        method.upper().encode(), b"\x00",
        path_and_query.encode(), b"\x00",
        (authorization or "").encode(), b"\x00",
        (body or b"")[:SIGNATURE_MAX_BODY], b"\x00",
    ])


def path_and_query(url: str) -> str:
    parts = urlsplit(url)
    return (parts.path or "/") + (f"?{parts.query}" if parts.query else "")


class ProofKey:
    """The entry's EC P-256 key: its JWK goes to Microsoft, it signs requests."""

    def __init__(self, key: ec.EllipticCurvePrivateKey) -> None:
        self._key = key

    @classmethod
    def generate(cls) -> ProofKey:
        return cls(ec.generate_private_key(ec.SECP256R1()))

    @classmethod
    def from_pem(cls, pem: str) -> ProofKey:
        key = serialization.load_pem_private_key(pem.encode(), password=None)
        if not isinstance(key, ec.EllipticCurvePrivateKey):
            raise ValueError("not an EC key")
        return cls(key)

    def to_pem(self) -> str:
        return self._key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()

    @property
    def public_key(self) -> ec.EllipticCurvePublicKey:
        return self._key.public_key()

    def jwk(self) -> dict[str, str]:
        numbers = self.public_key.public_numbers()
        return {
            "kty": "EC",
            "x": _b64url(numbers.x.to_bytes(32, "big")),
            "y": _b64url(numbers.y.to_bytes(32, "big")),
            "crv": "P-256",
            "alg": "ES256",
            "use": "sig",
        }

    def sign(
        self,
        method: str,
        url: str,
        authorization: str = "",
        body: bytes = b"",
        timestamp: int | None = None,
    ) -> str:
        """The ``Signature`` header value for one request."""
        ts = timestamp if timestamp is not None else _filetime()
        der = self._key.sign(
            signed_payload(method, path_and_query(url), authorization, body, ts),
            ec.ECDSA(hashes.SHA256()),
        )
        r, s = decode_dss_signature(der)
        raw = struct.pack(">I", SIGNATURE_VERSION) + struct.pack(">Q", ts)
        raw += r.to_bytes(32, "big") + s.to_bytes(32, "big")
        return base64.b64encode(raw).decode()


# ----------------------------------------------------------------------
# The requests
# ----------------------------------------------------------------------


@dataclass
class MsaTokens:
    access_token: str
    refresh_token: str | None
    expires_at: datetime | None


def _record(facts: dict | None, name: str, value: Any) -> None:
    if facts is not None:
        facts[name] = value


async def _msa_token(
    session: aiohttp.ClientSession, form: dict[str, str], step: str, facts: dict | None
) -> MsaTokens:
    status, payload, _headers = await _call(session, "POST", TOKEN_URL, data=form)
    ok = isinstance(payload, dict) and isinstance(payload.get("access_token"), str)
    error = payload.get("error") if isinstance(payload, dict) and isinstance(payload.get("error"), str) else None
    _record(facts, step, {
        "status": status,
        "error": error,
        "scope": payload.get("scope") if isinstance(payload, dict) and isinstance(payload.get("scope"), str) else None,
        "access_token_received": ok,
        "refresh_token_received": bool(isinstance(payload, dict) and payload.get("refresh_token")),
    })
    if status == 200 and ok:
        expires = payload.get("expires_in")
        return MsaTokens(
            payload["access_token"],
            payload.get("refresh_token") if isinstance(payload.get("refresh_token"), str) else None,
            dt_util.utcnow() + timedelta(seconds=int(expires)) if isinstance(expires, int) else None,
        )
    if status is not None and 400 <= status < 500:
        if error in ("invalid_grant", "interaction_required", "unauthorized_client"):
            raise XboxSignInExpired(step, status, error=error)
        raise XapproveRefused(step, status, error=error)
    raise XapproveError(f"{step}: HTTP {status}")


async def async_exchange_code(
    session: aiohttp.ClientSession, code: str, verifier: str, facts: dict | None = None
) -> MsaTokens:
    return await _msa_token(session, {
        "client_id": CLIENT_ID,
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": verifier,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE,
    }, "sisu_msa_code", facts)


async def async_refresh_msa(
    session: aiohttp.ClientSession, refresh_token: str, scope: str = SCOPE, facts: dict | None = None
) -> MsaTokens:
    step = "sisu_msa_refresh" if scope == SCOPE else f"sisu_msa_refresh[{scope}]"
    return await _msa_token(session, {
        "client_id": CLIENT_ID,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "scope": scope,
    }, step, facts)


async def _signed_post(
    session: aiohttp.ClientSession, key: ProofKey, url: str, body: dict, step: str, facts: dict | None,
    extra_headers: dict[str, str] | None = None,
    *,
    signed: bool = True,
    contract: str | None = "1",
) -> tuple[int, Any, dict[str, str]]:
    raw = json.dumps(body, separators=(",", ":")).encode()
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        **({"x-xbl-contract-version": contract} if contract else {}),
        **(extra_headers or {}),
    }
    if signed:
        headers["Signature"] = key.sign("POST", url, "", raw)
    status, payload, resp_headers = await _call(session, "POST", url, data=raw, headers=headers)
    _record(facts, step, {"status": status, "xerr": _xerr(payload, resp_headers)})
    return status, payload, resp_headers


def _claims(token: Any, name: str) -> dict[str, Any]:
    """The first ``name`` claim set (xui, xti, xdi) of a token answer ({} if none)."""
    if isinstance(token, dict):
        items = (token.get("DisplayClaims") or {}).get(name)
        if isinstance(items, list) and items and isinstance(items[0], dict):
            return items[0]
        if isinstance(items, dict):
            return items
    return {}


def _xui(token: Any) -> dict[str, Any]:
    """The first ``xui`` claim set of an XSTS answer ({} if none)."""
    return _claims(token, "xui")


def _title_id(claims: dict[str, Any]) -> str | None:
    tid = claims.get("tid")
    return str(tid) if isinstance(tid, (str, int)) and not isinstance(tid, bool) and str(tid) else None


async def async_device_token(
    session: aiohttp.ClientSession, key: ProofKey, device_id: str, facts: dict | None = None
) -> tuple[str, datetime | None]:
    status, payload, headers = await _signed_post(session, key, DEVICE_URL, {
        "RelyingParty": "http://auth.xboxlive.com",
        "TokenType": "JWT",
        "Properties": {
            "AuthMethod": "ProofOfPossession",
            "Id": f"{{{device_id}}}",
            "DeviceType": "Android",
            "SerialNumber": f"{{{device_id}}}",
            "Version": "15.0",
            "ProofKey": key.jwk(),
        },
    }, "device_authenticate", facts)
    xdi = _claims(payload, "xdi")
    if facts is not None and isinstance(facts.get("device_authenticate"), dict):
        # Claim names only, and the title id if there is one (an id, not a secret).
        facts["device_authenticate"].update({
            "xdi_keys": sorted(str(k) for k in xdi),
            "xdi_tid": redact(_title_id(xdi) or "") or None,
        })
    if status == 200 and isinstance(payload, dict) and isinstance(payload.get("Token"), str):
        return payload["Token"], _not_after(payload)
    if status and 400 <= status < 500:
        raise XapproveRefused("device_authenticate", status, _xerr(payload, headers))
    raise XapproveError(f"device_authenticate: HTTP {status}")


@dataclass(frozen=True)
class SisuProfile:
    """How sisu/authorize is asked (probe_xapprove tries several).

    ``ucs``: send the consent service tokens (UcsCheckinToken, UcsWebToken)
    the app gets before authorize, ``ucs_form`` ``raw`` or ``t`` (``t=<token>``);
    ``prompts``: ``UserPrompts: []``; ``contract``: the
    ``x-xbl-contract-version`` header (the app sent none; the plain profile
    keeps the 1 that has always worked).
    """

    name: str
    ucs: bool = False
    ucs_form: str = "raw"
    prompts: bool = False
    contract: str | None = "1"
    #: Replay the web view's proxy reads (WebPage) after authorize, as the
    #: app does, before the XSTS; ``reauth``: authorize once more after.
    webview: bool = False
    reauth: bool = False
    #: Run sisu/authenticate first and send its X-SessionId as SessionId.
    session: bool = False


PLAIN_PROFILE = SisuProfile("")


def _ucs_profiles() -> list[SisuProfile]:
    found = []
    for contract in (None, "1"):
        for prompts in (True, False):
            for form in ("raw", "t"):
                found.append(SisuProfile(
                    f"ucs_{form}_{'prompts' if prompts else 'noprompts'}_{'c1' if contract else 'noc'}",
                    ucs=True, ucs_form=form, prompts=prompts, contract=contract,
                ))
    return found


#: The plain profile, then the UCS ones, the app's (no contract, UserPrompts)
#: first.
SISU_PROFILES: dict[str, SisuProfile] = {
    p.name: p
    for p in [
        PLAIN_PROFILE,
        SisuProfile("webview", webview=True),
        SisuProfile("webview_reauth", webview=True, reauth=True),
        SisuProfile("session", session=True, webview=True),
        SisuProfile("session_ucs", ucs=True, prompts=True, contract=None, session=True, webview=True),
        *_ucs_profiles(),
    ]
}
#: The consent service scopes the app asks for before sisu/authorize.
UCS_CHECKIN_SCOPE = "https://consentservice.microsoft.com/checkin/UnifiedUserConsent.Read"
UCS_WEB_SCOPE = "https://consentservice.microsoft.com/web/UnifiedUserConsent.ReadWrite"


def sisu_step(profile: SisuProfile) -> str:
    return "sisu_authorize" if not profile.name else f"sisu_authorize[{profile.name}]"


async def async_sisu_authorize(
    session: aiohttp.ClientSession,
    key: ProofKey,
    msa_access_token: str,
    device_token: str,
    facts: dict | None = None,
    profile: SisuProfile = PLAIN_PROFILE,
    ucs: tuple[str, str] | None = None,
    step: str | None = None,
    session_id: str | None = None,
) -> XboxTokens:
    body: dict[str, Any] = {
        "AccessToken": f"t={msa_access_token}",
        "AppId": CLIENT_ID,
        "DeviceToken": device_token,
        "Sandbox": "RETAIL",
        "SiteName": "user.auth.xboxlive.com",
        "RelyingParty": RELYING_PARTY,
        "UseModernGamertag": True,
        "ProofKey": key.jwk(),
    }
    if profile.ucs and ucs is not None:
        prefix = "t=" if profile.ucs_form == "t" else ""
        body["UcsCheckinToken"] = f"{prefix}{ucs[0]}"
        body["UcsWebToken"] = f"{prefix}{ucs[1]}"
    if profile.prompts:
        body["UserPrompts"] = []
    if session_id:
        body["SessionId"] = session_id
    step = step or sisu_step(profile)
    status, payload, headers = await _signed_post(
        session, key, SISU_AUTHORIZE_URL, body, step, facts, contract=profile.contract
    )
    page = parse_web_page(payload.get("WebPage")) if isinstance(payload, dict) else None
    auth = payload.get("AuthorizationToken") if isinstance(payload, dict) else None
    xui = _xui(auth)
    title = payload.get("TitleToken") if isinstance(payload, dict) and isinstance(payload.get("TitleToken"), dict) else {}
    xti = _claims(title, "xti")
    if facts is not None and isinstance(facts.get(step), dict):
        facts[step].update({
            "authorization_token_received": bool(isinstance(auth, dict) and auth.get("Token")),
            "user_token_received": bool(isinstance(payload, dict) and payload.get("UserToken")),
            "title_token_received": bool(title.get("Token")),
            "device_token_received": bool(isinstance(payload, dict) and payload.get("DeviceToken")),
            "display_claims_xui_keys": sorted(str(k) for k in xui),
            # The title's claims: names, and the title id (an id, not a secret).
            "title_xti_keys": sorted(str(k) for k in xti),
            "title_id": redact(_title_id(xti) or "") or None,
            "response_keys": sorted(str(k) for k in payload) if isinstance(payload, dict) else None,
            "web_page": page.facts if page else None,
            "session_id_sent": bool(session_id),
        })
        if profile.ucs:
            facts[step]["ucs_sent"] = ucs is not None
    if status == 200 and isinstance(auth, dict) and isinstance(auth.get("Token"), str) and xui.get("uhs"):
        user = payload.get("UserToken") if isinstance(payload.get("UserToken"), dict) else {}
        device = payload.get("DeviceToken")
        xid = xui.get("xid")
        return XboxTokens(
            str(xui["uhs"]), auth["Token"], _not_after(auth),
            str(user.get("Token") or ""), _not_after(user) if user else None,
            str(xid) if xid else None,
            title_token=str(title.get("Token") or ""),
            title_not_after=_not_after(title) if title else None,
            xui_keys=tuple(sorted(str(k) for k in xui)),
            device_token=device if isinstance(device, str) else "",
            title_id=_title_id(xti),
            web_page=page,
        )
    if status and 400 <= status < 500:
        raise XapproveRefused(step, status, _xerr(payload, headers))
    raise XapproveError(f"{step}: HTTP {status}")


SISU_AUTHENTICATE_URL = "https://sisu.xboxlive.com/authenticate"
#: The Xbox Family Settings title id (seen live 2026-11-08 in SISU's
#: TitleToken), for sisu/authenticate before SISU has given one.
DEFAULT_TITLE_ID = "1618633878"


@dataclass
class SisuSession:
    """What sisu/authenticate gave: its session id and the MSA sign-in link
    (both secret-ish: never logged), and non-secret facts."""

    session_id: str | None = field(repr=False)
    msa_oauth_redirect: str | None = field(repr=False)
    facts: dict[str, Any] = field(default_factory=dict)


async def async_sisu_authenticate(
    session: aiohttp.ClientSession,
    key: ProofKey,
    device_token: str,
    title_id: str | None,
    *,
    challenge: str,
    state: str,
    redirect_uri: str = REDIRECT_URI,
    step: str = "sisu_authenticate",
    facts: dict | None = None,
) -> SisuSession:
    """sisu/authenticate as the app sends it (signed, contract 1): the
    session id (``X-SessionId``) and the MSA sign-in link
    (``MsaOauthRedirect``) for ``challenge`` and ``state``. Never raises for
    an HTTP status: the facts say how it went."""
    body: dict[str, Any] = {"AppId": CLIENT_ID}
    if title_id:
        body["TitleId"] = title_id
    body.update({
        "RedirectUri": redirect_uri,
        "DeviceToken": device_token,
        "Sandbox": "RETAIL",
        "TokenType": "code",
        "Offers": [SCOPE],
        "Query": {
            "display": "android_phone",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
            "prompt": "select_account",
        },
    })
    local: dict[str, Any] = {}
    status, payload, headers = await _signed_post(
        session, key, SISU_AUTHENTICATE_URL, body, step, local
    )
    params = payload.get("MsaRequestParameters") if isinstance(payload, dict) else None
    redirect = payload.get("MsaOauthRedirect") if isinstance(payload, dict) else None
    result = dict(local[step])
    result.update({
        "title_id_sent": bool(title_id),
        "response_keys": sorted(str(k) for k in payload) if isinstance(payload, dict) else None,
        "session_id_received": bool(isinstance(payload, dict) and payload.get("SessionId")),
        "msa_oauth_redirect_received": bool(redirect),
        "msa_oauth_redirect_query_keys": (
            sorted(parse_qs(urlsplit(redirect).query)) if isinstance(redirect, str) else None
        ),
        "msa_request_parameters_keys": sorted(str(k) for k in params) if isinstance(params, dict) else None,
        "session_id_header": any(str(n).lower() == "x-sessionid" for n in headers),
        "response_header_names": sorted(str(n) for n in headers),
    })
    _record(facts, step, result)
    sid = next((str(v) for n, v in headers.items() if str(n).lower() == "x-sessionid"), None)
    if sid is None and isinstance(payload, dict) and isinstance(payload.get("SessionId"), str):
        sid = payload["SessionId"]
    return SisuSession(sid, redirect if isinstance(redirect, str) else None, result)


async def async_sisu_authenticate_probe(
    session: aiohttp.ClientSession, key: ProofKey, device_token: str, title_id: str | None
) -> tuple[dict[str, Any], str | None]:
    """probe_xapprove only: one sisu/authenticate as the app sends it, to see
    what it answers: (facts, its session id). The facts are booleans and
    names only, never the session id or the sign-in link."""
    _verifier, challenge = new_pkce()
    result = await async_sisu_authenticate(
        session, key, device_token, title_id, challenge=challenge, state=secrets.token_urlsafe(16)
    )
    return result.facts, result.session_id


class XstsVariantSkipped(XapproveError):
    """A variant of the XSTS step that could not be sent (nothing was asked)."""


@dataclass(frozen=True)
class XstsVariant:
    """One way of asking the explicit XSTS step (probe_xapprove tries them).

    ``claims``: send OptionalDisplayClaims; ``title``: send SISU's TitleToken;
    ``sisu_device``: the DeviceToken from SISU's answer instead of the one
    from device/authenticate; ``signed``: the Signature header;
    ``charset``: ``application/json; charset=utf-8`` (else plain JSON).
    """

    name: str
    claims: bool = True
    title: bool = True
    sisu_device: bool = False
    signed: bool = True
    charset: bool = True


#: In order; the first that answers 200 is used (and remembered). Live
#: 2026-11-08 the app's request with OptionalDisplayClaims got 400 XErr
#: 2148916279 (0x8015DC37) for http://xboxlive.com and ``no_claims`` got 200:
#: it is the default, and every other variant leaves the claims out too
#: except ``app``.
XSTS_VARIANTS: list[XstsVariant] = [
    XstsVariant("no_claims", claims=False),
    XstsVariant("no_title", claims=False, title=False),
    XstsVariant("sisu_device", claims=False, sisu_device=True),
    XstsVariant("unsigned", claims=False, signed=False),
    XstsVariant("plain_json", claims=False, charset=False),
    XstsVariant("app"),
]
XSTS_VARIANTS_BY_NAME = {v.name: v for v in XSTS_VARIANTS}


def xsts_key(relying_party: str, profile: str = "") -> str:
    """The cache key of an explicit XSTS: the relying party, after the SISU
    profile when there is one."""
    return f"{profile}|{relying_party}" if profile else relying_party


def xsts_step(relying_party: str, variant: str = "no_claims", profile: str = "") -> str:
    """The name of the explicit XSTS step (facts)."""
    return f"xsts_authorize[{xsts_key(relying_party, profile)}|{variant}]"


async def async_xsts_authorize(
    session: aiohttp.ClientSession,
    key: ProofKey,
    sisu_tokens: XboxTokens,
    device_token: str,
    relying_party: str = RP_XBOXLIVE,
    facts: dict | None = None,
    variant: XstsVariant = XSTS_VARIANTS[0],
    profile: str = "",
) -> XboxTokens:
    """The app's explicit XSTS step: ``xsts.auth.xboxlive.com/xsts/authorize``
    with SISU's user token, the device token and SISU's title token (when one
    came), signed with the ProofKey, for ``relying_party``; ``variant``
    leaves parts out or changes them.

    The parent xuid is kept from SISU when the answer carries no ``xid``.
    """
    step = xsts_step(relying_party, variant.name, profile)
    device = sisu_tokens.device_token if variant.sisu_device else device_token
    if not device:
        _record(facts, step, {"skipped": "no DeviceToken in SISU's answer"})
        raise XstsVariantSkipped(f"{step}: no DeviceToken from SISU")
    properties: dict[str, Any] = {"SandboxId": "RETAIL", "DeviceToken": device}
    title_sent = bool(variant.title and sisu_tokens.title_token)
    if title_sent:
        properties["TitleToken"] = sisu_tokens.title_token
    properties["UserTokens"] = [sisu_tokens.user_token]
    if variant.claims:
        properties["OptionalDisplayClaims"] = list(OPTIONAL_DISPLAY_CLAIMS)
    status, payload, headers = await _signed_post(session, key, XSTS_AUTHORIZE_URL, {
        "RelyingParty": relying_party,
        "TokenType": "JWT",
        "Properties": properties,
    }, step, facts, {
        "Content-Type": "application/json; charset=utf-8" if variant.charset else "application/json",
        "User-Agent": XAL_USER_AGENT,
    }, signed=variant.signed)
    xui = _xui(payload)
    if facts is not None and isinstance(facts.get(step), dict):
        facts[step].update({
            "variant": variant.name,
            "title_token_sent": title_sent,
            "claims_sent": variant.claims,
            "signed": variant.signed,
            "device_token_from": "sisu" if variant.sisu_device else "device_authenticate",
            "token_received": bool(isinstance(payload, dict) and payload.get("Token")),
            "display_claims_xui_keys": sorted(str(k) for k in xui),
        })
    if status == 200 and isinstance(payload, dict) and isinstance(payload.get("Token"), str):
        xid = xui.get("xid")
        return XboxTokens(
            str(xui.get("uhs") or sisu_tokens.uhs), payload["Token"], _not_after(payload),
            sisu_tokens.user_token, sisu_tokens.user_not_after,
            str(xid) if xid else sisu_tokens.parent_xuid,
            xui_keys=tuple(sorted(str(k) for k in xui)),
        )
    if status and 400 <= status < 500:
        raise XapproveRefused(step, status, _xerr(payload, headers))
    raise XapproveError(f"{step}: HTTP {status}")


# ----------------------------------------------------------------------
# The SISU web view ("WebPage" in sisu/authorize's answer)
# ----------------------------------------------------------------------
#
# Live 2026-11-08 every sisu/authorize answered a WebPage. In the app's
# capture the app then opened it (sisu.xboxlive.com/client/v68/<app id>/view/
# index.html?MS-CV&redirect&sid&sig&state), the page POSTed
# sisu.xboxlive.com/proxy?sessionid=<sid> (x-xbl-contract-version 3) with
# {"ReadAccount":{}} and then {"ReadProfile":{}}, and only after that did
# the app ask for its XSTS tokens. How the page authorizes those POSTs is
# not in the capture: the ways below are guesses (see ProxyWay).

SISU_PROXY_URL = "https://sisu.xboxlive.com/proxy"
PROXY_READS = ("ReadAccount", "ReadProfile")
#: Words of a further step the page could ask for (reported, never called).
_FURTHER_STEP = re.compile(
    r"\b((?:Read|Write|Create|Accept|Set|Update|Complete|Finish|Claim|Change|Reserve|Verify|Consent)"
    r"[A-Z][A-Za-z]{2,40})\b"
)
_SCRIPT_SRC = re.compile(r"<script[^>]+src=[\"']([^\"']+)[\"']", re.IGNORECASE)


@dataclass(frozen=True)
class WebPage:
    """The parts of a WebPage URL that matter. Never logged; ``facts`` is
    what may be shown (names, shapes, no values)."""

    url: str = field(repr=False)
    sid: str | None = field(default=None, repr=False)
    sig: str | None = field(default=None, repr=False)
    facts: dict[str, Any] = field(default_factory=dict)


def _redact_path(path: str) -> str:
    """A URL path with each segment redacted on its own (a whole path is
    long enough to look like a token)."""
    return "/".join(redact(segment) for segment in path.split("/"))


def _shape(value: str) -> dict[str, Any]:
    """What a value looks like, not what it is."""
    return {
        "length": len(value),
        "base64_like": bool(re.fullmatch(r"[A-Za-z0-9+/_=-]+", value)),
        "dots": value.count("."),
        "jwt_like": value.startswith("eyJ"),
    }


def parse_web_page(url: Any) -> WebPage | None:
    """A WebPage URL and its non-secret facts: host, path (ids redacted),
    the segment that names the page (before the file name), query and
    fragment key names, whether ``sid``/``sig`` are present and the shape
    of ``sig``, the scheme of ``redirect``."""
    if not isinstance(url, str) or not url:
        return None
    parts = urlsplit(url)
    query = parse_qs(parts.query, keep_blank_values=True)
    fragment = parse_qs(parts.fragment, keep_blank_values=True)
    segments = [s for s in parts.path.split("/") if s]
    view = segments[-2] if len(segments) >= 2 and "." in segments[-1] else (segments[-1] if segments else None)
    sid = (query.get("sid") or [None])[0] or None
    sig = (query.get("sig") or [None])[0] or None
    redirect = (query.get("redirect") or [""])[0]
    facts: dict[str, Any] = {
        "host": parts.netloc,
        "path": _redact_path(parts.path),
        "view": redact(view) if view else None,
        "query_keys": sorted(query),
        "fragment_keys": sorted(fragment),
        "sid_present": bool(sid),
        "sig_present": bool(sig),
        "sig_shape": _shape(sig) if sig else None,
        "redirect_scheme": redact(urlsplit(redirect).scheme) if redirect else None,
    }
    return WebPage(url, sid, sig, facts)


@dataclass(frozen=True)
class ProxyWay:
    """One guess at how the web view authorizes its proxy POSTs.

    ``sid``: ``web_page`` (the WebPage's ``sid``) or ``authenticate`` (the
    X-SessionId of a sisu/authenticate); ``auth``: ``none``, ``xbl`` (SISU's
    ``XBL3.0 x=<uhs>;<AuthorizationToken>``) or ``sig`` (the WebPage's
    ``sig`` as the Authorization value, a guess); ``signed``: the ProofKey
    Signature header.
    """

    name: str
    sid: str = "web_page"
    auth: str = "xbl"
    signed: bool = True


PROXY_WAYS: list[ProxyWay] = [
    ProxyWay("page_xbl_signed"),
    ProxyWay("page_none_signed", auth="none"),
    ProxyWay("page_sig_signed", auth="sig"),
    ProxyWay("page_xbl_unsigned", signed=False),
    ProxyWay("page_none_unsigned", auth="none", signed=False),
    ProxyWay("page_sig_unsigned", auth="sig", signed=False),
    ProxyWay("authn_xbl_signed", sid="authenticate"),
    ProxyWay("authn_none_signed", sid="authenticate", auth="none"),
]


def _key_names(payload: Any) -> dict[str, Any] | list[str] | None:
    """The key names of an answer, one level deep (no values)."""
    if not isinstance(payload, dict):
        return None
    return {
        str(k): sorted(str(x) for x in v) if isinstance(v, dict) else type(v).__name__
        for k, v in payload.items()
    }


def _further_steps(*texts: Any) -> list[str]:
    found: set[str] = set()
    for text in texts:
        found.update(_FURTHER_STEP.findall(json.dumps(text) if not isinstance(text, str) else text))
    return sorted(found)[:40]


async def async_sisu_proxy(
    session: aiohttp.ClientSession,
    key: ProofKey,
    sid: str,
    read: str,
    authorization: str | None,
    signed: bool,
    step: str,
    facts: dict | None,
) -> bool:
    """One proxy POST of the web view (a read: ReadAccount or ReadProfile).
    True on a 2xx. Facts: status, XErr, key names, further step words."""
    url = f"{SISU_PROXY_URL}?{urlencode({'sessionid': sid})}"
    raw = json.dumps({read: {}}, separators=(",", ":")).encode()
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "x-xbl-contract-version": "3",
    }
    if authorization:
        headers["Authorization"] = authorization
    if signed:
        headers["Signature"] = key.sign("POST", url, authorization or "", raw)
    status, payload, resp_headers = await _call(session, "POST", url, data=raw, headers=headers)
    _record(facts, step, {
        "status": status,
        "xerr": _xerr(payload, resp_headers),
        "response_keys": _key_names(payload),
        "further_steps": _further_steps(_key_names(payload)) if payload is not None else [],
    })
    return status is not None and 200 <= status < 300


async def async_replay_web_view(
    session: aiohttp.ClientSession,
    key: ProofKey,
    tokens: XboxTokens,
    page: WebPage,
    authenticate_sid: str | None,
    facts: dict | None,
    name: str,
    preferred: str | None = None,
) -> str | None:
    """Replay the web view's proxy reads for ``page`` (see ProxyWay): each way
    in turn (``preferred`` first) until ReadAccount answers, then ReadProfile
    the same way. The way that answered, or None. Read-only: nothing on the
    account is created, reserved, set or accepted."""
    ways = list(PROXY_WAYS)
    if preferred:
        ways.sort(key=lambda w: w.name != preferred)
    for way in ways:
        sid = page.sid if way.sid == "web_page" else authenticate_sid
        if not sid or (way.auth == "sig" and not page.sig):
            continue
        auth = {"none": None, "xbl": tokens.header(), "sig": page.sig}[way.auth]
        ok = await async_sisu_proxy(
            session, key, sid, PROXY_READS[0], auth, way.signed,
            f"sisu_proxy[{name}|{way.name}|{PROXY_READS[0]}]", facts,
        )
        if not ok:
            continue
        await async_sisu_proxy(
            session, key, sid, PROXY_READS[1], auth, way.signed,
            f"sisu_proxy[{name}|{way.name}|{PROXY_READS[1]}]", facts,
        )
        return way.name
    return None


async def async_web_page_probe(session: aiohttp.ClientSession, page: WebPage) -> dict[str, Any]:
    """probe_xapprove: GET the WebPage (read-only) and up to three of its
    scripts on the same host; report the status, content type, the script
    paths (redacted) and the proxy-like command words found in them."""
    out: dict[str, Any] = {}
    try:
        status, _payload, headers, text = await _call_text(session, "GET", page.url)
    except XapproveError as err:
        return {"error": str(err)}
    content_type = next((v for k, v in headers.items() if str(k).lower() == "content-type"), None)
    scripts = _SCRIPT_SRC.findall(text or "")
    out.update({
        "status": status,
        "content_type": str(content_type)[:80] if content_type else None,
        "scripts": [_redact_path(urlsplit(s).path)[:120] for s in scripts[:10]],
    })
    texts = [text or ""]
    base = urlsplit(page.url)
    for src in scripts[:3]:
        target = urljoin(page.url, src)
        if urlsplit(target).netloc != base.netloc:
            continue
        try:
            _s, _p, _h, script = await _call_text(session, "GET", target)
        except XapproveError:
            continue
        texts.append(script or "")
    out["further_steps"] = _further_steps(*texts)
    return out


# ----------------------------------------------------------------------
# The stored sign-in
# ----------------------------------------------------------------------


def new_credential(refresh_token: str, key: ProofKey | None = None) -> dict[str, Any]:
    """What the Store keeps: the refresh token, the ProofKey, a device id."""
    return {
        "refresh_token": refresh_token,
        "proof_key": (key or ProofKey.generate()).to_pem(),
        "device_id": str(uuid.uuid4()),
        "signed_in": dt_util.utcnow().isoformat(),
    }


class XboxSisuAuth:
    """The Xbox Family Settings sign-in of one entry: XSTS on demand, signing.

    ``save`` is awaited with the credential whenever Microsoft rotates the
    refresh token of the main scope. Tokens other than the refresh token
    live in memory only. SISU tokens are kept per SisuProfile (the plain one
    serves the family read and the parent xuid).
    """

    def __init__(
        self,
        session_getter: Callable[[], aiohttp.ClientSession],
        credential: dict[str, Any],
        save: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        self._session_getter = session_getter
        self._credential = dict(credential)
        self._save = save
        self.key = ProofKey.from_pem(credential["proof_key"])
        self._device: tuple[str, datetime | None] | None = None
        #: SisuProfile name -> SISU's tokens ("" = the plain profile).
        self._sisu_tokens: dict[str, XboxTokens] = {}
        #: xsts_key -> the explicit XSTS step's token (memory only).
        self._xsts: dict[str, XboxTokens] = {}
        #: xsts_key -> the XstsVariant name that worked.
        self.xsts_variants: dict[str, str] = {}
        #: xsts_key -> the refusal when every variant failed (until those
        #: SISU tokens are renewed: no second sweep for nothing).
        self._xsts_failed: dict[str, XapproveError] = {}
        #: The variant tried first (probe_xapprove saves the one that worked).
        self.preferred_variant: str | None = None
        #: The title id from SISU's TitleToken (xti tid), when one came.
        self.title_id: str | None = None
        #: The session id of the diagnostic sisu/authenticate (memory only).
        self.authenticate_sid: str | None = None
        #: The ProxyWay that answered the web view's reads; "" when none did
        #: (the replay is then not swept again); None before any try.
        self.proxy_way: str | None = None
        #: probe_xapprove: also fetch the WebPage and its scripts.
        self.diagnose = False
        self._access: dict[str, MsaTokens] = {}
        self._lock = asyncio.Lock()
        self.token_facts: dict[str, Any] = {}

    @property
    def _tokens(self) -> XboxTokens | None:
        """The plain profile's SISU tokens."""
        return self._sisu_tokens.get("")

    @property
    def device_token(self) -> str | None:
        return self._device[0] if self._device else None

    def forget_tokens(self) -> None:
        self._sisu_tokens.clear()
        self._xsts.clear()
        self._xsts_failed.clear()
        self._access.clear()

    def seed(self, sign_in: SignIn) -> None:
        """Start from the tokens a fresh sign-in already got (no refresh needed)."""
        self._device = (sign_in.device_token, sign_in.device_not_after)
        self._sisu_tokens = {"": sign_in.tokens}
        self._xsts.clear()
        self._xsts_failed.clear()

    async def _async_msa(self, scope: str = SCOPE) -> MsaTokens:
        cached = self._access.get(scope)
        if cached is not None and _valid(cached.expires_at):
            return cached
        try:
            tokens = await async_refresh_msa(
                self._session_getter(), self._credential["refresh_token"], scope, self.token_facts
            )
        except XboxSignInExpired as err:
            if scope == SCOPE:
                raise
            # Another scope refused: not a sign-in that ran out.
            raise XapproveRefused(err.step, err.status, err.xerr, err.error) from err
        # Only the main scope's refresh token replaces the stored one.
        if (
            scope == SCOPE
            and tokens.refresh_token
            and tokens.refresh_token != self._credential["refresh_token"]
        ):
            self._credential["refresh_token"] = tokens.refresh_token
            await self._save(dict(self._credential))
        self._access[scope] = tokens
        return tokens

    async def async_xsts(self, *, force: bool = False, profile: str = "") -> XboxTokens:
        """SISU's own XSTS (its ``AuthorizationToken``, http://xboxlive.com)."""
        async with self._lock:
            return await self._async_sisu(force, profile)

    async def async_title_xsts(
        self, relying_party: str = RP_XBOXLIVE, *, force: bool = False, profile: str = ""
    ) -> XboxTokens:
        """An XSTS from the explicit XSTS step (SISU's user, device and title
        tokens) for ``relying_party``: how the Xbox Family Settings app gets
        the token it sends to xapprove. ``force`` renews SISU's tokens too."""
        key = xsts_key(relying_party, profile)
        async with self._lock:
            cached = self._xsts.get(key)
            if cached is not None and not force and _valid(cached.xsts_not_after):
                return cached
            sisu_tokens = await self._async_sisu(force, profile)
            if not force and key in self._xsts_failed:
                raise self._xsts_failed[key]
            session = self._session_getter()
            await self._async_device(session)
            first: XapproveError | None = None
            for variant in self._variant_order():
                try:
                    tokens = await async_xsts_authorize(
                        session, self.key, sisu_tokens, self._device[0], relying_party,
                        self.token_facts, variant, profile,
                    )
                except XapproveRefused as err:
                    first = first or err
                    continue
                except XstsVariantSkipped:
                    continue
                self._xsts[key] = tokens
                self.xsts_variants[key] = variant.name
                return tokens
            error = first or XapproveError(f"{xsts_step(relying_party, profile=profile)}: no variant answered")
            self._xsts_failed[key] = error
            raise error

    def _variant_order(self) -> list[XstsVariant]:
        """XSTS_VARIANTS, the preferred one (if any) first."""
        preferred = XSTS_VARIANTS_BY_NAME.get(self.preferred_variant or "")
        if preferred is None:
            return list(XSTS_VARIANTS)
        return [preferred] + [v for v in XSTS_VARIANTS if v is not preferred]

    async def _async_device(self, session: aiohttp.ClientSession) -> None:
        if self._device is None or not _valid(self._device[1]):
            self._device = await async_device_token(
                session, self.key, self._credential["device_id"], self.token_facts
            )

    def _drop_xsts(self, profile: str) -> None:
        """New SISU tokens of a profile: the XSTS made from the old ones go."""
        for key in [k for k in self._xsts if (k.split("|", 1)[0] if "|" in k else "") == profile]:
            del self._xsts[key]
        for key in [k for k in self._xsts_failed if (k.split("|", 1)[0] if "|" in k else "") == profile]:
            del self._xsts_failed[key]

    async def _async_authorize(self, session: aiohttp.ClientSession, profile: SisuProfile) -> XboxTokens:
        msa = await self._async_msa()
        ucs: tuple[str, str] | None = None
        if profile.ucs:
            ucs = (
                (await self._async_msa(UCS_CHECKIN_SCOPE)).access_token,
                (await self._async_msa(UCS_WEB_SCOPE)).access_token,
            )
        await self._async_device(session)
        sisu_session: SisuSession | None = None
        if profile.session:
            _verifier, challenge = new_pkce()
            sisu_session = await async_sisu_authenticate(
                session, self.key, self._device[0], self.title_id or DEFAULT_TITLE_ID,
                challenge=challenge, state=secrets.token_urlsafe(16),
                step=f"sisu_authenticate[{profile.name}]", facts=self.token_facts,
            )
        tokens = await async_sisu_authorize(
            session, self.key, msa.access_token, self._device[0], self.token_facts, profile, ucs,
            session_id=sisu_session.session_id if sisu_session else None,
        )
        if tokens.title_id:
            self.title_id = tokens.title_id
        if profile.webview:
            await self._async_web_view(
                session, profile, tokens,
                sisu_session.session_id if sisu_session else self.authenticate_sid,
            )
            if profile.reauth:
                tokens = await async_sisu_authorize(
                    session, self.key, msa.access_token, self._device[0], self.token_facts, profile,
                    ucs, step=f"{sisu_step(profile)}[after]",
                )
        return tokens

    async def _async_web_view(
        self,
        session: aiohttp.ClientSession,
        profile: SisuProfile,
        tokens: XboxTokens,
        authenticate_sid: str | None,
    ) -> None:
        """Replay the web view's proxy reads for SISU's WebPage, remembering
        the way that answered (``proxy_way``)."""
        prefix = f"web_view[{profile.name}]"
        page = tokens.web_page
        if page is None:
            _record(self.token_facts, prefix, {"skipped": "no WebPage in SISU's answer"})
            return
        if self.diagnose and "web_page_fetch" not in self.token_facts:
            _record(self.token_facts, "web_page_fetch", await async_web_page_probe(session, page))
        if self.proxy_way == "":
            _record(self.token_facts, prefix, {"skipped": "no proxy way answered before"})
            return
        way = await async_replay_web_view(
            session, self.key, tokens, page, authenticate_sid, self.token_facts, profile.name,
            self.proxy_way,
        )
        if way is not None or page.sid:
            # "" (no way answered) only after a real try on a page with a
            # session: a bare page says nothing about the ways.
            self.proxy_way = way or ""
        _record(self.token_facts, prefix, {"proxy_way": way, "web_page_sid": bool(page.sid)})

    async def _async_sisu(self, force: bool, profile_name: str = "") -> XboxTokens:
        """SISU's tokens of a profile, renewed when they ran out. The caller
        holds the lock."""
        profile = SISU_PROFILES.get(profile_name, PLAIN_PROFILE)
        cached = self._sisu_tokens.get(profile.name)
        if cached is not None and not force and _valid(cached.xsts_not_after):
            return cached
        self._drop_xsts(profile.name)
        session = self._session_getter()
        try:
            tokens = await self._async_authorize(session, profile)
        except XapproveRefused:
            if profile.name:
                raise
            # A device token or MSA token Microsoft no longer takes: once
            # more from fresh ones before giving up.
            self._device = None
            self._access.pop(SCOPE, None)
            tokens = await self._async_authorize(session, profile)
        self._sisu_tokens[profile.name] = tokens
        return tokens

    async def async_access_token(self, scope: str) -> str:
        """An MSA access token of the app's client for another scope (probe)."""
        async with self._lock:
            return (await self._async_msa(scope)).access_token

    async def async_authenticate_probe(self) -> dict[str, Any]:
        """probe_xapprove: one diagnostic sisu/authenticate (see there)."""
        async with self._lock:
            session = self._session_getter()
            await self._async_device(session)
            facts, self.authenticate_sid = await async_sisu_authenticate_probe(
                session, self.key, self._device[0], self.title_id
            )
            return facts

    def sign(self, method: str, url: str, authorization: str, body: bytes) -> str:
        return self.key.sign(method, url, authorization, body)


@dataclass
class SignIn:
    """A completed sign-in: what to store, and the tokens it already got."""

    credential: dict[str, Any]
    tokens: XboxTokens
    device_token: str
    device_not_after: datetime | None


async def async_sign_in(
    session: aiohttp.ClientSession, redirect: str, state: str, verifier: str
) -> SignIn:
    """Options flow, the pasted address: code -> MSA tokens -> device token
    -> SISU authorize.

    Raises ValueError (bad redirect) or XapproveError (Microsoft refused a
    step; XboxSignInExpired when the code was expired or already used).
    """
    code = code_from_redirect(redirect, state)
    msa = await async_exchange_code(session, code, verifier)
    return await async_finish_sign_in(session, msa)


async def async_finish_sign_in(session: aiohttp.ClientSession, msa: MsaTokens) -> SignIn:
    """MSA tokens of the Xbox Family Settings client -> device token -> SISU."""
    if not msa.refresh_token:
        raise XapproveError("sisu_msa_code: no refresh token in the answer")
    key = ProofKey.generate()
    credential = new_credential(msa.refresh_token, key)
    device, until = await async_device_token(session, key, credential["device_id"])
    tokens = await async_sisu_authorize(session, key, msa.access_token, device)
    return SignIn(credential, tokens, device, until)


async def async_complete_sign_in(
    session: aiohttp.ClientSession, redirect: str, state: str, verifier: str
) -> dict[str, Any]:
    """The credential to store (see async_sign_in)."""
    return (await async_sign_in(session, redirect, state, verifier)).credential


# ----------------------------------------------------------------------
# The device-code sign-in
# ----------------------------------------------------------------------
#
# login.live.com's device-code grant: the user opens microsoft.com/link on any
# device, enters a short code and signs in; meanwhile Home Assistant polls the
# token endpoint. No address to copy. Whether Microsoft offers it for this
# client id is not documented: a refusal (unsupported_response_type,
# invalid_client, ...) is DeviceCodeUnavailable, and the options flow then
# offers the pasted-address sign-in instead.


class DeviceCodeUnavailable(XapproveError):
    """Microsoft does not offer the device-code sign-in here; ``reason`` says why."""

    def __init__(self, reason: str, status: int | None = None) -> None:
        self.reason = reason
        self.status = status
        super().__init__(f"device code: {reason} (HTTP {status})")


class DeviceCodeExpired(XapproveError):
    """The code was not entered in time."""


class DeviceCodeDeclined(XapproveError):
    """The user declined, or Microsoft refused the sign-in at the code page."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"device code declined: {reason}")


@dataclass
class DeviceCode:
    """What the user enters where; the device_code itself is never shown."""

    device_code: str = field(repr=False)
    user_code: str
    verification_uri: str
    expires_at: datetime
    interval: float


#: Errors of the device-code request that mean "not offered for this client".
_DEVICE_CODE_UNSUPPORTED = (
    "unsupported_response_type", "invalid_client", "unauthorized_client",
    "invalid_scope", "invalid_request", "unsupported_grant_type",
)


async def async_start_device_code(session: aiohttp.ClientSession) -> DeviceCode:
    """Ask login.live.com for a user code. DeviceCodeUnavailable if refused."""
    status, payload, _headers = await _call(session, "POST", DEVICE_CODE_URL, data={
        "client_id": CLIENT_ID,
        "scope": SCOPE,
        "response_type": "device_code",
    })
    if (
        status == 200
        and isinstance(payload, dict)
        and isinstance(payload.get("device_code"), str)
        and isinstance(payload.get("user_code"), str)
    ):
        expires_in = payload.get("expires_in")
        interval = payload.get("interval")
        return DeviceCode(
            device_code=payload["device_code"],
            user_code=payload["user_code"],
            verification_uri=str(
                payload.get("verification_uri") or "https://www.microsoft.com/link"
            ),
            expires_at=dt_util.utcnow() + timedelta(
                seconds=int(expires_in) if isinstance(expires_in, (int, float)) else 900
            ),
            interval=float(interval) if isinstance(interval, (int, float)) else 5.0,
        )
    error = payload.get("error") if isinstance(payload, dict) and isinstance(payload.get("error"), str) else None
    if status is not None and 400 <= status < 500:
        raise DeviceCodeUnavailable(error or "refused", status)
    if status == 200:
        raise DeviceCodeUnavailable("no code in the answer", status)
    raise XapproveError(f"device code: HTTP {status}")


async def async_poll_device_code(session: aiohttp.ClientSession, code: DeviceCode) -> MsaTokens | None:
    """One poll: the MSA tokens once the user has signed in, None while pending.

    ``slow_down`` adds 5 seconds to ``code.interval``. Raises DeviceCodeExpired,
    DeviceCodeDeclined, or XapproveError.
    """
    status, payload, _headers = await _call(session, "POST", TOKEN_URL, data={
        "client_id": CLIENT_ID,
        "grant_type": GRANT_DEVICE_CODE,
        "device_code": code.device_code,
    })
    if status == 200 and isinstance(payload, dict) and isinstance(payload.get("access_token"), str):
        expires = payload.get("expires_in")
        return MsaTokens(
            payload["access_token"],
            payload.get("refresh_token") if isinstance(payload.get("refresh_token"), str) else None,
            dt_util.utcnow() + timedelta(seconds=int(expires)) if isinstance(expires, int) else None,
        )
    error = payload.get("error") if isinstance(payload, dict) and isinstance(payload.get("error"), str) else None
    if error == "authorization_pending":
        return None
    if error == "slow_down":
        code.interval += 5
        return None
    if error in ("expired_token", "code_expired"):
        raise DeviceCodeExpired("the code expired")
    if error in ("access_denied", "authorization_declined", "bad_verification_code", "invalid_grant"):
        raise DeviceCodeDeclined(error)
    raise XapproveError(f"device code poll: HTTP {status}, {error}")


async def async_wait_for_device_code(
    session: aiohttp.ClientSession,
    code: DeviceCode,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> SignIn:
    """Poll until the user has signed in, then finish the Xbox sign-in."""
    while True:
        msa = await async_poll_device_code(session, code)
        if msa is not None:
            return await async_finish_sign_in(session, msa)
        if dt_util.utcnow() >= code.expires_at:
            raise DeviceCodeExpired("the code expired")
        await sleep(code.interval)


# ----------------------------------------------------------------------
# The app-style sign-in (options flow)
# ----------------------------------------------------------------------
#
# As the app does it: sisu/authenticate (a session and the MSA sign-in link
# for our PKCE) -> the user signs in -> code exchange -> the consent tokens
# -> sisu/authorize with the SessionId -> the WebPage's proxy reads. The
# RedirectUri is oauth20_desktop.srf, the only one seen to work with this
# client: the page erases the code from its address after loading, so the
# address is copied from the browser's network log (as in the pasted-address
# sign-in).

#: sisu/authorize ways tried after the app-style sign-in, in order: the app's
#: (consent tokens raw, UserPrompts, no contract), consent tokens as t=,
#: without consent tokens.
APP_SIGNIN_PROFILES = [
    SisuProfile("app", ucs=True, ucs_form="raw", prompts=True, contract=None, session=True),
    SisuProfile("app_t", ucs=True, ucs_form="t", prompts=True, contract=None, session=True),
    SisuProfile("app_no_ucs", prompts=True, contract=None, session=True),
]


@dataclass
class AppSignInStart:
    """An app-style sign-in waiting for the user. Memory only, never logged."""

    url: str = field(repr=False)
    verifier: str = field(repr=False)
    state: str = field(repr=False)
    redirect_uri: str
    scope: str
    session_id: str = field(repr=False)
    credential: dict[str, Any] = field(repr=False)
    device_token: str = field(repr=False)
    device_not_after: datetime | None


@dataclass
class AppSignInResult:
    """A finished app-style sign-in: the SignIn to save and check, the
    WebPage (when it has a session; the user can finish it in a browser),
    the proxy way that answered, and non-secret facts."""

    sign_in: SignIn
    web_page_url: str | None = field(repr=False)
    proxy_way: str | None
    facts: dict[str, Any]


async def async_start_app_sign_in(
    session: aiohttp.ClientSession, credential: dict[str, Any] | None, title_id: str | None
) -> AppSignInStart:
    """sisu/authenticate with a fresh PKCE: the link for the user. The stored
    sign-in's ProofKey and device id are kept when there is one."""
    if isinstance(credential, dict) and credential.get("proof_key") and credential.get("device_id"):
        base = dict(credential)
    else:
        base = new_credential("", ProofKey.generate())
    key = ProofKey.from_pem(base["proof_key"])
    device, until = await async_device_token(session, key, base["device_id"])
    verifier, challenge = new_pkce()
    state = secrets.token_urlsafe(16)
    started = await async_sisu_authenticate(
        session, key, device, title_id or DEFAULT_TITLE_ID, challenge=challenge, state=state
    )
    if not started.msa_oauth_redirect or not started.session_id:
        status = started.facts.get("status")
        raise XapproveRefused("sisu_authenticate", status, started.facts.get("xerr"), "no sign-in link")
    query = parse_qs(urlsplit(started.msa_oauth_redirect).query)
    return AppSignInStart(
        url=started.msa_oauth_redirect,
        verifier=verifier,
        state=(query.get("state") or [state])[0],
        redirect_uri=(query.get("redirect_uri") or [REDIRECT_URI])[0],
        scope=(query.get("scope") or [SCOPE])[0],
        session_id=started.session_id,
        credential=base,
        device_token=device,
        device_not_after=until,
    )


def app_code_from_redirect(url: str, start: AppSignInStart) -> str:
    """The code in the pasted address of the app-style sign-in."""
    target = urlsplit(start.redirect_uri)
    text = re.sub(r"\s+", "", url or "").strip("\"'<>`")
    if target.netloc.lower() != "login.live.com" and text.lower().startswith(
        f"{target.scheme}://{target.netloc}".lower()
    ):
        parts = urlsplit(text)
        query = parse_qs(parts.query)
        query.update({k: v for k, v in parse_qs(parts.fragment).items() if k not in query})
        if query.get("error"):
            raise ValueError(f"signin_error:{query['error'][0]}")
        if not query.get("code"):
            raise ValueError("invalid_redirect")
        pasted_state = query.get("state", [None])[0]
        if pasted_state is not None and pasted_state != start.state:
            raise ValueError("state_mismatch")
        return query["code"][0]
    return code_from_redirect(url, start.state)


async def async_finish_app_sign_in(
    session: aiohttp.ClientSession, start: AppSignInStart, pasted: str
) -> AppSignInResult:
    """The pasted address -> code exchange -> consent tokens -> sisu/authorize
    with the SessionId -> the WebPage's proxy reads (read-only). Raises
    ValueError (the address) or XapproveError (a step refused)."""
    facts: dict[str, Any] = {}
    code = app_code_from_redirect(pasted, start)
    msa = await _msa_token(session, {
        "client_id": CLIENT_ID,
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": start.verifier,
        "redirect_uri": start.redirect_uri,
        "scope": start.scope,
    }, "sisu_msa_code", facts)
    if not msa.refresh_token:
        raise XapproveError("sisu_msa_code: no refresh token in the answer")
    ucs: tuple[str, str] | None = None
    try:
        ucs = (
            (await async_refresh_msa(session, msa.refresh_token, UCS_CHECKIN_SCOPE, facts)).access_token,
            (await async_refresh_msa(session, msa.refresh_token, UCS_WEB_SCOPE, facts)).access_token,
        )
    except XapproveError:
        ucs = None
    key = ProofKey.from_pem(start.credential["proof_key"])
    tokens: XboxTokens | None = None
    refused: XapproveError | None = None
    for profile in APP_SIGNIN_PROFILES:
        if profile.ucs and ucs is None:
            continue
        try:
            tokens = await async_sisu_authorize(
                session, key, msa.access_token, start.device_token, facts, profile, ucs,
                session_id=start.session_id,
            )
        except XapproveRefused as err:
            refused = refused or err
            continue
        break
    if tokens is None:
        raise refused or XapproveError("sisu_authorize: no way answered")
    page = tokens.web_page
    way: str | None = None
    if page is not None and page.sid:
        way = await async_replay_web_view(session, key, tokens, page, start.session_id, facts, "app")
    credential = {
        **start.credential,
        "refresh_token": msa.refresh_token,
        "signed_in": dt_util.utcnow().isoformat(),
    }
    return AppSignInResult(
        SignIn(credential, tokens, start.device_token, start.device_not_after),
        page.url if page is not None and page.sid else None,
        way,
        facts,
    )
