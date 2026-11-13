"""The integration runs on the mobile token and the Xbox Family Settings link only.

Microsoft asks for an interactive sign-in every 24 h on the Family web session
(#52), and the integration's own visits to account.microsoft.com and to the
Microsoft sign-in pages are what sent the parent security codes. These tests
pin that nothing at runtime goes there, even for an entry that still carries
the web session of an earlier version, that such an entry upgrades without a
new sign-in, and that the sign-in itself stores tokens only.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import httpx
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from yarl import URL

from homeassistant.components import persistent_notification
from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_USER
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component

from custom_components.microsoft_family_safety import coordinator as coordinator_module
from custom_components.microsoft_family_safety.config_flow import (
    FamilySafetyConfigFlow,
    NativeAuthHttpsRequired,
)
from custom_components.microsoft_family_safety.const import (
    CONF_XBOX_FEATURES,
    DAY_KEYS,
    DOMAIN,
    LEGACY_WEB_AUTH_KEYS,
)
from custom_components.microsoft_family_safety.coordinator import (
    FamilySafetyDataUpdateCoordinator,
)
from custom_components.microsoft_family_safety.xbox_family_api import XboxFamilyAuthError

CHILD = 1234567890123456
XUID = "2535400000000001"
PARENT_XUID = "2535400000000002"
EXPIRED_COOKIES = [
    {"name": "AMCSecAuth", "value": "expired", "domain": "account.microsoft.com", "path": "/"},
    {"name": "MSPAuth", "value": "expired", "domain": ".login.live.com", "path": "/"},
]
MOBILE_SCHEDULES = {
    "mode": "PerDeviceType",
    "schedules": [
        {
            "appliesTo": platform,
            "enabled": True,
            "dailyRestrictions": {
                day: {
                    "allowance": 7_200_000,
                    "allottedIntervals": [{"begin": "07:00:00", "end": "22:00:00"}],
                }
                for day in DAY_KEYS
            },
        }
        for platform in ("Windows", "Xbox")
    ],
}
XBOX_SCHEDULE = {
    "isEnabled": True,
    "dailyRestrictions": [
        {
            "dayOfWeek": index,
            "allowance": {"hour": 1, "minute": 0},
            "allowedIntervals": [
                {"begin": {"days": 0, "hour": 7, "minute": 0},
                 "end": {"days": 0, "hour": 22, "minute": 0}}
            ],
        }
        for index in range(7)
    ],
}
SIGN_IN_PATCH = "custom_components.microsoft_family_safety.config_flow.validate_redirect_url"
SETUP_PATCH = "custom_components.microsoft_family_safety.async_setup_entry"


# --- transport-level guard -------------------------------------------------------
class _Response:
    def __init__(self, status: int, body: object = None) -> None:
        self.status = status
        self._text = "" if body is None else json.dumps(body)
        self.headers: dict[str, str] = {}

    async def text(self) -> str:
        return self._text

    async def json(self, content_type=None):
        return json.loads(self._text) if self._text else None

    def release(self) -> None:
        return None

    async def wait_for_close(self) -> None:
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args) -> None:
        return None


#: The Windows overrides on the mobile API; a POST replaces them.
_WINDOWS_OVERRIDES: list[dict] = []


def _answer(method: str, url: URL, body: object) -> _Response | None:
    """Microsoft as the mobile client and the Xbox Family Settings client see it."""
    host, path = url.host or "", url.path
    if host == "mobileaggregator.family.microsoft.com":
        if path.startswith("/api/v4/devicelimits/") and path.endswith("/overrides"):
            if method == "POST" and isinstance(body, dict):
                _WINDOWS_OVERRIDES[:] = (
                    [] if body.get("overrideType") == "Cancel"
                    else [{"overrideType": body.get("overrideType"),
                           "validUntil": body.get("validUntil")}]
                )
            return _Response(200, {"lockablePlatforms": [
                {"appliesTo": "Desktop", "overrides": list(_WINDOWS_OVERRIDES), "devices": []},
            ]})
        if method == "GET" and path.startswith("/api/v4/devicelimits/schedules/"):
            return _Response(200, MOBILE_SCHEDULES)
        if method == "GET" and path.startswith("/api/v1/WebRestrictions/"):
            return _Response(200, {
                "enabled": True,
                "exceptions": [{"website": "https://example.org", "allowed": False}],
            })
        if method == "GET" and path.startswith("/api/v2/AppLimits/settings/"):
            return _Response(200, {"enabled": True})
        if method == "GET":
            return _Response(200, {})
        return _Response(204)
    if host == "login.live.com" and path == "/oauth20_token.srf":
        return _Response(200, {"access_token": "a", "refresh_token": "xbox-rt", "expires_in": 86400})
    if host == "user.auth.xboxlive.com":
        return _Response(200, {"Token": "user", "NotAfter": "2099-01-01T00:00:00Z"})
    if host == "xsts.auth.xboxlive.com":
        return _Response(200, {
            "Token": "xsts", "NotAfter": "2099-01-01T00:00:00Z",
            "DisplayClaims": {"xui": [{"uhs": "1", "xid": PARENT_XUID}]},
        })
    if host == "accounts.xboxlive.com":
        return _Response(200, {"familyUsers": [
            {"firstName": "Alex", "lastName": "Rivera", "xuid": XUID, "role": "Child"},
        ]})
    if host == "xapprove.xboxlive.com":
        if path.endswith("/overrides/create"):
            blocked = isinstance(body, dict) and body.get("overrideType") == 2
            return _Response(200, {"overrides": [
                {"overrideType": 2, "validUntil": "2099-01-01T00:00:00Z"}
            ] if blocked else []})
        if path.endswith("/overrides"):
            return _Response(200, {"overrides": []})
        if path.endswith("/screenTimeUsage"):
            return _Response(200, {"dailyScreenTimeEvents": []})
        if path.endswith("/settings/screentime"):
            return _Response(200, XBOX_SCHEDULE if method == "GET" else None)
    return None


@pytest.fixture
def transport():
    """Answer the token APIs, refuse and record everything else.

    The integration's own clients run unpatched on top of this: only hosts
    that a refresh token reaches are answered. ``transport.refused`` lists
    every other request (account.microsoft.com, sign-in pages...).
    """
    seen: list[tuple[str, URL]] = []
    refused: list[tuple[str, URL]] = []
    _WINDOWS_OVERRIDES.clear()

    async def aiohttp_request(self, method, url, *args, **kwargs):
        target = URL(str(url))
        seen.append((method.upper(), target))
        response = _answer(method.upper(), target, kwargs.get("json"))
        if response is None:
            refused.append((method.upper(), target))
            raise aiohttp.ClientConnectionError("not a token API")
        return response

    async def httpx_send(self, request, *args, **kwargs):
        refused.append((request.method, URL(str(request.url))))
        raise httpx.ConnectError("no httpx at runtime", request=request)

    with (
        patch.object(aiohttp.ClientSession, "_request", aiohttp_request),
        patch.object(httpx.AsyncClient, "send", httpx_send),
    ):
        yield SimpleNamespace(seen=seen, refused=refused)


def _account() -> SimpleNamespace:
    return SimpleNamespace(
        user_id=CHILD, first_name="Alex", surname="Rivera", profile_picture=None,
        today_screentime_usage=0, average_screentime_usage=0, account_balance=0,
        account_currency="USD", blocked_platforms=[], roster_errors={}, devices=[],
        applications=[], override_device=AsyncMock(),
    )


def _fake_family_safety(refresh_token: str) -> SimpleNamespace:
    """pyfamilysafety with a valid access token (its own requests are its business)."""
    authenticator = SimpleNamespace(
        refresh_token=refresh_token,
        access_token="access",
        expires=datetime.now() + timedelta(hours=1),
    )
    return SimpleNamespace(
        api=SimpleNamespace(authenticator=authenticator),
        accounts=[_account()],
        update=AsyncMock(),
        pending_requests=[],
    )


@pytest.fixture
def legacy_entry(hass) -> MockConfigEntry:
    """An entry saved by a version that kept the Family web session."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="parent",
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


def _store(hass_storage, key: str, data: dict) -> None:
    hass_storage[key] = {"version": 1, "key": key, "data": data}


# --- runtime ---------------------------------------------------------------------
async def test_poll_with_an_expired_web_session_stays_on_the_token_apis(
    hass, hass_storage, legacy_entry, transport
):
    coordinator = FamilySafetyDataUpdateCoordinator(hass, legacy_entry)
    # The rotated mobile token of the earlier version, next to its web cookies.
    _store(hass_storage, f"{DOMAIN}.auth.{legacy_entry.entry_id}", {
        "anchor": coordinator._legacy_entry_auth_anchor(),
        "refresh_token": "rotated",
        "web_cookies": EXPIRED_COOKIES,
        "web_family_token": "stale-antiforgery",
        "web_family_token_rejected": True,
    })
    create = AsyncMock(return_value=_fake_family_safety("rotated"))
    with patch.object(coordinator_module.FamilySafety, "create", create):
        await coordinator.async_load_saved_screentime()
        data = await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert transport.refused == []
    assert {url.host for _, url in transport.seen} == {"mobileaggregator.family.microsoft.com"}
    # The rotated token of the earlier version is the one in use.
    assert create.await_args.kwargs["token"] == "rotated"
    account = data["accounts"][CHILD]
    assert account["screentime_policy"]["dailyRestrictions"]["monday"]["allowance"] == "02:00:00"
    assert account["web_browsing"]["blockedSites"] == ["https://example.org"]
    # No reauthentication for the dropped web session...
    state = coordinator.connection_state()
    assert state["reauth_requested"] is False
    assert hass.config_entries.flow.async_progress_by_handler(DOMAIN) == []
    # ...but the missing Xbox link is visible and gets a repair issue.
    assert state["xbox_family"] == "not_linked"
    assert state["state"] == "degraded"
    issue = ir.async_get(hass).async_get_issue(DOMAIN, coordinator.xbox_issue_id)
    assert issue is not None and issue.translation_key == "xbox_not_linked"


async def test_linked_poll_and_every_action_stay_on_the_token_apis(
    hass, hass_storage, transport
):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"refresh_token": "rt", "signed_in_at": "2026-11-01T00:00:00"},
        options={CONF_XBOX_FEATURES: True, "platforms": ["Windows", "Xbox"]},
    )
    entry.add_to_hass(hass)
    _store(hass_storage, f"{DOMAIN}.xbox.{entry.entry_id}", {"refresh_token": "xbox-rt"})
    coordinator = FamilySafetyDataUpdateCoordinator(hass, entry)
    coordinator.async_request_refresh = AsyncMock()
    with patch.object(
        coordinator_module.FamilySafety, "create", AsyncMock(return_value=_fake_family_safety("rt"))
    ):
        await coordinator.async_load_saved_screentime()
        coordinator.data = await coordinator._async_update_data()

    account = coordinator.data["accounts"][CHILD]
    assert account["xbox_source"] == "xapprove"
    assert account["xbox_lock_known"] is True
    assert ir.async_get(hass).async_get_issue(DOMAIN, coordinator.xbox_issue_id) is None

    await coordinator.async_set_screentime_limit(CHILD, 1, 1, 30)
    await coordinator.async_set_screentime_intervals(CHILD, 1, 8, 0, 20, 0)
    await coordinator.async_lock_account(CHILD)
    await coordinator.async_unlock_account(CHILD)
    await coordinator.async_set_policy_enabled(CHILD, False)
    await coordinator.async_lock_platform(CHILD, "Windows")
    await coordinator.async_unlock_platform(CHILD, "Windows")
    await coordinator.async_lock_platform(CHILD, "Xbox")
    await coordinator.async_unlock_platform(CHILD, "Xbox")
    await coordinator.async_set_xbox_screentime_limit(CHILD, 2, 0, 45)
    await coordinator.async_set_xbox_schedule_enabled(CHILD, True)
    await coordinator.async_set_app_limits_enabled(CHILD, True)
    await coordinator.async_set_app_time_limit(CHILD, "win32:app", "App", "windows", "01:00:00")
    await coordinator.async_remove_app_time_limit(CHILD, "win32:app", "App", "windows")
    await coordinator.async_block_website(CHILD, "example.org")
    await coordinator.async_allow_website(CHILD, "example.com")
    await coordinator.async_remove_website(CHILD, "example.org")
    await coordinator.async_toggle_web_filter(CHILD, True)
    await coordinator.async_set_age_rating(CHILD, 12)
    await coordinator.async_set_acquisition_policy(CHILD, True)
    await coordinator.async_cleanup()

    assert transport.refused == []
    hosts = {url.host for _, url in transport.seen}
    assert "account.microsoft.com" not in hosts
    assert {"mobileaggregator.family.microsoft.com", "xapprove.xboxlive.com"} <= hosts
    assert all(
        url.path == "/oauth20_token.srf" for _, url in transport.seen if url.host == "login.live.com"
    )


async def test_refused_xbox_link_asks_for_a_new_link_not_a_sign_in(hass, hass_storage, transport):
    entry = MockConfigEntry(
        domain=DOMAIN, data={"refresh_token": "rt"}, options={CONF_XBOX_FEATURES: True}
    )
    entry.add_to_hass(hass)
    coordinator = FamilySafetyDataUpdateCoordinator(hass, entry)
    coordinator.xbox_family._refresh_token = "refused"
    coordinator.xbox_family.async_get_schedule = AsyncMock(
        side_effect=XboxFamilyAuthError("invalid_grant")
    )
    coordinator.xbox_family.async_child_xuid = AsyncMock(return_value=XUID)
    entry.async_start_reauth = MagicMock()
    account = _account()

    assert await coordinator._async_fetch_xapprove(account, {}) is None
    coordinator.xbox_family.state = "auth_failed"
    coordinator._async_sync_xbox_issue()
    # Retried after the backoff only.
    assert await coordinator._async_fetch_xapprove(account, {}) is None
    assert coordinator.xbox_family.async_get_schedule.await_count == 1

    entry.async_start_reauth.assert_not_called()
    issue = ir.async_get(hass).async_get_issue(DOMAIN, coordinator.xbox_issue_id)
    assert issue is not None and issue.translation_key == "xbox_link_refused"
    coordinator.api = MagicMock()  # the mobile API answers
    assert coordinator.connection_state()["state"] == "degraded"

    # A new link from the options retries at once.
    coordinator.async_request_refresh = AsyncMock()
    coordinator.async_xbox_linked()
    await hass.async_block_till_done()
    await coordinator._async_fetch_xapprove(account, {})
    assert coordinator.xbox_family.async_get_schedule.await_count == 2


async def test_first_update_clears_a_stale_auth_notification(hass, transport):
    entry = MockConfigEntry(domain=DOMAIN, data={"refresh_token": "rt"}, options={})
    entry.add_to_hass(hass)
    coordinator = FamilySafetyDataUpdateCoordinator(hass, entry)
    with (
        patch.object(
            coordinator_module.FamilySafety, "create",
            AsyncMock(return_value=_fake_family_safety("rt")),
        ),
        patch.object(coordinator_module.persistent_notification, "async_dismiss") as dismiss,
    ):
        await coordinator._async_update_data()
        await coordinator._async_update_data()
    dismiss.assert_called_once_with(hass, coordinator_module.AUTH_NOTIFICATION_ID)


async def test_manual_reauth_request_is_withdrawn_once_its_flow_is_gone(hass, transport):
    """request_reauth on a working token: cleared by a good poll after the flow ends."""
    await async_setup_component(hass, "persistent_notification", {})
    entry = MockConfigEntry(domain=DOMAIN, data={"refresh_token": "rt"}, options={})
    entry.add_to_hass(hass)
    coordinator = FamilySafetyDataUpdateCoordinator(hass, entry)
    with patch.object(
        coordinator_module.FamilySafety, "create",
        AsyncMock(return_value=_fake_family_safety("rt")),
    ):
        await coordinator._async_update_data()
        await coordinator.async_request_reauth()
        await hass.async_block_till_done()
        flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
        assert len(flows) == 1
        notification = coordinator_module.AUTH_NOTIFICATION_ID
        assert notification in persistent_notification._async_get_or_create_notifications(hass)

        # While the sign-in is open, a good poll keeps the request.
        await coordinator._async_update_data()
        assert coordinator.connection_state()["reauth_requested"] is True
        assert notification in persistent_notification._async_get_or_create_notifications(hass)

        hass.config_entries.flow.async_abort(flows[0]["flow_id"])
        await coordinator._async_update_data()
    assert coordinator.connection_state()["reauth_requested"] is False
    assert notification not in persistent_notification._async_get_or_create_notifications(hass)


# --- upgrade ---------------------------------------------------------------------
async def test_upgrade_drops_the_stored_web_session(hass, hass_storage, legacy_entry):
    coordinator = FamilySafetyDataUpdateCoordinator(hass, legacy_entry)
    key = f"{DOMAIN}.auth.{legacy_entry.entry_id}"
    _store(hass_storage, key, {
        "anchor": coordinator._legacy_entry_auth_anchor(),
        "refresh_token": "rotated",
        "web_cookies": EXPIRED_COOKIES,
    })
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


async def test_superseded_store_with_cookies_is_removed(hass, hass_storage):
    entry = MockConfigEntry(
        domain=DOMAIN, data={"refresh_token": "new", "signed_in_at": "2026-11-10T00:00:00"}
    )
    entry.add_to_hass(hass)
    coordinator = FamilySafetyDataUpdateCoordinator(hass, entry)
    key = f"{DOMAIN}.auth.{entry.entry_id}"
    # Written for an older sign-in; with the same token string the legacy
    # anchor would even match, but this entry has signed in since.
    _store(hass_storage, key, {
        "anchor": coordinator._legacy_entry_auth_anchor(),
        "refresh_token": "old",
        "web_cookies": EXPIRED_COOKIES,
    })
    await coordinator.async_load_saved_screentime()
    await hass.async_block_till_done()
    assert coordinator._runtime_refresh_token() == "new"
    assert key not in hass_storage


async def test_new_sign_in_supersedes_the_rotated_token(hass, hass_storage):
    entry = MockConfigEntry(
        domain=DOMAIN, data={"refresh_token": "same", "signed_in_at": "2026-11-01T00:00:00"}
    )
    entry.add_to_hass(hass)
    coordinator = FamilySafetyDataUpdateCoordinator(hass, entry)
    key = f"{DOMAIN}.auth.{entry.entry_id}"
    _store(hass_storage, key, {"anchor": coordinator._entry_auth_anchor(), "refresh_token": "rotated"})
    # Microsoft may return the same refresh-token string for a new sign-in.
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "signed_in_at": "2026-11-10T00:00:00"}
    )
    await coordinator.async_load_saved_screentime()
    assert coordinator._runtime_refresh_token() == "same"


async def test_xbox_token_from_the_sign_in_is_linked_once(hass, hass_storage, transport):
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


# --- sign-in ---------------------------------------------------------------------
def _finished_proxy() -> MagicMock:
    return MagicMock(
        complete=True,
        oauth_redirect_url="https://login.live.com/oauth20_desktop.srf?code=mobile",
        export_cookies=MagicMock(return_value=EXPIRED_COOKIES),
    )


async def _fake_start(self, user_input=None):
    """The browser part: the proxy has captured the mobile OAuth redirect."""
    self._mobile_proxy = _finished_proxy()
    return self.async_external_step(step_id="check_mobile_proxy", url="https://ha.example/x")


@pytest.fixture
def sign_in():
    """Patch the browser and Microsoft out of the sign-in; returns the mocks."""
    fetch = AsyncMock(return_value="https://login.live.com/oauth20_desktop.srf?code=xbox")
    validate = AsyncMock(
        return_value={"title": "Family", "refresh_token": "rt", "user_id": "parent"}
    )
    with (
        patch.object(FamilySafetyConfigFlow, "async_step_start_mobile_auth", _fake_start),
        patch(SIGN_IN_PATCH, validate),
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
        patch(SETUP_PATCH, return_value=True),
    ):
        yield SimpleNamespace(fetch=fetch, validate=validate)


async def _complete(hass, flow_id: str) -> dict:
    """Browser callback, then the frontend until the flow ends."""
    flow = hass.config_entries.flow
    result = await flow.async_configure(flow_id, {"native_callback": True})
    assert result["type"] == "external_done"
    for _ in range(3):
        result = await flow.async_configure(flow_id)
        await hass.async_block_till_done()
        if result["type"] not in ("progress", "progress_done"):
            return result
    return result


@pytest.mark.parametrize("xbox", [True, False])
async def test_sign_in_stores_tokens_only(hass, sign_in, xbox):
    flow = hass.config_entries.flow
    result = await flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await flow.async_configure(
        result["flow_id"],
        {"update_interval": 300, "platforms": ["Windows", "Xbox"] if xbox else ["Windows"]},
    )
    assert result["type"] == "external"
    result = await _complete(hass, result["flow_id"])

    assert result["type"] == "create_entry"
    data = result["data"]
    assert data["refresh_token"] == "rt"
    assert data["signed_in_at"]
    assert not set(LEGACY_WEB_AUTH_KEYS) & set(data)
    if xbox:
        # The sign-in cookies were used once, for the Xbox Family Settings code.
        assert data["xbox_refresh_token"] == "xbox-rt"
        assert URL(sign_in.fetch.await_args.args[1]).query["client_id"] == "00000000482C8F49"
    else:
        assert "xbox_refresh_token" not in data
        sign_in.fetch.assert_not_called()


async def test_concurrent_entries_attach_to_one_token_exchange(hass, sign_in):
    """The frontend and the re-notification helper configure the flow at once."""
    release = asyncio.Event()

    async def slow_validate(hass_, url):
        await release.wait()
        return {"title": "Family", "refresh_token": "rt", "user_id": "parent"}

    sign_in.validate.side_effect = slow_validate
    flow = hass.config_entries.flow
    result = await flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    flow_id = result["flow_id"]
    await flow.async_configure(flow_id, {"update_interval": 300, "platforms": ["Windows"]})
    result = await flow.async_configure(flow_id, {"native_callback": True})
    assert result["type"] == "external_done"

    first, second = await asyncio.gather(
        flow.async_configure(flow_id), flow.async_configure(flow_id)
    )
    assert first["type"] == second["type"] == "progress"
    assert sign_in.validate.await_count == 1

    release.set()
    await hass.async_block_till_done()
    result = await flow.async_configure(flow_id)
    assert result["type"] == "create_entry"
    assert result["data"]["refresh_token"] == "rt"
    assert sign_in.validate.await_count == 1


async def test_reauth_of_a_legacy_entry_keeps_tokens_only(hass, legacy_entry, sign_in):
    flow = hass.config_entries.flow
    result = await flow.async_init(
        DOMAIN,
        context={"source": SOURCE_REAUTH, "entry_id": legacy_entry.entry_id},
        data=dict(legacy_entry.data),
    )
    assert result["step_id"] == "reauth_full_confirm"
    result = await flow.async_configure(result["flow_id"], {})
    result = await _complete(hass, result["flow_id"])

    assert result["type"] == "abort" and result["reason"] == "reauth_successful"
    assert legacy_entry.data["refresh_token"] == "rt"
    assert legacy_entry.data["xbox_refresh_token"] == "xbox-rt"
    assert not set(LEGACY_WEB_AUTH_KEYS) & set(legacy_entry.data)


async def test_reauth_after_failed_setup_keeps_the_stored_xbox_link(
    hass, hass_storage, legacy_entry, sign_in
):
    """No coordinator (setup failed on a refused token): the stored link is read."""
    key = f"{DOMAIN}.xbox.{legacy_entry.entry_id}"
    _store(hass_storage, key, {"refresh_token": "options-link", "linked_via": "options"})
    assert legacy_entry.entry_id not in hass.data.get(DOMAIN, {})
    flow = hass.config_entries.flow
    result = await flow.async_init(
        DOMAIN,
        context={"source": SOURCE_REAUTH, "entry_id": legacy_entry.entry_id},
        data=dict(legacy_entry.data),
    )
    result = await flow.async_configure(result["flow_id"], {})
    result = await _complete(hass, result["flow_id"])

    assert result["type"] == "abort" and result["reason"] == "reauth_successful"
    assert legacy_entry.data["refresh_token"] == "rt"
    sign_in.fetch.assert_not_called()
    assert "xbox_refresh_token" not in legacy_entry.data
    assert hass_storage[key]["data"]["refresh_token"] == "options-link"


@pytest.mark.parametrize(
    ("user_id", "reason", "token"),
    [("parent", "reauth_successful", "manual"), ("someone-else", "wrong_account", "signed-in")],
)
async def test_http_only_reauth_uses_the_manual_step(
    hass, legacy_entry, sign_in, user_id, reason, token
):
    sign_in.validate.return_value = {"title": "Family", "refresh_token": "manual", "user_id": user_id}
    flow = hass.config_entries.flow
    with patch.object(
        FamilySafetyConfigFlow, "async_step_start_mobile_auth",
        AsyncMock(side_effect=NativeAuthHttpsRequired),
    ):
        result = await flow.async_init(
            DOMAIN,
            context={"source": SOURCE_REAUTH, "entry_id": legacy_entry.entry_id},
            data=dict(legacy_entry.data),
        )
        result = await flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "auth"
    result = await flow.async_configure(
        result["flow_id"], {"redirect_url": "https://login.live.com/oauth20_desktop.srf?code=x"}
    )
    assert result["type"] == "abort" and result["reason"] == reason
    assert legacy_entry.data["refresh_token"] == token


async def test_manual_sign_in_of_a_configured_account_updates_it(hass, legacy_entry, sign_in):
    """#50 on the manual path: no duplicate entry for the same account."""
    flow = hass.config_entries.flow
    with patch.object(
        FamilySafetyConfigFlow, "async_step_start_mobile_auth",
        AsyncMock(side_effect=NativeAuthHttpsRequired),
    ):
        result = await flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        result = await flow.async_configure(
            result["flow_id"], {"update_interval": 300, "platforms": ["Windows"]}
        )
    assert result["step_id"] == "auth"
    result = await flow.async_configure(
        result["flow_id"], {"redirect_url": "https://login.live.com/oauth20_desktop.srf?code=x"}
    )
    assert result["type"] == "abort" and result["reason"] == "reauth_successful"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1
    assert legacy_entry.data["refresh_token"] == "rt"
