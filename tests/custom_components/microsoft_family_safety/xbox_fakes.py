"""Fakes for the Xbox tests: login.live.com, Xbox Live SISU, the Xbox family
and xapprove holding one override, plus a coordinator signed in to Xbox
Family Settings. Every signed request is checked against the ProofKey."""

from __future__ import annotations

import asyncio
import base64
import json
import hashlib
import struct
from urllib.parse import parse_qs, urlencode, urlsplit
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
TITLE_TOKEN = "title-SECRET"
#: The DeviceToken in SISU's answer (another than device/authenticate's).
SISU_DEVICE_TOKEN = "sisu-device-token-SECRET"
#: The consent service access tokens (no refresh token comes with them).
UCS_TOKENS = {
    sisu.UCS_CHECKIN_SCOPE: "ucs-checkin-SECRET",
    sisu.UCS_WEB_SCOPE: "ucs-web-SECRET",
}
#: sisu/authenticate's answer.
SESSION_ID = "session-id-SECRET"
TITLE_ID = "1016898439"
#: The web view: its page, the sig of its URL, its session ids.
WEB_PAGE_BASE = "https://sisu.xboxlive.com/client/v68/00000000482c8f49/view/index.html"
WEB_SIG = "websig-SECRET-" + "Q" * 40
WEB_SID = "websid-{}-SECRET"
WEB_PAGE_HTML = (
    '<html><head><script src="/client/v68/app.js"></script>'
    '<script src="https://cdn.example.com/x.js"></script></head></html>'
)
WEB_PAGE_JS = 'proxy({"ReadAccount":{}}); proxy({"ReadProfile":{}}); CreateAccount(); AcceptTerms();'
#: A small title-endpoints document, with an xapprove entry whose signature
#: policy and relying party differ from the ones the integration uses.
ENDPOINTS_DOC = {
    "EndPoints": [
        {
            "Protocol": "https", "Host": "xapprove.xboxlive.com", "HostType": "fqdn",
            "RelyingParty": "http://xapprove.xboxlive.com/",
            "SubRelyingParty": "http://xapprove.xboxlive.com/screentime",
            "TokenType": "JWT", "SignaturePolicyIndex": 2,
        },
        {
            "Protocol": "https", "Host": "accounts.xboxlive.com", "HostType": "fqdn",
            "RelyingParty": "http://xboxlive.com", "TokenType": "JWT", "SignaturePolicyIndex": 1,
        },
        {
            "Protocol": "https", "Host": "notifications.xboxlive.com", "HostType": "fqdn",
            "RelyingParty": "http://notifications.xboxlive.com", "SignaturePolicyIndex": 0,
        },
    ],
    "SignaturePolicies": [
        {"Version": 1, "SupportedAlgorithms": ["ES256"], "MaxBodyBytes": 8192},
        {"Version": 2, "SupportedAlgorithms": ["ES256"], "MaxBodyBytes": 4096},
        {"Version": 3, "SupportedAlgorithms": ["ES256", "RS256"], "MaxBodyBytes": 1048576},
    ],
}
#: The explicit XSTS step's token per relying party.
XSTS_TOKENS = {
    "http://xboxlive.com": "xsts-xboxlive-SECRET",
    "http://xapprove.xboxlive.com": "xsts-xapprove-SECRET",
    "https://xapprove.xboxlive.com/": "xsts-xapprove-https-SECRET",
}
#: What the app sends xapprove: the title-bound XSTS for http://xboxlive.com.
XSTS_AUTH = f"XBL3.0 x=4343;{XSTS_TOKENS['http://xboxlive.com']}"
XSTS_XUI = {
    "gtg": "SamParent", "xid": PARENT_XUID, "uhs": "4343", "mgt": "SamParent",
    "umg": "SamParent", "agg": "Adult", "usr": "234", "prv": "184 185", "ugc": "1",
}
DEVICE_CODE = "device-code-SECRET"
USER_CODE = "ABCD-1234"
SECRETS = (
    PARENT_XUID, CHILD_XUID, GAMERTAG, CODE, SISU_ACCESS, DEVICE_TOKEN, SISU_XSTS,
    "sisu-refresh-", TITLE_TOKEN, "sisu-user-SECRET", DEVICE_CODE, *XSTS_TOKENS.values(),
    SISU_DEVICE_TOKEN, *UCS_TOKENS.values(), SESSION_ID, "redirect-SECRET", "other-scope-refresh-SECRET",
    "websid-", WEB_SIG,
)


class FakeResponse:
    def __init__(
        self, status: int, payload: Any = None, headers: dict | None = None, text: str | None = None
    ) -> None:
        self.status = status
        self._payload = payload
        self._text = text
        self.headers = headers or {}

    async def json(self, content_type=None):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload

    async def text(self):
        if self._text is not None:
            return self._text
        return json.dumps(self._payload) if self._payload is not None else ""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def authorize_shape(payload: dict, headers: dict) -> str:
    """"" for a plain sisu/authorize, else ``~ucs:<form>:<prompts>:<contract>``,
    put after the tokens it gives so that a test can tell them apart."""
    if "UcsCheckinToken" not in payload:
        assert "UcsWebToken" not in payload
        return ""
    checkin, web = payload["UcsCheckinToken"], payload["UcsWebToken"]
    form = "t" if checkin.startswith("t=") else "raw"
    prefix = "t=" if form == "t" else ""
    assert checkin == f"{prefix}{UCS_TOKENS[sisu.UCS_CHECKIN_SCOPE]}"
    assert web == f"{prefix}{UCS_TOKENS[sisu.UCS_WEB_SCOPE]}"
    prompts = "prompts" if payload.get("UserPrompts") == [] else "noprompts"
    contract = "c1" if headers.get("x-xbl-contract-version") == "1" else "noc"
    return f"~ucs_{form}_{prompts}_{contract}"


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
        #: Which request headers xapprove accepts: by default the app's way,
        #: the title-bound XSTS for http://xboxlive.com, signed.
        self.accepts = lambda h: h.get("Authorization") == XSTS_AUTH and "Signature" in h
        #: Every xapprove request's headers, in order.
        self.xapprove_headers: list[dict] = []
        #: Relying party -> status the explicit XSTS step answers (default 200).
        self.xsts_status: dict[str, int] = {}
        #: Whether SISU's answer carries a TitleToken / its own DeviceToken.
        self.title_token = True
        self.sisu_device_token = True
        #: Which explicit XSTS requests answer 200 (else 400 XErr 0x8015DC37).
        self.xsts_accepts = lambda headers, props: True
        #: Which headers xapprove accepts on the screen-time settings (None:
        #: the same as ``accepts``).
        self.screentime_accepts = None
        #: xapprove's refusal: headers and body text.
        self.refusal_headers = {"WWW-Authenticate": "Token realm='xboxlive.com', error='token_required'"}
        self.refusal_text: str | None = None
        self.refresh_tokens = {"sisu-refresh-1"}
        self.next_refresh = 2
        self.dead = False
        self.code_dead = False
        self.jwk: dict | None = None
        self.sisu_calls: list[str] = []
        self.sisu_authorize_status = 200
        #: The device-code request: 200, or an error status (refused).
        self.device_code_status = 200
        self.device_code_error = "unsupported_response_type"
        #: Answers to the device-code polls, in order: "ok" or an error code.
        self.device_polls: list[str] = ["ok"]
        #: Every sisu/authorize body and its contract header.
        self.authorize_bodies: list[dict] = []
        self.authorize_contracts: list[str | None] = []
        #: Which sisu/authorize requests answer 200 (else 400).
        self.authorize_accepts = lambda payload, headers: True
        #: Every sisu/authenticate body.
        self.authenticate_bodies: list[dict] = []
        #: The web view: whether authorize answers a WebPage (live: always),
        #: and still after a session was finished; the sessions made, the
        #: ones finished (ReadAccount then ReadProfile), the ones made after.
        self.web_page = False
        self.web_page_after_done = True
        self.sessions = 0
        self.read_account: set[int] = set()
        self.done_sessions: set[int] = set()
        self.fresh_sessions: set[int] = set()
        #: Which proxy POSTs answer 200: (headers, session id) -> bool.
        self.proxy_accepts = lambda h, sid: (
            h.get("Authorization", "").startswith("XBL3.0") and "Signature" in h and sid.startswith("websid-")
        )
        self.proxy_calls: list[str] = []
        #: As live: without a SessionId, authorize's WebPage is bare (no sid).
        self.web_page_needs_session = False
        #: The PKCE challenge of the last sisu/authenticate (the code
        #: exchange's verifier must match it).
        self.last_challenge: str | None = None
        #: The title-endpoints document: 200 by default, or an error status.
        self.endpoints_status = 200
        #: A ``Flow`` value to add to every sisu/authorize answer, or None.
        self.authorize_flow = None

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
        if url.startswith(xapprove.ENDPOINTS_URL):
            assert method == "GET" and "Authorization" not in headers
            if self.endpoints_status != 200:
                return FakeResponse(self.endpoints_status, None)
            return FakeResponse(200, ENDPOINTS_DOC)
        body = kwargs.get("data") if isinstance(kwargs.get("data"), bytes) else b""
        if url == sisu.DEVICE_CODE_URL:
            form = kwargs["data"]
            assert form == {
                "client_id": sisu.CLIENT_ID, "scope": sisu.SCOPE, "response_type": "device_code",
            }
            self.sisu_calls.append("device_code")
            if self.device_code_status != 200:
                return FakeResponse(self.device_code_status, {"error": self.device_code_error})
            return FakeResponse(200, {
                "device_code": DEVICE_CODE, "user_code": USER_CODE,
                "verification_uri": "https://www.microsoft.com/link",
                "expires_in": 900, "interval": 0,
            })
        if url == sisu.TOKEN_URL and kwargs["data"].get("grant_type") == sisu.GRANT_DEVICE_CODE:
            form = kwargs["data"]
            assert form["client_id"] == sisu.CLIENT_ID and form["device_code"] == DEVICE_CODE
            answer = self.device_polls.pop(0) if self.device_polls else "authorization_pending"
            self.sisu_calls.append(f"device_poll:{answer}")
            if answer != "ok":
                return FakeResponse(400, {"error": answer})
            return FakeResponse(200, {
                "token_type": "bearer", "expires_in": 3600, "scope": sisu.SCOPE,
                "access_token": SISU_ACCESS, "refresh_token": self._rotate(),
            })
        if url == sisu.TOKEN_URL:
            form = kwargs["data"]
            assert form["client_id"] == sisu.CLIENT_ID
            self.sisu_calls.append(f"token:{form['grant_type']}:{form['scope']}")
            if form["grant_type"] == "authorization_code":
                assert form["redirect_uri"] == sisu.REDIRECT_URI and form["code_verifier"]
                if self.last_challenge is not None:
                    digest = hashlib.sha256(form["code_verifier"].encode()).digest()
                    assert base64.urlsafe_b64encode(digest).rstrip(b"=").decode() == self.last_challenge
                if form["code"] != CODE or self.code_dead:
                    return FakeResponse(400, {"error": "invalid_grant"})
            elif self.dead or form["refresh_token"] not in self.refresh_tokens:
                return FakeResponse(400, {"error": "invalid_grant"})
            if form["scope"] in UCS_TOKENS:
                return FakeResponse(200, {
                    "token_type": "bearer", "expires_in": 3600, "scope": form["scope"],
                    "access_token": UCS_TOKENS[form["scope"]], "user_id": "abc",
                })
            if form["scope"] != sisu.SCOPE:
                # As live for another scope: no rotation of the stored token
                # is expected; a new one may still come back.
                return FakeResponse(200, {
                    "token_type": "bearer", "expires_in": 3600, "scope": form["scope"],
                    "access_token": SISU_ACCESS, "refresh_token": "other-scope-refresh-SECRET",
                })
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
                "DisplayClaims": {"xdi": {"did": "F700000000000001", "dcs": "0"}},
            })
        if url.startswith(sisu.SISU_PROXY_URL + "?"):
            sid = parse_qs(urlsplit(url).query)["sessionid"][0]
            payload = json.loads(kwargs["data"])
            assert headers["x-xbl-contract-version"] == "3"
            assert headers["Content-Type"] == "application/json"
            if "Signature" in headers:
                verify_signature(
                    headers["Signature"], self.jwk, "POST", url, headers.get("Authorization", ""),
                    kwargs["data"],
                )
            read = next(iter(payload))
            assert payload == {read: {}} and read in ("ReadAccount", "ReadProfile")
            self.proxy_calls.append(read)
            if not self.proxy_accepts(headers, sid):
                return FakeResponse(401, None)
            session = int(sid.split("-")[1]) if sid.startswith("websid-") else 0
            if read == "ReadAccount":
                self.read_account.add(session)
                return FakeResponse(200, {"Account": {"Gamertag": "SamParent", "AgeGroup": "Adult"}})
            if session in self.read_account:
                self.done_sessions.add(session)
            return FakeResponse(200, {"Profile": {"Gamertag": "SamParent"}, "Prompts": []})
        if url.startswith(WEB_PAGE_BASE):
            assert method == "GET"
            return FakeResponse(200, None, {"Content-Type": "text/html"}, WEB_PAGE_HTML)
        if url == "https://sisu.xboxlive.com/client/v68/app.js":
            return FakeResponse(200, None, {"Content-Type": "text/javascript"}, WEB_PAGE_JS)
        if url == sisu.SISU_AUTHENTICATE_URL:
            self.sisu_calls.append("authenticate")
            payload = json.loads(kwargs["data"])
            verify_signature(headers["Signature"], self.jwk, "POST", url, "", kwargs["data"])
            self.authenticate_bodies.append(payload)
            query = payload["Query"]
            self.last_challenge = query["code_challenge"]
            link = "https://login.live.com/oauth20_authorize.srf?" + urlencode({
                "client_id": sisu.CLIENT_ID, "redirect_uri": payload["RedirectUri"],
                "response_type": "code", "scope": "service::user.auth.xboxlive.com::MBI_SSL",
                "code_challenge": query["code_challenge"], "code_challenge_method": "S256",
                "state": f"sisu-{query['state']}", "x": "redirect-SECRET",
            })
            return FakeResponse(
                200, {"MsaOauthRedirect": link, "MsaRequestParameters": {"ru": "x", "cobrandid": "y"}},
                {"X-SessionId": SESSION_ID},
            )
        if url == sisu.SISU_AUTHORIZE_URL:
            self.sisu_calls.append("authorize")
            payload = json.loads(kwargs["data"])
            assert payload["ProofKey"] == self.jwk
            verify_signature(headers["Signature"], self.jwk, "POST", url, "", kwargs["data"])
            assert payload["AccessToken"] == f"t={SISU_ACCESS}"
            assert payload["DeviceToken"] == DEVICE_TOKEN
            assert payload["AppId"] == sisu.CLIENT_ID and payload["RelyingParty"] == "http://xboxlive.com"
            self.authorize_bodies.append(payload)
            self.authorize_contracts.append(headers.get("x-xbl-contract-version"))
            if self.sisu_authorize_status != 200:
                return FakeResponse(self.sisu_authorize_status, None, {"x-err": "2148916236"})
            if not self.authorize_accepts(payload, headers):
                return FakeResponse(400, {"XErr": 2148916233})
            shape = authorize_shape(payload, headers)
            if "SessionId" in payload:
                assert payload["SessionId"] == SESSION_ID
                shape += "~sess"
            web_page = None
            if self.web_page and self.web_page_needs_session and "SessionId" not in payload:
                web_page = WEB_PAGE_BASE  # bare, as live
            elif self.web_page:
                self.sessions += 1
                if self.done_sessions:
                    self.fresh_sessions.add(self.sessions)
                shape += f"~s{self.sessions}"
                if self.web_page_after_done or not self.done_sessions:
                    web_page = (
                        f"{WEB_PAGE_BASE}?MS-CV=abc.1&redirect=ms-xal-00000000482c8f49%3A%2F%2Fauth"
                        f"&sid={WEB_SID.format(self.sessions)}&sig={WEB_SIG}&state=st1"
                    )
            return FakeResponse(200, {
                **({"DeviceToken": SISU_DEVICE_TOKEN} if self.sisu_device_token else {}),
                **({"TitleToken": {
                    "Token": TITLE_TOKEN,
                    "NotAfter": iso_utc(dt_util.utcnow() + timedelta(days=14)),
                    "DisplayClaims": {"xti": {"tid": TITLE_ID}},
                }} if self.title_token else {}),
                **({"WebPage": web_page} if web_page else {}),
                **({"Flow": self.authorize_flow} if self.authorize_flow is not None else {}),
                "UserToken": {"Token": f"sisu-user-SECRET{shape}",
                              "NotAfter": iso_utc(dt_util.utcnow() + timedelta(days=14))},
                "AuthorizationToken": {
                    "Token": f"{SISU_XSTS}{shape}",
                    "NotAfter": iso_utc(dt_util.utcnow() + timedelta(hours=16)),
                    "DisplayClaims": {"xui": [{"uhs": "4343", "xid": PARENT_XUID, "gtg": "SamParent"}]},
                },
            })
        if url == sisu.XSTS_AUTHORIZE_URL:
            payload = json.loads(kwargs["data"])
            rp = payload["RelyingParty"]
            self.sisu_calls.append(f"xsts:{rp}")
            if "Signature" in headers:
                verify_signature(headers["Signature"], self.jwk, "POST", url, "", kwargs["data"])
            assert headers["x-xbl-contract-version"] == "1"
            assert headers["Content-Type"] in ("application/json; charset=utf-8", "application/json")
            assert headers["User-Agent"].startswith("XAL Android")
            assert payload["TokenType"] == "JWT"
            props = payload["Properties"]
            assert props["SandboxId"] == "RETAIL"
            assert props["DeviceToken"] in (DEVICE_TOKEN, SISU_DEVICE_TOKEN)
            assert len(props["UserTokens"]) == 1 and props["UserTokens"][0].startswith("sisu-user-SECRET")
            shape = props["UserTokens"][0][len("sisu-user-SECRET"):]
            if not self.title_token:
                assert "TitleToken" not in props
            elif "TitleToken" in props:
                assert props["TitleToken"] == TITLE_TOKEN
            if "OptionalDisplayClaims" in props:
                assert {"mgt", "umg", "agg", "prv", "ugc"} <= set(props["OptionalDisplayClaims"])
            status = self.xsts_status.get(rp, 200)
            if status != 200 or rp not in XSTS_TOKENS:
                return FakeResponse(status if status != 200 else 400, {"XErr": 2148916233})
            if not self.xsts_accepts(headers, props):
                return FakeResponse(400, {"XErr": 2148916279})
            return FakeResponse(200, {
                "Token": f"{XSTS_TOKENS[rp]}{shape}",
                "NotAfter": iso_utc(dt_util.utcnow() + timedelta(hours=16)),
                "DisplayClaims": {"xui": [dict(XSTS_XUI)]},
            })
        if "Signature" in headers and self.jwk is not None:
            verify_signature(headers["Signature"], self.jwk, method, url, headers["Authorization"], body)
        if url.startswith("https://accounts.xboxlive.com/family/"):
            assert headers["Authorization"].startswith(SISU_AUTH)
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
            self.xapprove_headers.append(dict(headers))
            if self.xapprove_status != 200:
                return FakeResponse(self.xapprove_status, None, {"x-err": "2148916238"})
            accepts = self.accepts
            if url.endswith("/settings/screentime") and self.screentime_accepts is not None:
                accepts = self.screentime_accepts
            if not accepts(headers):
                return FakeResponse(401, None, dict(self.refusal_headers), self.refusal_text)
            if url.endswith("/settings/screentime"):
                assert method == "GET"
                return FakeResponse(200, {"enabled": True})
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
