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
from urllib.parse import parse_qs, urlencode, urlsplit
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
    _valid,
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
) -> tuple[int, Any, dict[str, str]]:
    raw = json.dumps(body, separators=(",", ":")).encode()
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "x-xbl-contract-version": "1",
        **(extra_headers or {}),
    }
    if signed:
        headers["Signature"] = key.sign("POST", url, "", raw)
    status, payload, resp_headers = await _call(session, "POST", url, data=raw, headers=headers)
    _record(facts, step, {"status": status, "xerr": _xerr(payload, resp_headers)})
    return status, payload, resp_headers


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
    if status == 200 and isinstance(payload, dict) and isinstance(payload.get("Token"), str):
        return payload["Token"], _not_after(payload)
    if status and 400 <= status < 500:
        raise XapproveRefused("device_authenticate", status, _xerr(payload, headers))
    raise XapproveError(f"device_authenticate: HTTP {status}")


async def async_sisu_authorize(
    session: aiohttp.ClientSession,
    key: ProofKey,
    msa_access_token: str,
    device_token: str,
    facts: dict | None = None,
) -> XboxTokens:
    status, payload, headers = await _signed_post(session, key, SISU_AUTHORIZE_URL, {
        "AccessToken": f"t={msa_access_token}",
        "AppId": CLIENT_ID,
        "DeviceToken": device_token,
        "Sandbox": "RETAIL",
        "SiteName": "user.auth.xboxlive.com",
        "RelyingParty": RELYING_PARTY,
        "UseModernGamertag": True,
        "ProofKey": key.jwk(),
    }, "sisu_authorize", facts)
    auth = payload.get("AuthorizationToken") if isinstance(payload, dict) else None
    xui = _xui(auth)
    if facts is not None and isinstance(facts.get("sisu_authorize"), dict):
        facts["sisu_authorize"].update({
            "authorization_token_received": bool(isinstance(auth, dict) and auth.get("Token")),
            "user_token_received": bool(isinstance(payload, dict) and payload.get("UserToken")),
            "title_token_received": bool(isinstance(payload, dict) and payload.get("TitleToken")),
            "device_token_received": bool(isinstance(payload, dict) and payload.get("DeviceToken")),
            "display_claims_xui_keys": sorted(str(k) for k in xui),
        })
    if status == 200 and isinstance(auth, dict) and isinstance(auth.get("Token"), str) and xui.get("uhs"):
        user = payload.get("UserToken") if isinstance(payload.get("UserToken"), dict) else {}
        title = payload.get("TitleToken") if isinstance(payload.get("TitleToken"), dict) else {}
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
        )
    if status and 400 <= status < 500:
        raise XapproveRefused("sisu_authorize", status, _xerr(payload, headers))
    raise XapproveError(f"sisu_authorize: HTTP {status}")


def _xui(token: Any) -> dict[str, Any]:
    """The first ``xui`` claim set of an XSTS answer ({} if none)."""
    if isinstance(token, dict):
        items = (token.get("DisplayClaims") or {}).get("xui")
        if isinstance(items, list) and items and isinstance(items[0], dict):
            return items[0]
    return {}


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
#: 2026-11-08 ``app`` got 400 XErr 2148916279 (0x8015DC37) for
#: http://xboxlive.com.
XSTS_VARIANTS: list[XstsVariant] = [
    XstsVariant("app"),
    XstsVariant("no_claims", claims=False),
    XstsVariant("no_title", title=False),
    XstsVariant("sisu_device", sisu_device=True),
    XstsVariant("unsigned", signed=False),
    XstsVariant("plain_json", charset=False),
    XstsVariant("bare", claims=False, title=False, charset=False),
]
XSTS_VARIANTS_BY_NAME = {v.name: v for v in XSTS_VARIANTS}


def xsts_step(relying_party: str, variant: str = "app") -> str:
    """The name of the explicit XSTS step for one relying party (facts)."""
    if variant == "app":
        return f"xsts_authorize[{relying_party}]"
    return f"xsts_authorize[{relying_party}|{variant}]"


async def async_xsts_authorize(
    session: aiohttp.ClientSession,
    key: ProofKey,
    sisu_tokens: XboxTokens,
    device_token: str,
    relying_party: str = RP_XBOXLIVE,
    facts: dict | None = None,
    variant: XstsVariant = XSTS_VARIANTS[0],
) -> XboxTokens:
    """The app's explicit XSTS step: ``xsts.auth.xboxlive.com/xsts/authorize``
    with SISU's user token, the device token and SISU's title token (when one
    came), signed with the ProofKey, for ``relying_party``; ``variant``
    leaves parts out or changes them.

    The parent xuid is kept from SISU when the answer carries no ``xid``.
    """
    step = xsts_step(relying_party, variant.name)
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
    refresh token. Tokens other than the refresh token live in memory only.
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
        self._tokens: XboxTokens | None = None
        #: relying party -> the explicit XSTS step's token (memory only).
        self._xsts: dict[str, XboxTokens] = {}
        #: relying party -> the XstsVariant name that worked.
        self.xsts_variants: dict[str, str] = {}
        #: relying party -> the refusal when every variant failed (until the
        #: SISU tokens are renewed: no second sweep for nothing).
        self._xsts_failed: dict[str, XapproveError] = {}
        #: The variant tried first (probe_xapprove saves the one that worked).
        self.preferred_variant: str | None = None
        self._access: dict[str, MsaTokens] = {}
        self._lock = asyncio.Lock()
        self.token_facts: dict[str, Any] = {}

    def forget_tokens(self) -> None:
        self._tokens = None
        self._xsts.clear()
        self._xsts_failed.clear()
        self._access.clear()

    def seed(self, sign_in: SignIn) -> None:
        """Start from the tokens a fresh sign-in already got (no refresh needed)."""
        self._device = (sign_in.device_token, sign_in.device_not_after)
        self._tokens = sign_in.tokens
        self._xsts.clear()
        self._xsts_failed.clear()

    async def _async_msa(self, scope: str = SCOPE) -> MsaTokens:
        cached = self._access.get(scope)
        if cached is not None and _valid(cached.expires_at):
            return cached
        tokens = await async_refresh_msa(
            self._session_getter(), self._credential["refresh_token"], scope, self.token_facts
        )
        if tokens.refresh_token and tokens.refresh_token != self._credential["refresh_token"]:
            self._credential["refresh_token"] = tokens.refresh_token
            await self._save(dict(self._credential))
        self._access[scope] = tokens
        return tokens

    async def async_xsts(self, *, force: bool = False) -> XboxTokens:
        """SISU's own XSTS (its ``AuthorizationToken``, http://xboxlive.com)."""
        async with self._lock:
            return await self._async_sisu(force)

    async def async_title_xsts(
        self, relying_party: str = RP_XBOXLIVE, *, force: bool = False
    ) -> XboxTokens:
        """An XSTS from the explicit XSTS step (SISU's user, device and title
        tokens) for ``relying_party``: how the Xbox Family Settings app gets
        the token it sends to xapprove. ``force`` renews SISU's tokens too."""
        async with self._lock:
            cached = self._xsts.get(relying_party)
            if cached is not None and not force and _valid(cached.xsts_not_after):
                return cached
            sisu_tokens = await self._async_sisu(force)
            if not force and relying_party in self._xsts_failed:
                raise self._xsts_failed[relying_party]
            session = self._session_getter()
            if self._device is None or not _valid(self._device[1]):
                self._device = await async_device_token(
                    session, self.key, self._credential["device_id"], self.token_facts
                )
            first: XapproveError | None = None
            for variant in self._variant_order():
                try:
                    tokens = await async_xsts_authorize(
                        session, self.key, sisu_tokens, self._device[0], relying_party,
                        self.token_facts, variant,
                    )
                except XapproveRefused as err:
                    first = first or err
                    continue
                except XstsVariantSkipped:
                    continue
                self._xsts[relying_party] = tokens
                self.xsts_variants[relying_party] = variant.name
                return tokens
            error = first or XapproveError(f"{xsts_step(relying_party)}: no variant answered")
            self._xsts_failed[relying_party] = error
            raise error

    def _variant_order(self) -> list[XstsVariant]:
        """XSTS_VARIANTS, the preferred one (if any) first."""
        preferred = XSTS_VARIANTS_BY_NAME.get(self.preferred_variant or "")
        if preferred is None:
            return list(XSTS_VARIANTS)
        return [preferred] + [v for v in XSTS_VARIANTS if v is not preferred]

    async def _async_sisu(self, force: bool) -> XboxTokens:
        """SISU's tokens, renewed when they ran out. The caller holds the lock."""
        if self._tokens is not None and not force and _valid(self._tokens.xsts_not_after):
            return self._tokens
        # New SISU tokens: the XSTS made from the old ones go too.
        self._xsts.clear()
        self._xsts_failed.clear()
        session = self._session_getter()
        msa = await self._async_msa()
        if self._device is None or not _valid(self._device[1]):
            self._device = await async_device_token(
                session, self.key, self._credential["device_id"], self.token_facts
            )
        try:
            self._tokens = await async_sisu_authorize(
                session, self.key, msa.access_token, self._device[0], self.token_facts
            )
        except XapproveRefused:
            # A device token or MSA token Microsoft no longer takes: once
            # more from fresh ones before giving up.
            self._device = None
            self._access.pop(SCOPE, None)
            msa = await self._async_msa()
            self._device = await async_device_token(
                session, self.key, self._credential["device_id"], self.token_facts
            )
            self._tokens = await async_sisu_authorize(
                session, self.key, msa.access_token, self._device[0], self.token_facts
            )
        return self._tokens

    async def async_access_token(self, scope: str) -> str:
        """An MSA access token of the app's client for another scope (probe)."""
        async with self._lock:
            return (await self._async_msa(scope)).access_token

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
