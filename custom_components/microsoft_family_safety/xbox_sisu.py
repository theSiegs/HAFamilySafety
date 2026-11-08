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
5. When that XSTS runs out, the MSA token is refreshed (the new refresh token
   replaces the old one) and step 4 runs again.

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
from dataclasses import dataclass
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
RELYING_PARTY = "http://xboxlive.com"

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
    session: aiohttp.ClientSession, key: ProofKey, url: str, body: dict, step: str, facts: dict | None
) -> tuple[int, Any, dict[str, str]]:
    raw = json.dumps(body, separators=(",", ":")).encode()
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "x-xbl-contract-version": "1",
        "Signature": key.sign("POST", url, "", raw),
    }
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
    xui: dict[str, Any] = {}
    if isinstance(auth, dict):
        items = (auth.get("DisplayClaims") or {}).get("xui")
        if isinstance(items, list) and items and isinstance(items[0], dict):
            xui = items[0]
    if facts is not None and isinstance(facts.get("sisu_authorize"), dict):
        facts["sisu_authorize"].update({
            "authorization_token_received": bool(isinstance(auth, dict) and auth.get("Token")),
            "user_token_received": bool(isinstance(payload, dict) and payload.get("UserToken")),
            "title_token_received": bool(isinstance(payload, dict) and payload.get("TitleToken")),
            "display_claims_xui_keys": sorted(str(k) for k in xui),
        })
    if status == 200 and isinstance(auth, dict) and isinstance(auth.get("Token"), str) and xui.get("uhs"):
        user = payload.get("UserToken") if isinstance(payload.get("UserToken"), dict) else {}
        xid = xui.get("xid")
        return XboxTokens(
            str(xui["uhs"]), auth["Token"], _not_after(auth),
            str(user.get("Token") or ""), _not_after(user) if user else None,
            str(xid) if xid else None,
        )
    if status and 400 <= status < 500:
        raise XapproveRefused("sisu_authorize", status, _xerr(payload, headers))
    raise XapproveError(f"sisu_authorize: HTTP {status}")


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
        self._access: dict[str, MsaTokens] = {}
        self._lock = asyncio.Lock()
        self.token_facts: dict[str, Any] = {}

    def forget_tokens(self) -> None:
        self._tokens = None
        self._access.clear()

    def seed(self, sign_in: SignIn) -> None:
        """Start from the tokens a fresh sign-in already got (no refresh needed)."""
        self._device = (sign_in.device_token, sign_in.device_not_after)
        self._tokens = sign_in.tokens

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
        async with self._lock:
            if self._tokens is not None and not force and _valid(self._tokens.xsts_not_after):
                return self._tokens
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
    """Options flow: code -> MSA tokens -> device token -> SISU authorize.

    Raises ValueError (bad redirect) or XapproveError (Microsoft refused a
    step; XboxSignInExpired when the code was expired or already used).
    """
    code = code_from_redirect(redirect, state)
    msa = await async_exchange_code(session, code, verifier)
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
