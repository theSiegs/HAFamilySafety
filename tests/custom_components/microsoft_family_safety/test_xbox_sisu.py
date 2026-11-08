"""The Xbox Family Settings sign-in (SISU), with mocked HTTP.

The authorize URL and the pasted redirect, the code exchange, the device
token and SISU authorize (each request signed with the ProofKey, checked
here against the public key in the request), the refresh with a rotated
refresh token, xapprove signed with it, a 401 falling back, a dead refresh
token raising a Repairs issue, the options-flow step, and the probe.
"""

from __future__ import annotations

import base64
import json
import struct
import time
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from pytest_homeassistant_custom_component.common import MockConfigEntry
from unittest.mock import patch

from homeassistant import data_entry_flow
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from custom_components.microsoft_family_safety import xbox_sisu as sisu
from custom_components.microsoft_family_safety.const import (
    CONF_XBOX_SIGNED_IN_AT,
    CONF_XBOX_XAPPROVE,
    DOMAIN,
)
from custom_components.microsoft_family_safety.screen_time import iso_utc

from .test_platform_lock import CHILD
from .test_xbox_xapprove import (
    CHILD_XUID,
    PARENT_XUID,
    PATCH_SESSION,
    SECRETS,
    FakeResponse,
    XboxServer,
    _hours,
    _make_rig,
)

CODE = "auth-code-SECRET"
SISU_ACCESS = "sisu-access-SECRET"
DEVICE_TOKEN = "device-token-SECRET"
SISU_XSTS = "sisu-xsts-SECRET"
SISU_SECRETS = (CODE, SISU_ACCESS, DEVICE_TOKEN, SISU_XSTS, "sisu-refresh-")
SISU_AUTH = f"XBL3.0 x=4343;{SISU_XSTS}"


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


class SisuServer(XboxServer):
    """XboxServer plus the Xbox Family Settings client's sign-in."""

    def __init__(self) -> None:
        super().__init__()
        self.refresh_tokens = {"sisu-refresh-1"}
        self.next_refresh = 2
        self.dead = False
        self.jwk: dict | None = None
        self.sisu_calls: list[str] = []
        self.family_auth = {SISU_AUTH}
        self.accepts = self._signed_sisu
        self.sisu_authorize_status = 200
        self.code_dead = False

    def _rotate(self) -> str:
        token = f"sisu-refresh-{self.next_refresh}"
        self.next_refresh += 1
        self.refresh_tokens = {token}
        return token

    def _signed_sisu(self, headers: dict) -> bool:
        if headers.get("Authorization") != SISU_AUTH or "Signature" not in headers:
            return False
        return True  # the signature itself is checked in request()

    def request(self, method, url, **kwargs):
        headers = kwargs.get("headers") or {}
        body = kwargs.get("data") if isinstance(kwargs.get("data"), bytes) else b""
        if url == sisu.TOKEN_URL and kwargs["data"].get("client_id") == sisu.CLIENT_ID:
            form = kwargs["data"]
            self.sisu_calls.append(f"token:{form['grant_type']}:{form['scope']}")
            if form["grant_type"] == "authorization_code":
                assert form["redirect_uri"] == sisu.REDIRECT_URI and form["code_verifier"]
                if form["code"] != CODE or self.code_dead:
                    return FakeResponse(400, {"error": "invalid_grant"})
            else:
                if self.dead or form["refresh_token"] not in self.refresh_tokens:
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
        if "Signature" in headers and self.jwk is not None and (
            url.startswith("https://xapprove.xboxlive.com/")
            or url.startswith("https://accounts.xboxlive.com/")
        ):
            verify_signature(headers["Signature"], self.jwk, method, url, headers["Authorization"], body)
        return super().request(method, url, **kwargs)


def _no_secrets(value) -> None:
    text = json.dumps(value, default=str)
    for secret in (*SECRETS, *SISU_SECRETS):
        assert secret not in text


@pytest.fixture
def server():
    srv = SisuServer()
    with patch(PATCH_SESSION, return_value=srv), patch(
        "custom_components.microsoft_family_safety.config_flow.async_get_clientsession",
        return_value=srv,
    ):
        yield srv


async def _signed_in_rig(hass, server, hass_storage):
    coord, entry = _make_rig(hass)
    key = sisu.ProofKey.generate()
    server.jwk = key.jwk()
    credential = sisu.new_credential("sisu-refresh-1", key)
    await coord._xbox_signin_store.async_save(credential)
    return coord, entry


# The authorize URL and the redirect -------------------------------------------


def test_the_authorize_url_uses_pkce():
    verifier, challenge = sisu.new_pkce()
    url = sisu.authorize_url(challenge, "state-1")
    parts = urlsplit(url)
    query = {k: v[0] for k, v in parse_qs(parts.query).items()}
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == sisu.AUTHORIZE_URL
    assert query["client_id"] == "00000000482C8F49"
    assert query["scope"] == "service::user.auth.xboxlive.com::MBI_SSL"
    assert query["redirect_uri"] == "https://login.live.com/oauth20_desktop.srf"
    assert query["response_type"] == "code" and query["display"] == "android_phone"
    assert query["code_challenge_method"] == "S256" and query["state"] == "state-1"
    import hashlib
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert query["code_challenge"] == expected
    assert 43 <= len(verifier) <= 128


def test_the_redirect_is_checked():
    good = f"https://login.live.com/oauth20_desktop.srf?code={CODE}&state=s1&lc=1033"
    assert sisu.code_from_redirect(good, "s1") == CODE
    with pytest.raises(ValueError, match="state_mismatch"):
        sisu.code_from_redirect(good, "other")
    with pytest.raises(ValueError, match="invalid_redirect"):
        sisu.code_from_redirect(f"https://example.com/?code={CODE}&state=s1", "s1")
    with pytest.raises(ValueError, match="signin_error:access_denied"):
        sisu.code_from_redirect(
            "https://login.live.com/oauth20_desktop.srf?error=access_denied&state=s1", "s1"
        )


def test_the_redirect_may_be_copied_loosely():
    good = f"https://login.live.com/oauth20_desktop.srf?code={CODE}&state=s1&lc=1033"
    # Quotes, angle brackets, spaces and a line break from a phone's copy.
    assert sisu.code_from_redirect(f'  "{good}"\n', "s1") == CODE
    assert sisu.code_from_redirect(f"<{good[:40]} {good[40:]}>", "s1") == CODE
    # Without https://.
    assert sisu.code_from_redirect(good.removeprefix("https://"), "s1") == CODE
    # Without the state (the PKCE verifier still binds the code).
    assert sisu.code_from_redirect(
        f"https://login.live.com/oauth20_desktop.srf?code={CODE}", "s1"
    ) == CODE
    # Only the code.
    bare = "M.C507_BAY.2.U.0a1b2c3d-1111-2222-3333-444455556666"
    assert sisu.code_from_redirect(f" {bare} ", "s1") == bare
    # The sign-in link itself, pasted back by mistake.
    link = sisu.authorize_url("challenge", "s1")
    with pytest.raises(ValueError, match="pasted_link"):
        sisu.code_from_redirect(link, "s1")
    with pytest.raises(ValueError, match="invalid_redirect"):
        sisu.code_from_redirect("hello", "s1")


# Signing --------------------------------------------------------------------


def test_the_signature_is_well_formed_and_verifiable():
    key = sisu.ProofKey.generate()
    body = b'{"a":1}'
    url = "https://sisu.xboxlive.com/authorize?x=1"
    header = key.sign("post", url, "XBL3.0 x=1;t", body, timestamp=133_000_000_000_000_000)
    assert verify_signature(header, key.jwk(), "POST", url, "XBL3.0 x=1;t", body) == 133_000_000_000_000_000
    payload = sisu.signed_payload("POST", "/authorize?x=1", "XBL3.0 x=1;t", body, 5)
    assert payload == (
        b"\x00\x00\x00\x01\x00" + struct.pack(">Q", 5) + b"\x00POST\x00/authorize?x=1\x00"
        b"XBL3.0 x=1;t\x00" + body + b"\x00"
    )
    # A changed body does not verify.
    with pytest.raises(Exception):
        verify_signature(header, key.jwk(), "POST", url, "XBL3.0 x=1;t", b"{}")
    # The timestamp is a Windows FILETIME of now.
    now = sisu._filetime()
    assert abs(now - int((time.time() + 11644473600) * 10_000_000)) < 10_000_000
    # The key survives a save.
    again = sisu.ProofKey.from_pem(key.to_pem())
    assert again.jwk() == key.jwk()


# Sign-in, refresh -------------------------------------------------------------


async def test_complete_sign_in(server):
    _verifier, _challenge = sisu.new_pkce()
    credential = await sisu.async_complete_sign_in(
        server, f"https://login.live.com/oauth20_desktop.srf?code={CODE}&state=s1", "s1", "v" * 43,
    )
    assert server.sisu_calls == [
        "token:authorization_code:service::user.auth.xboxlive.com::MBI_SSL", "device", "authorize",
    ]
    assert credential["refresh_token"] == "sisu-refresh-2"
    assert sisu.ProofKey.from_pem(credential["proof_key"]).jwk() == server.jwk
    assert credential["device_id"]


async def test_refresh_rotates_and_is_saved(server, freezer):
    key = sisu.ProofKey.generate()
    saved: list[dict] = []

    async def save(credential):
        saved.append(credential)

    auth = sisu.XboxSisuAuth(lambda: server, sisu.new_credential("sisu-refresh-1", key), save)
    tokens = await auth.async_xsts()
    assert tokens.header() == SISU_AUTH and tokens.parent_xuid == PARENT_XUID
    assert saved[-1]["refresh_token"] == "sisu-refresh-2"
    await auth.async_xsts()
    assert server.sisu_calls == [
        "token:refresh_token:service::user.auth.xboxlive.com::MBI_SSL", "device", "authorize",
    ]
    freezer.tick(timedelta(hours=17))  # the XSTS ran out; the device token did not
    await auth.async_xsts()
    assert server.sisu_calls[3:] == [
        "token:refresh_token:service::user.auth.xboxlive.com::MBI_SSL", "authorize",
    ]
    assert saved[-1]["refresh_token"] == "sisu-refresh-3"


async def test_a_dead_refresh_token(server):
    server.dead = True
    auth = sisu.XboxSisuAuth(
        lambda: server, sisu.new_credential("sisu-refresh-1"), lambda c: None
    )
    with pytest.raises(sisu.XboxSignInExpired):
        await auth.async_xsts()


# The integration with the sign-in ----------------------------------------------


async def test_lock_uses_the_signed_xbox_sign_in(hass, server, hass_storage):
    coord, _ = await _signed_in_rig(hass, server, hass_storage)
    result = await coord.async_lock_platform(CHILD, "Xbox", _hours(3))

    assert result["lock_path"] == "xapprove"
    assert server.create_types() == [2]
    # The Family Safety sign-in was not used for Xbox Live at all.
    assert server.scopes == []
    assert coord.xbox_signed_in and coord._xapprove_method.name == "sisu"


async def test_a_401_on_the_xbox_sign_in_falls_back(hass, server, hass_storage):
    coord, _ = await _signed_in_rig(hass, server, hass_storage)
    server.accepts = lambda h: False
    result = await coord.async_lock_platform(CHILD, "Xbox", _hours(3))

    assert result["lock_path"] == "web"
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"xapprove_refused_{coord.entry.entry_id}")


async def test_a_dead_sign_in_raises_its_repair_and_falls_back(hass, server, hass_storage):
    coord, _ = await _signed_in_rig(hass, server, hass_storage)
    server.dead = True
    result = await coord.async_lock_platform(CHILD, "Xbox", _hours(3))

    assert result["lock_path"] == "web"
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"xbox_signin_expired_{coord.entry.entry_id}")


async def test_the_probe_with_the_xbox_sign_in(hass, server, hass_storage, caplog):
    caplog.set_level("DEBUG")
    coord, _ = await _signed_in_rig(hass, server, hass_storage)
    result = await coord.async_probe_xapprove(True)

    assert result["identity"] == "xbox_family_settings" and result["xbox_signed_in"] is True
    assert result["steps"]["sisu_authorize"]["status"] == 200
    assert result["steps"]["sisu_authorize"]["authorization_token_received"] is True
    assert result["steps"]["device_authenticate"]["status"] == 200
    assert result["family"]["ok"] is True and result["family"]["adults"] == 1
    assert result["overrides_attempts"] == [
        {"name": "sisu", "stage": "xapprove", "status": 200, "xerr": None}
    ]
    assert result["working_method"] == "sisu" and result["method_saved"] is True
    _no_secrets(result)
    ours = " | ".join(
        r.getMessage() for r in caplog.records if r.name.startswith("custom_components.")
    )
    for secret in (*SECRETS, *SISU_SECRETS):
        assert secret not in ours


async def test_the_probe_when_the_xbox_sign_in_is_refused(hass, server, hass_storage):
    coord, _ = await _signed_in_rig(hass, server, hass_storage)
    server.sisu_authorize_status = 400
    server.family_auth = {"XBL3.0 x=4242;xsts-token-SECRET"}
    server.accepts = lambda h: False
    result = await coord.async_probe_xapprove(True)

    assert result["xbox_signin_error"]["step"] == "sisu_authorize"
    assert result["identity"] == "family_safety_signin"
    assert result["family"]["ok"] is True
    names = [a["name"] for a in result["overrides_attempts"]]
    assert names[:4] == ["sisu", "sisu_unsigned", "sisu_contract_1", "sisu_xapprove_scope_wlid"]
    assert result["working_method"] is None
    _no_secrets(result)


# The options flow ------------------------------------------------------------------


SETUP = "custom_components.microsoft_family_safety.async_setup_entry"
INIT_FORM = {"update_interval": 300, "platforms": ["Windows", "Xbox"]}


async def _to_the_sign_in_page(hass, entry) -> tuple[dict, str]:
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**INIT_FORM, "xbox_sign_in": True},
    )
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "xbox_signin"
    url = result["description_placeholders"]["url"]
    return result, parse_qs(urlsplit(url).query)["state"][0]


def _landed(state: str, code: str = CODE) -> dict:
    return {"redirect_url": f"https://login.live.com/oauth20_desktop.srf?code={code}&state={state}"}


def _expired_issue(hass, entry) -> None:
    ir.async_create_issue(
        hass, DOMAIN, f"xbox_signin_expired_{entry.entry_id}", is_fixable=False,
        severity=ir.IssueSeverity.WARNING, translation_key="xbox_signin_expired",
    )


async def test_the_options_flow_signs_in(hass, server, hass_storage):
    """Without a running integration the sign-in is saved, not checked."""
    entry = MockConfigEntry(
        domain=DOMAIN, data={"refresh_token": "x"}, options={CONF_XBOX_XAPPROVE: True},
    )
    entry.add_to_hass(hass)
    with patch(SETUP, return_value=True):
        result, state = await _to_the_sign_in_page(hass, entry)

        bad = await hass.config_entries.options.async_configure(
            result["flow_id"], {"redirect_url": "https://login.live.com/oauth20_desktop.srf?code=x&state=nope"},
        )
        assert bad["errors"] == {"base": "state_mismatch"}

        checked = await hass.config_entries.options.async_configure(result["flow_id"], _landed(state))
        assert checked["step_id"] == "xbox_signin_unchecked"
        assert checked["description_placeholders"]["reason"] == "the integration is not running"
        # Saved already, before the last page.
        stored = hass_storage[f"{DOMAIN}.xbox_signin.{entry.entry_id}"]["data"]
        assert stored["refresh_token"] == "sisu-refresh-2"
        assert "BEGIN PRIVATE KEY" in stored["proof_key"]
        done = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_XBOX_XAPPROVE: False},
        )
    assert done["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_XBOX_SIGNED_IN_AT]
    assert entry.options[CONF_XBOX_XAPPROVE] is False
    assert "xbox_sign_in" not in entry.options
    # Nothing secret in the options.
    assert "sisu-refresh" not in json.dumps(dict(entry.options))


async def test_the_flow_checks_the_sign_in_and_offers_xapprove(hass, server, hass_storage):
    coord, entry = _make_rig(hass, options={CONF_XBOX_XAPPROVE: False})
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coord
    _expired_issue(hass, entry)
    with patch(SETUP, return_value=True):
        result, state = await _to_the_sign_in_page(hass, entry)
        checked = await hass.config_entries.options.async_configure(result["flow_id"], _landed(state))
        assert checked["step_id"] == "xbox_signin_works"
        # The box is ticked for the user.
        field = next(iter(checked["data_schema"].schema))
        assert str(field) == CONF_XBOX_XAPPROVE and field.default() is True
        done = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_XBOX_XAPPROVE: True},
        )
    assert done["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_XBOX_XAPPROVE] is True
    # Reads only: nothing written, the Family Safety sign-in not used, no refresh.
    assert server.creates == [] and server.scopes == []
    assert [c for c in server.sisu_calls if c.startswith("token:refresh")] == []
    # The way that worked is saved, as probe_xapprove would have.
    assert hass_storage[f"{DOMAIN}.xapprove.{entry.entry_id}"]["data"]["method"] == "sisu"
    # The old "sign in again" issue is gone.
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"xbox_signin_expired_{entry.entry_id}") is None


async def test_the_flow_says_when_xbox_refuses(hass, server, hass_storage):
    coord, entry = _make_rig(hass, options={CONF_XBOX_XAPPROVE: True})
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coord
    coord._xuids[CHILD] = (CHILD_XUID, "family_name")  # no family read needed
    server.accepts = lambda headers: False
    with patch(SETUP, return_value=True):
        result, state = await _to_the_sign_in_page(hass, entry)
        checked = await hass.config_entries.options.async_configure(result["flow_id"], _landed(state))
        assert checked["step_id"] == "xbox_signin_refused"
        assert checked["description_placeholders"]["reason"] == "HTTP 401"
        field = next(iter(checked["data_schema"].schema))
        assert field.default() is False
        await hass.config_entries.options.async_configure(result["flow_id"], {CONF_XBOX_XAPPROVE: False})
    assert entry.options[CONF_XBOX_XAPPROVE] is False
    assert f"{DOMAIN}.xapprove.{entry.entry_id}" not in hass_storage
    # The sign-in itself is kept: signing out is a separate choice.
    assert hass_storage[f"{DOMAIN}.xbox_signin.{entry.entry_id}"]["data"]["refresh_token"]


async def test_an_expired_code_and_a_pasted_link_say_what_to_do(hass, server, hass_storage):
    entry = MockConfigEntry(domain=DOMAIN, data={"refresh_token": "x"})
    entry.add_to_hass(hass)
    with patch(SETUP, return_value=True):
        result, state = await _to_the_sign_in_page(hass, entry)
        server.code_dead = True
        expired = await hass.config_entries.options.async_configure(result["flow_id"], _landed(state))
        assert expired["errors"] == {"base": "code_expired"}
        link = await hass.config_entries.options.async_configure(
            result["flow_id"], {"redirect_url": expired["description_placeholders"]["url"]},
        )
        assert link["errors"] == {"base": "pasted_link"}
        # The same link stays on the page; a new code from it works.
        assert link["description_placeholders"]["url"] == result["description_placeholders"]["url"]
        server.code_dead = False
        checked = await hass.config_entries.options.async_configure(result["flow_id"], _landed(state))
    assert checked["step_id"] == "xbox_signin_unchecked"
    assert f"{DOMAIN}.xbox_signin.{entry.entry_id}" in hass_storage


async def test_a_closed_dialog_keeps_the_link(hass, server, hass_storage):
    entry = MockConfigEntry(domain=DOMAIN, data={"refresh_token": "x"})
    entry.add_to_hass(hass)
    with patch(SETUP, return_value=True):
        first, state = await _to_the_sign_in_page(hass, entry)
        hass.config_entries.options.async_abort(first["flow_id"])
        # Opened again: the same link, so the address copied from it works.
        second, again = await _to_the_sign_in_page(hass, entry)
        assert again == state
        checked = await hass.config_entries.options.async_configure(second["flow_id"], _landed(state))
        assert checked["step_id"] == "xbox_signin_unchecked"
        hass.config_entries.options.async_abort(second["flow_id"])
        # After a sign-in the next link is a new one.
        third, fresh = await _to_the_sign_in_page(hass, entry)
        assert fresh != state
        hass.config_entries.options.async_abort(third["flow_id"])


async def test_the_options_page_shows_the_sign_in(hass, server, hass_storage):
    entry = MockConfigEntry(domain=DOMAIN, data={"refresh_token": "x"})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["description_placeholders"]["xbox_signin_status"] == "not signed in"
    assert "xbox_sign_out" not in {str(k) for k in result["data_schema"].schema}
    hass.config_entries.options.async_abort(result["flow_id"])

    hass.config_entries.async_update_entry(
        entry, options={CONF_XBOX_SIGNED_IN_AT: "2026-11-08T12:00:00+00:00"}
    )
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["description_placeholders"]["xbox_signin_status"].startswith("signed in on 2026-11-08")
    assert "xbox_sign_out" in {str(k) for k in result["data_schema"].schema}
    hass.config_entries.options.async_abort(result["flow_id"])

    _expired_issue(hass, entry)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["description_placeholders"]["xbox_signin_status"] == "expired, sign in again"
    # Signing out forgets it and drops the issue.
    with patch(SETUP, return_value=True):
        await hass.config_entries.options.async_configure(
            result["flow_id"], {**INIT_FORM, "xbox_sign_out": True},
        )
    assert CONF_XBOX_SIGNED_IN_AT not in entry.options
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"xbox_signin_expired_{entry.entry_id}") is None
