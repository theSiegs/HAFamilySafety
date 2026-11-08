"""Fakes for the Xbox tests: login.live.com, Xbox Live SISU, the Xbox family
and xapprove holding one override, plus a coordinator signed in to Xbox
Family Settings. Every signed request is checked against the ProofKey."""

from __future__ import annotations

import asyncio
import base64
import json
import struct
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.util import dt as dt_util

from custom_components.microsoft_family_safety import xapprove
from custom_components.microsoft_family_safety import xbox_sisu as sisu
from custom_components.microsoft_family_safety.const import (
    CONF_XBOX_FEATURES,
    CONF_XBOX_SIGNED_IN_AT,
    CONF_XBOX_XAPPROVE,
    DOMAIN,
)
from custom_components.microsoft_family_safety.screen_time import iso_utc

from .test_platform_lock import CHILD, _make
from .test_xbox_mobile_lock import FakeWeb

PATCH_SESSION = "custom_components.microsoft_family_safety.xbox_xapprove.async_get_clientsession"
PATCH_FLOW_SESSION = "custom_components.microsoft_family_safety.config_flow.async_get_clientsession"

PARENT_XUID = "2535400000000001"
CHILD_XUID = "2535400000000002"
GAMERTAG = "AlexPlaysGames"
CODE = "auth-code-SECRET"
SISU_ACCESS = "sisu-access-SECRET"
DEVICE_TOKEN = "device-token-SECRET"
SISU_XSTS = "sisu-xsts-SECRET"
SISU_AUTH = f"XBL3.0 x=4343;{SISU_XSTS}"
SECRETS = (
    PARENT_XUID, CHILD_XUID, GAMERTAG, CODE, SISU_ACCESS, DEVICE_TOKEN, SISU_XSTS,
    "sisu-refresh-", "title-SECRET", "sisu-user-SECRET",
)


class FakeResponse:
    def __init__(self, status: int, payload: Any = None, headers: dict | None = None) -> None:
        self.status = status
        self._payload = payload
        self.headers = headers or {}

    async def json(self, content_type=None):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def verify_signature(header: str, jwk: dict, method: str, url: str, auth: str, body: bytes) -> int:
    """Check a Signature header against the JWK's public key; its timestamp back."""
    raw = base64.b64decode(header)
    assert len(raw) == 4 + 8 + 64
    assert struct.unpack(">I", raw[:4])[0] == 1
    timestamp = struct.unpack(">Q", raw[4:12])[0]
    r = int.from_bytes(raw[12:44], "big")
    s = int.from_bytes(raw[44:76], "big")

    def num(value: str) -> int:
        return int.from_bytes(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)), "big")

    assert jwk["kty"] == "EC" and jwk["crv"] == "P-256" and jwk["alg"] == "ES256"
    public = ec.EllipticCurvePublicNumbers(num(jwk["x"]), num(jwk["y"]), ec.SECP256R1()).public_key()
    public.verify(
        encode_dss_signature(r, s),
        sisu.signed_payload(method, sisu.path_and_query(url), auth, body, timestamp),
        ec.ECDSA(hashes.SHA256()),
    )
    return timestamp


class XboxServer:
    """The Xbox Family Settings client's sign-in, the Xbox family, xapprove."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        #: (overrideType, validUntil) in force, or None.
        self.override: tuple[int, str] | None = None
        self.creates: list[dict] = []
        self.xapprove_status = 200
        self.family_status = 200
        self.create_ignored = False
        #: Which request headers xapprove accepts: by default the signed SISU XSTS.
        self.accepts = lambda h: h.get("Authorization") == SISU_AUTH and "Signature" in h
        self.refresh_tokens = {"sisu-refresh-1"}
        self.next_refresh = 2
        self.dead = False
        self.code_dead = False
        self.jwk: dict | None = None
        self.sisu_calls: list[str] = []
        self.sisu_authorize_status = 200

    def state(self) -> dict:
        overrides = []
        if self.override is not None:
            code, until = self.override
            overrides.append({
                "overrideType": code, "target": 1, "appliesTo": [1], "validUntil": until,
            })
        return {
            "actions": {"create": {"allow": [1], "block": [1]}, "cancel": {"target": [1]}},
            "overrides": overrides,
        }

    def _rotate(self) -> str:
        token = f"sisu-refresh-{self.next_refresh}"
        self.next_refresh += 1
        self.refresh_tokens = {token}
        return token

    def request(self, method, url, **kwargs):
        self.calls.append((method, url))
        headers = kwargs.get("headers") or {}
        body = kwargs.get("data") if isinstance(kwargs.get("data"), bytes) else b""
        if url == sisu.TOKEN_URL:
            form = kwargs["data"]
            assert form["client_id"] == sisu.CLIENT_ID
            self.sisu_calls.append(f"token:{form['grant_type']}:{form['scope']}")
            if form["grant_type"] == "authorization_code":
                assert form["redirect_uri"] == sisu.REDIRECT_URI and form["code_verifier"]
                if form["code"] != CODE or self.code_dead:
                    return FakeResponse(400, {"error": "invalid_grant"})
            elif self.dead or form["refresh_token"] not in self.refresh_tokens:
                return FakeResponse(400, {"error": "invalid_grant"})
            return FakeResponse(200, {
                "token_type": "bearer", "expires_in": 3600, "scope": form["scope"],
                "access_token": SISU_ACCESS, "refresh_token": self._rotate(),
            })
        if url == sisu.DEVICE_URL:
            self.sisu_calls.append("device")
            payload = json.loads(kwargs["data"])
            self.jwk = payload["Properties"]["ProofKey"]
            verify_signature(headers["Signature"], self.jwk, "POST", url, "", kwargs["data"])
            assert payload["Properties"]["AuthMethod"] == "ProofOfPossession"
            assert payload["Properties"]["Id"].startswith("{")
            return FakeResponse(200, {
                "Token": DEVICE_TOKEN, "NotAfter": iso_utc(dt_util.utcnow() + timedelta(days=14)),
            })
        if url == sisu.SISU_AUTHORIZE_URL:
            self.sisu_calls.append("authorize")
            payload = json.loads(kwargs["data"])
            assert payload["ProofKey"] == self.jwk
            verify_signature(headers["Signature"], self.jwk, "POST", url, "", kwargs["data"])
            assert payload["AccessToken"] == f"t={SISU_ACCESS}"
            assert payload["DeviceToken"] == DEVICE_TOKEN
            assert payload["AppId"] == sisu.CLIENT_ID and payload["RelyingParty"] == "http://xboxlive.com"
            if self.sisu_authorize_status != 200:
                return FakeResponse(self.sisu_authorize_status, None, {"x-err": "2148916236"})
            return FakeResponse(200, {
                "DeviceToken": DEVICE_TOKEN,
                "TitleToken": {"Token": "title-SECRET"},
                "UserToken": {"Token": "sisu-user-SECRET",
                              "NotAfter": iso_utc(dt_util.utcnow() + timedelta(days=14))},
                "AuthorizationToken": {
                    "Token": SISU_XSTS,
                    "NotAfter": iso_utc(dt_util.utcnow() + timedelta(hours=16)),
                    "DisplayClaims": {"xui": [{"uhs": "4343", "xid": PARENT_XUID, "gtg": "SamParent"}]},
                },
            })
        if "Signature" in headers and self.jwk is not None:
            verify_signature(headers["Signature"], self.jwk, method, url, headers["Authorization"], body)
        if url.startswith("https://accounts.xboxlive.com/family/"):
            assert headers["Authorization"] == SISU_AUTH
            if self.family_status != 200:
                return FakeResponse(self.family_status, None)
            # As seen live: no isAdult, a role per member.
            return FakeResponse(200, {"familyUsers": [
                {"xuid": PARENT_XUID, "role": "Admin", "firstName": "Sam", "lastName": "Rivera",
                 "gamertag": "SamParent"},
                {"xuid": CHILD_XUID, "role": "Child", "firstName": "Alex", "lastName": "Rivera",
                 "gamertag": GAMERTAG},
            ]})
        if url.startswith(xapprove.XAPPROVE_HOST):
            assert f"/user/{CHILD_XUID}/" in url
            if self.xapprove_status != 200:
                return FakeResponse(self.xapprove_status, None, {"x-err": "2148916238"})
            if not self.accepts(headers):
                return FakeResponse(
                    401, None, {"WWW-Authenticate": "Token realm='xboxlive.com', error='token_required'"}
                )
            if url.endswith("/settings/screentime/overrides/create"):
                assert method == "POST"
                payload = json.loads(kwargs["data"])
                assert payload["target"] == 1
                self.creates.append(dict(payload))
                if not self.create_ignored:
                    if payload["overrideType"] == 100:
                        self.override = None
                    else:
                        self.override = (payload["overrideType"], payload["validUntil"])
                return FakeResponse(200, self.state())
            if url.endswith("/settings/screentime/overrides"):
                assert method == "GET"
                return FakeResponse(200, self.state())
        raise AssertionError(f"unexpected {method} {url}")

    def create_types(self) -> list[int]:
        return [c["overrideType"] for c in self.creates]

    def urls(self, prefix: str) -> list[str]:
        return [u for _, u in self.calls if u.startswith(prefix)]


class RecordingWeb(FakeWeb):
    """FakeWeb that records every Family web-session call (there must be none)."""

    def __init__(self) -> None:
        super().__init__()
        self.web_calls: list[str] = []

    async def get_xbox_screentime_policy(self, child_id):
        self.web_calls.append("get_xbox_screentime_policy")
        return await super().get_xbox_screentime_policy(child_id)

    async def get_xbox_screentime_usage(self, *args, **kwargs):
        self.web_calls.append("get_xbox_screentime_usage")
        return None

    async def set_platform_time_override(self, child_id, platform, kind, date_time):
        self.web_calls.append(f"set_platform_time_override:{platform}:{kind}")
        return await super().set_platform_time_override(child_id, platform, kind, date_time)

    async def _warm_family_context(self):
        self.web_calls.append("_warm_family_context")

    async def async_check_web_session(self):
        self.web_calls.append("async_check_web_session")
        return True


#: Coordinators made here, so their schedule-restore timers can be cancelled.
MADE: list = []


def stop_timers() -> None:
    while MADE:
        MADE.pop()._cancel_all_mobile_restores()


def hours(value: float):
    return dt_util.utcnow() + timedelta(hours=value)


async def make_rig(
    hass,
    server: XboxServer,
    options: dict | None = None,
    *,
    signed_in: bool = True,
    web_session: bool = True,
):
    """A coordinator with the Xbox Family Settings sign-in and xapprove on.

    ``web`` (coord.web_api) is test_xbox_mobile_lock's FakeWeb: the mobile Xbox
    schedule (the fallback) and a record of any Family web call.
    """
    opts: dict[str, Any] = {CONF_XBOX_XAPPROVE: True, CONF_XBOX_FEATURES: web_session}
    if signed_in:
        opts[CONF_XBOX_SIGNED_IN_AT] = dt_util.utcnow().isoformat()
    opts.update(options or {})
    entry = MockConfigEntry(domain=DOMAIN, data={"refresh_token": "x"}, options=opts)
    entry.add_to_hass(hass)
    coord = _make(hass, entry)
    coord.data["accounts"][CHILD].update({"first_name": "Alex", "surname": "Rivera"})
    web = RecordingWeb()
    MADE.append(coord)
    web.family_context_state = "ready"  # even a working web session is never used
    coord.web_api = web
    coord.data["accounts"][CHILD]["xbox_screentime_policy"] = web.policy()
    coord.api = SimpleNamespace(api=SimpleNamespace(authenticator=SimpleNamespace(
        refresh_token="family-refresh", _login_lock=asyncio.Lock(), perform_refresh=AsyncMock(),
    )))
    if signed_in:
        key = sisu.ProofKey.generate()
        server.jwk = key.jwk()
        await coord._xbox_signin_store.async_save(sisu.new_credential("sisu-refresh-1", key))
    return coord, entry


def no_secrets(value) -> None:
    text = json.dumps(value, default=str)
    for secret in SECRETS:
        assert secret not in text
