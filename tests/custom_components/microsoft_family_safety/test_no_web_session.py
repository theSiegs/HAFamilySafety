"""The integration runs on the mobile token and the Xbox Family Settings link only.

Microsoft asks for an interactive sign-in every 24 h on the Family web session
(#52), and the integration's own visits to account.microsoft.com and to the
Microsoft sign-in pages are what sent the parent security codes. These tests
pin that a poll never goes there, even for an entry that still carries the web
session of an earlier version, and that such an entry upgrades without a new
sign-in.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import httpx
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from yarl import URL

from custom_components.microsoft_family_safety import coordinator as coordinator_module
from custom_components.microsoft_family_safety.api_client import FamilySafetyWebAPI
from custom_components.microsoft_family_safety.config_flow import FamilySafetyConfigFlow
from custom_components.microsoft_family_safety.const import (
    CONF_XBOX_FEATURES,
    DOMAIN,
    LEGACY_WEB_AUTH_KEYS,
)
from custom_components.microsoft_family_safety.coordinator import (
    FamilySafetyDataUpdateCoordinator,
)

CHILD = 1234567890123456
EXPIRED_COOKIES = [
    {"name": "AMCSecAuth", "value": "expired", "domain": "account.microsoft.com", "path": "/"},
    {"name": "MSPAuth", "value": "expired", "domain": ".login.live.com", "path": "/"},
]
#: Hosts of the Family web session and of Microsoft's sign-in pages. Only the
#: OAuth token endpoint (refresh-token grants) may be reached on login.live.com.
WEB_HOSTS = {
    "account.microsoft.com",
    "account.live.com",
    "login.live.com",
    "login.microsoft.com",
    "login.microsoftonline.com",
}
SCHEDULES = {
    "mode": "PerDeviceType",
    "schedules": [{
        "appliesTo": "Windows",
        "enabled": True,
        "dailyRestrictions": {
            day: {"allowance": 7_200_000, "allottedIntervals": [{"begin": "07:00:00", "end": "22:00:00"}]}
            for day in ("sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday")
        },
    }],
}


def _account() -> SimpleNamespace:
    return SimpleNamespace(
        user_id=CHILD, first_name="Alex", surname="Rivera", profile_picture=None,
        today_screentime_usage=0, average_screentime_usage=0, account_balance=0,
        account_currency="USD", blocked_platforms=[], roster_errors={}, devices=[],
        applications=[],
    )


@pytest.fixture
def network():
    """Record every HTTP request leaving the integration outside the mobile client."""
    sent: list[URL] = []

    async def aiohttp_request(self, method, url, *args, **kwargs):
        sent.append(URL(str(url)))
        raise aiohttp.ClientConnectionError("offline in tests")

    async def httpx_send(self, request, *args, **kwargs):
        sent.append(URL(str(request.url)))
        raise httpx.ConnectError("offline in tests", request=request)

    async def mobile_request(method, path, json_data=None, params=None, extra_headers=None):
        if path.startswith("/v4/devicelimits/schedules/"):
            return SCHEDULES
        if path.startswith("/v1/WebRestrictions/"):
            return {"enabled": True, "exceptions": [{"website": "https://example.org", "allowed": False}]}
        return {}

    with (
        patch.object(aiohttp.ClientSession, "_request", aiohttp_request),
        patch.object(httpx.AsyncClient, "send", httpx_send),
        patch.object(FamilySafetyWebAPI, "_request", AsyncMock(side_effect=mobile_request)),
    ):
        yield sent


def _web_requests(sent: list[URL]) -> list[URL]:
    return [
        url for url in sent
        if (url.host or "") in WEB_HOSTS and url.path != "/oauth20_token.srf"
    ]


@pytest.fixture
def legacy_entry(hass) -> MockConfigEntry:
    """An entry saved by a version that kept the Family web session."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "refresh_token": "signed-in",
            "auth_user_id": "parent",
            "web_cookies": EXPIRED_COOKIES,
            "web_family_token": "stale-antiforgery",
            "web_family_referer": "https://account.microsoft.com/family/home",
            "auth_url": "http://addon:8098",
        },
        options={CONF_XBOX_FEATURES: True, "platforms": ["Windows", "Xbox"], "api_key": "k"},
    )
    entry.add_to_hass(hass)
    return entry


def _fake_family_safety(refresh_token: str) -> SimpleNamespace:
    authenticator = SimpleNamespace(refresh_token=refresh_token)
    return SimpleNamespace(
        api=SimpleNamespace(authenticator=authenticator),
        accounts=[_account()],
        update=AsyncMock(),
        pending_requests=[],
    )


async def test_poll_with_an_expired_web_session_makes_no_web_request(
    hass, hass_storage, legacy_entry, network
):
    coordinator = FamilySafetyDataUpdateCoordinator(hass, legacy_entry)
    # The rotated mobile token of the earlier version, next to its web cookies.
    hass_storage[f"{DOMAIN}.auth.{legacy_entry.entry_id}"] = {
        "version": 1,
        "key": f"{DOMAIN}.auth.{legacy_entry.entry_id}",
        "data": {
            "anchor": coordinator._legacy_entry_auth_anchor(),
            "refresh_token": "rotated",
            "web_cookies": EXPIRED_COOKIES,
            "web_family_token": "stale-antiforgery",
            "web_family_token_rejected": True,
        },
    }
    create = AsyncMock(return_value=_fake_family_safety("rotated"))
    with patch.object(coordinator_module.FamilySafety, "create", create):
        await coordinator.async_load_saved_screentime()
        data = await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert _web_requests(network) == []
    assert network == []
    # The rotated token of the earlier version is the one in use.
    assert create.await_args.kwargs["token"] == "rotated"
    # Schedules and website lists came from the mobile API.
    account = data["accounts"][CHILD]
    assert account["screentime_policy"]["dailyRestrictions"]["monday"]["allowance"] == "02:00:00"
    assert account["web_browsing"]["blockedSites"] == ["https://example.org"]
    # No reauthentication for the dropped web session.
    assert coordinator.connection_state()["reauth_requested"] is False
    assert coordinator.connection_state()["state"] == "connected"
    assert hass.config_entries.flow.async_progress_by_handler(DOMAIN) == []


async def test_upgrade_drops_the_stored_web_session(hass, hass_storage, legacy_entry, network):
    coordinator = FamilySafetyDataUpdateCoordinator(hass, legacy_entry)
    key = f"{DOMAIN}.auth.{legacy_entry.entry_id}"
    hass_storage[key] = {
        "version": 1,
        "key": key,
        "data": {
            "anchor": coordinator._legacy_entry_auth_anchor(),
            "refresh_token": "rotated",
            "web_cookies": EXPIRED_COOKIES,
        },
    }
    await coordinator.async_load_saved_screentime()
    await hass.async_block_till_done()

    assert not set(LEGACY_WEB_AUTH_KEYS) & set(legacy_entry.data)
    assert not set(LEGACY_WEB_AUTH_KEYS) & set(legacy_entry.options)
    assert legacy_entry.data["refresh_token"] == "signed-in"
    stored = hass_storage[key]["data"]
    assert stored["refresh_token"] == "rotated"
    assert "web_cookies" not in stored
    assert stored["anchor"] == coordinator._entry_auth_anchor()
    # A restart after the upgrade still finds the rotated token.
    again = FamilySafetyDataUpdateCoordinator(hass, legacy_entry)
    await again.async_load_saved_screentime()
    assert again._runtime_refresh_token() == "rotated"


async def test_new_sign_in_supersedes_the_rotated_token(hass, hass_storage):
    entry = MockConfigEntry(
        domain=DOMAIN, data={"refresh_token": "same", "signed_in_at": "2026-11-01T00:00:00"}
    )
    entry.add_to_hass(hass)
    coordinator = FamilySafetyDataUpdateCoordinator(hass, entry)
    key = f"{DOMAIN}.auth.{entry.entry_id}"
    hass_storage[key] = {
        "version": 1, "key": key,
        "data": {"anchor": coordinator._entry_auth_anchor(), "refresh_token": "rotated"},
    }
    # Microsoft may return the same refresh-token string for a new sign-in.
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "signed_in_at": "2026-11-10T00:00:00"}
    )
    await coordinator.async_load_saved_screentime()
    assert coordinator._runtime_refresh_token() == "same"


async def test_xbox_token_from_the_sign_in_is_linked_once(hass, hass_storage, network):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"refresh_token": "rt", "xbox_refresh_token": "xbox-rt"},
        options={CONF_XBOX_FEATURES: True},
    )
    entry.add_to_hass(hass)
    coordinator = FamilySafetyDataUpdateCoordinator(hass, entry)
    with patch.object(
        coordinator_module.FamilySafety, "create", AsyncMock(return_value=_fake_family_safety("rt"))
    ):
        await coordinator._async_setup_api()
    await hass.async_block_till_done()

    assert coordinator.xbox_family.linked
    assert coordinator.xbox_family.linked_via == "sign_in"
    assert hass_storage[f"{DOMAIN}.xbox.{entry.entry_id}"]["data"]["refresh_token"] == "xbox-rt"
    assert "xbox_refresh_token" not in entry.data


def _finished_proxy() -> MagicMock:
    return MagicMock(
        complete=True,
        oauth_redirect_url="https://login.live.com/oauth20_desktop.srf?code=mobile",
        export_cookies=MagicMock(return_value=EXPIRED_COOKIES),
    )


@pytest.mark.parametrize("xbox", [True, False])
async def test_sign_in_stores_tokens_only(hass, xbox):
    flow = FamilySafetyConfigFlow()
    flow.hass = hass
    flow.handler = DOMAIN
    flow.flow_id = "flow"
    flow.context = {"source": "user"}
    flow._pending_options = {CONF_XBOX_FEATURES: xbox}
    flow._mobile_proxy = _finished_proxy()
    fetch = AsyncMock(return_value="https://login.live.com/oauth20_desktop.srf?code=xbox")
    with (
        patch(
            "custom_components.microsoft_family_safety.config_flow.validate_redirect_url",
            AsyncMock(return_value={"title": "Family", "refresh_token": "rt", "user_id": "parent"}),
        ),
        patch(
            "custom_components.microsoft_family_safety.auth.native_proxy.unregister_native_proxy",
            AsyncMock(),
        ),
        patch(
            "custom_components.microsoft_family_safety.auth.native_proxy.async_fetch_oauth_redirect",
            fetch,
        ),
        patch(
            "custom_components.microsoft_family_safety.xbox_family_api.XboxFamilyAPI.async_exchange_redirect",
            AsyncMock(return_value={"refresh_token": "xbox-rt", "access_token": "a"}),
        ),
    ):
        result = await flow.async_step_finish_mobile_proxy()

    assert result["type"] == "create_entry"
    data = result["data"]
    assert data["refresh_token"] == "rt"
    assert data["signed_in_at"]
    assert not set(LEGACY_WEB_AUTH_KEYS) & set(data)
    if xbox:
        # The sign-in cookies were used once, for the Xbox Family Settings code.
        assert data["xbox_refresh_token"] == "xbox-rt"
        assert URL(fetch.await_args.args[1]).query["client_id"] == "00000000482C8F49"
    else:
        assert "xbox_refresh_token" not in data
        fetch.assert_not_called()
