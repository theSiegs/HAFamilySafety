"""probe_xbox_token: non-secret answers only, and the stored token untouched."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, MockUser

from homeassistant.core import Context
from homeassistant.exceptions import ServiceValidationError, Unauthorized
from homeassistant.util import dt as dt_util

from custom_components.microsoft_family_safety import _register_services
from custom_components.microsoft_family_safety import xbox_token_probe as probe
from custom_components.microsoft_family_safety.const import DOMAIN
from custom_components.microsoft_family_safety.coordinator import (
    FamilySafetyDataUpdateCoordinator,
)

STORED = "stored-family-refresh-SECRET"
NEW_RT = "new-xbox-refresh-SECRET"
ACCESS = "xbox-access-SECRET"
USER_TOKEN = "user-token-SECRET"
XSTS_TOKEN = "xsts-token-SECRET"
SECRETS = (STORED, NEW_RT, ACCESS, USER_TOKEN, XSTS_TOKEN, "Alex123", "2533274000000000")


class FakeResponse:
    def __init__(self, status: int, payload: Any, headers: dict | None = None) -> None:
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


class FakeSession:
    """Answers per URL; user/authenticate answers per RpsTicket prefix."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.token = FakeResponse(200, {
            "token_type": "bearer", "expires_in": 86400,
            "scope": probe.XBOX_SCOPE, "access_token": ACCESS,
            "refresh_token": NEW_RT, "user_id": "abc",
        })
        self.user = {"t": FakeResponse(400, None, {"x-err": "2148916238"}),
                     "d": FakeResponse(200, {
                         "Token": USER_TOKEN,
                         "NotAfter": (dt_util.utcnow().replace(microsecond=0)).isoformat(),
                         "DisplayClaims": {"xui": [{"uhs": "123"}]},
                     })}
        self.xsts = FakeResponse(200, {
            "Token": XSTS_TOKEN,
            "NotAfter": "2099-01-01T00:00:00Z",
            "DisplayClaims": {"xui": [{"gtg": "Alex123", "xid": "2533274000000000", "uhs": "123"}]},
        })

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if url == probe.TOKEN_URL:
            return self.token
        if url == probe.USER_AUTH_URL:
            ticket = kwargs["json"]["Properties"]["RpsTicket"]
            return self.user[ticket.split("=", 1)[0]]
        if url == probe.XSTS_URL:
            return self.xsts
        raise AssertionError(url)


def _no_secrets(result) -> None:
    text = json.dumps(result)
    for secret in SECRETS:
        assert secret not in text


async def test_full_chain_reports_only_facts():
    session = FakeSession()
    result = await probe.async_probe_xbox_token(session, STORED)

    token = result["steps"]["msa_refresh"]
    assert token["status"] == 200
    assert token["scope"] == probe.XBOX_SCOPE
    assert token["expires_in"] == 86400
    assert token["access_token_received"] is True
    assert token["refresh_token_returned"] is True and token["refresh_token_discarded"] is True
    form = session.calls[0][1]["data"]
    assert form == {
        "grant_type": "refresh_token", "client_id": "000000000004893A",
        "scope": "service::user.auth.xboxlive.com::MBI_SSL", "refresh_token": STORED,
    }

    attempts = result["steps"]["user_authenticate"]
    assert [a["ticket_prefix"] for a in attempts] == ["t", "d"]
    assert attempts[0]["status"] == 400 and attempts[0]["xerr"] == "2148916238"
    assert attempts[1]["status"] == 200 and attempts[1]["token_received"] is True
    body = session.calls[2][1]["json"]
    assert body["RelyingParty"] == "http://auth.xboxlive.com"
    assert body["Properties"]["AuthMethod"] == "RPS"
    assert body["Properties"]["SiteName"] == "user.auth.xboxlive.com"
    assert body["Properties"]["RpsTicket"] == f"d={ACCESS}"

    xsts = result["steps"]["xsts_authorize"]
    assert xsts["status"] == 200
    assert xsts["display_claims_xui_keys"] == ["gtg", "uhs", "xid"]
    assert xsts["expires_in"] > 0
    xsts_body = session.calls[3][1]["json"]
    assert xsts_body["RelyingParty"] == "http://xboxlive.com"
    assert xsts_body["Properties"]["SandboxId"] == "RETAIL"
    assert xsts_body["Properties"]["UserTokens"] == [USER_TOKEN]
    _no_secrets(result)


async def test_refused_scope_stops_after_the_first_step():
    session = FakeSession()
    session.token = FakeResponse(400, {
        "error": "invalid_scope", "error_description": "The provided value for scope is not valid.",
    })
    result = await probe.async_probe_xbox_token(session, STORED)

    assert result["steps"]["msa_refresh"]["error"] == "invalid_scope"
    assert result["steps"]["msa_refresh"]["access_token_received"] is False
    assert list(result["steps"]) == ["msa_refresh"]
    assert len(session.calls) == 1
    _no_secrets(result)


async def test_xsts_refusal_reports_the_xerr():
    session = FakeSession()
    session.xsts = FakeResponse(401, {"XErr": 2148916233, "Message": "", "Redirect": "https://start.ui.xboxlive.com/CreateAccount"})
    result = await probe.async_probe_xbox_token(session, STORED)

    xsts = result["steps"]["xsts_authorize"]
    assert xsts["status"] == 401 and xsts["xerr"] == 2148916233
    assert xsts["display_claims_xui_keys"] == []
    _no_secrets(result)


async def test_network_error_is_reported_by_class():
    session = FakeSession()

    def boom(url, **kwargs):
        raise asyncio.TimeoutError

    session.post = boom
    result = await probe.async_probe_xbox_token(session, STORED)
    assert result["steps"]["msa_refresh"] == {"status": None, "error": "TimeoutError"}


async def test_nothing_secret_is_logged(caplog):
    caplog.set_level("DEBUG")
    await probe.async_probe_xbox_token(FakeSession(), STORED)
    for secret in SECRETS:
        assert secret not in caplog.text


# Coordinator and service ---------------------------------------------------------


def _coordinator(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={"refresh_token": STORED})
    entry.add_to_hass(hass)
    coord = FamilySafetyDataUpdateCoordinator(hass, entry)
    authenticator = SimpleNamespace(
        refresh_token=STORED, _login_lock=asyncio.Lock(), perform_refresh=AsyncMock(),
    )
    coord.api = SimpleNamespace(api=SimpleNamespace(authenticator=authenticator))
    coord._async_persist_current_auth = AsyncMock()
    return coord, entry, authenticator


async def test_refuses_without_confirm_risk(hass):
    coord, _, authenticator = _coordinator(hass)
    with patch.object(probe, "async_probe_xbox_token") as run, pytest.raises(
        ServiceValidationError, match="confirm_risk"
    ):
        await coord.async_probe_xbox_token(False)
    run.assert_not_called()
    authenticator.perform_refresh.assert_not_called()


async def test_stored_token_is_left_and_checked_after(hass):
    coord, entry, authenticator = _coordinator(hass)
    session = FakeSession()
    with patch(
        "custom_components.microsoft_family_safety.coordinator.async_get_clientsession",
        return_value=session,
    ):
        result = await coord.async_probe_xbox_token(True)

    # The Xbox-scope refresh token Microsoft returned went nowhere.
    assert authenticator.refresh_token == STORED
    assert result["stored_refresh_token_unchanged"] is True
    assert result["family_refresh_after"] == {"ok": True}
    authenticator.perform_refresh.assert_awaited_once()
    assert result["entry_id"] == entry.entry_id
    _no_secrets(result)


async def test_a_failed_family_refresh_after_is_reported(hass):
    coord, _, authenticator = _coordinator(hass)
    authenticator.perform_refresh.side_effect = RuntimeError("revoked")
    with patch(
        "custom_components.microsoft_family_safety.coordinator.async_get_clientsession",
        return_value=FakeSession(),
    ):
        result = await coord.async_probe_xbox_token(True)
    assert result["family_refresh_after"] == {"ok": False, "error": "RuntimeError"}


async def test_service_is_for_administrators(hass, hass_admin_user, hass_read_only_user):
    coord, entry, _ = _coordinator(hass)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coord
    _register_services(hass)
    coord.async_probe_xbox_token = AsyncMock(return_value={"steps": {}})

    with pytest.raises(Unauthorized):
        await hass.services.async_call(
            DOMAIN, "probe_xbox_token", {"confirm_risk": True}, blocking=True,
            return_response=True, context=Context(user_id=hass_read_only_user.id),
        )
    response = await hass.services.async_call(
        DOMAIN, "probe_xbox_token", {"confirm_risk": True}, blocking=True,
        return_response=True, context=Context(user_id=hass_admin_user.id),
    )
    assert response == {"steps": {}}
    coord.async_probe_xbox_token.assert_awaited_once_with(True)

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "probe_xbox_token", {"entry_id": "nope", "confirm_risk": True},
            blocking=True, return_response=True,
        )
