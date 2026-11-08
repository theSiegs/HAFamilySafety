"""The Xbox Family Settings sign-in (SISU), with mocked HTTP.

The authorize URL and the pasted redirect, the code exchange, the device
token and SISU authorize (each request signed with the ProofKey, checked
against the public key in the request: xbox_fakes.verify_signature), the
refresh with a rotated refresh token, xapprove signed with it, a 401 and a
dead refresh token falling back to today's schedule, the options flow.
"""

from __future__ import annotations

import base64
import json
import struct
import time
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from unittest.mock import patch

from homeassistant import data_entry_flow
from homeassistant.helpers import issue_registry as ir

from custom_components.microsoft_family_safety import xbox_sisu as sisu
from custom_components.microsoft_family_safety.const import (
    CONF_XBOX_SIGNED_IN_AT,
    CONF_XBOX_XAPPROVE,
    DOMAIN,
)

from .test_platform_lock import CHILD
from .xbox_fakes import (
    CHILD_XUID,
    CODE,
    PARENT_XUID,
    PATCH_FLOW_SESSION,
    PATCH_SESSION,
    SECRETS,
    SISU_AUTH,
    XboxServer,
    hours as _hours,
    make_rig,
    no_secrets as _no_secrets,
    stop_timers,
    verify_signature,
)


@pytest.fixture(autouse=True)
def _timers():
    yield
    stop_timers()


@pytest.fixture
def server():
    srv = XboxServer()
    with patch(PATCH_SESSION, return_value=srv), patch(PATCH_FLOW_SESSION, return_value=srv):
        yield srv


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
    coord, _ = await make_rig(hass, server)
    result = await coord.async_lock_platform(CHILD, "Xbox", _hours(3))

    assert result["lock_path"] == "xapprove"
    assert server.create_types() == [2]
    # Every token request was the Xbox Family Settings client's (the fake
    # server asserts it); the Family web session was never used.
    assert coord.web_api.web_calls == []
    assert coord.xbox_signed_in and coord._xapprove_method.name == "sisu"


async def test_a_401_on_the_xbox_sign_in_falls_back(hass, server, hass_storage):
    coord, _ = await make_rig(hass, server)
    server.accepts = lambda h: False
    result = await coord.async_lock_platform(CHILD, "Xbox", _hours(3))

    assert result["lock_path"] == "mobile_schedule"
    assert coord.web_api.web_calls == []
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"xapprove_refused_{coord.entry.entry_id}")


async def test_a_dead_sign_in_raises_its_repair_and_falls_back(hass, server, hass_storage):
    coord, _ = await make_rig(hass, server)
    server.dead = True
    result = await coord.async_lock_platform(CHILD, "Xbox", _hours(3))

    assert result["lock_path"] == "mobile_schedule"
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"xbox_signin_expired_{coord.entry.entry_id}")


async def test_the_probe_with_the_xbox_sign_in(hass, server, hass_storage, caplog):
    caplog.set_level("DEBUG")
    coord, _ = await make_rig(hass, server)
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
    for secret in SECRETS:
        assert secret not in ours


async def test_the_probe_when_the_xbox_sign_in_is_refused(hass, server, hass_storage):
    coord, _ = await make_rig(hass, server)
    server.sisu_authorize_status = 400
    result = await coord.async_probe_xapprove(True)

    assert result["xbox_signin_error"]["step"] == "sisu_authorize"
    assert result["identity"] == "xbox_family_settings"
    assert "family" not in result and "overrides_attempts" not in result
    assert coord.web_api.web_calls == []
    _no_secrets(result)


# The options flow ------------------------------------------------------------------


SETUP = "custom_components.microsoft_family_safety.async_setup_entry"
INIT_FORM = {"update_interval": 300, "platforms": ["Windows"]}


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


@pytest.mark.parametrize("web_session", [True, False])
async def test_the_flow_checks_the_sign_in_and_offers_xapprove(hass, server, hass_storage, web_session):
    coord, entry = await make_rig(
        hass, server, {CONF_XBOX_XAPPROVE: False}, signed_in=False, web_session=web_session
    )
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
    assert server.creates == [] and coord.web_api.web_calls == []
    assert [c for c in server.sisu_calls if c.startswith("token:refresh")] == []
    # The way that worked is saved, as probe_xapprove would have.
    assert hass_storage[f"{DOMAIN}.xapprove.{entry.entry_id}"]["data"]["method"] == "sisu"
    # The old "sign in again" issue is gone.
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"xbox_signin_expired_{entry.entry_id}") is None


async def test_the_flow_says_when_xbox_refuses(hass, server, hass_storage):
    coord, entry = await make_rig(hass, server, signed_in=False)
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
