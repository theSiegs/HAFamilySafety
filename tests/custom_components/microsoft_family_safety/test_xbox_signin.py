"""A refused Xbox read is the Family web session needing a sign-in, not a missing Xbox.

The Xbox play-time report and the Xbox policy are the only reads left on the
Family web session. When Microsoft wants its daily interactive sign-in (#52)
they answer 401 while the Family landing page still loads, so the context kept
reading "ready", the Xbox sensors stayed unknown and no sign-in was asked for.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.microsoft_family_safety.api_client import FamilySafetyWebAPI
from custom_components.microsoft_family_safety.const import (
    CONF_PLATFORMS,
    CONF_XBOX_FEATURES,
    DOMAIN,
)
from custom_components.microsoft_family_safety.coordinator import (
    FamilySafetyDataUpdateCoordinator,
)

CHILD = "1234567890123456"


class _Response:
    def __init__(self, status: int) -> None:
        self.status = status
        self.headers: dict[str, str] = {}
        self.url = "https://account.microsoft.com/family/api/xbox/recent-activity/report-v3"

    async def text(self) -> str:
        return '{"statusCode":%d,"message":""}' % self.status

    async def json(self, content_type=None):
        return {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        return None


class _Session:
    def __init__(self, status: int) -> None:
        self.status = status
        self.cookie_jar = MagicMock()
        self.cookie_jar.filter_cookies.return_value = {}
        self.cookie_jar.__iter__ = lambda _self: iter(())

    def request(self, *args, **kwargs) -> _Response:
        return _Response(self.status)


def _api(status: int) -> FamilySafetyWebAPI:
    api = FamilySafetyWebAPI(authenticator=None)
    api._web_cookies = [{"name": "AMCSecAuthJWT", "value": "x"}]
    api._web_csrf = "token"
    api.family_context_state = "ready"
    api.family_token_source = "auth_proxy"
    api.sync_web_cookies_from_session = lambda: False
    api._get_web_session = lambda: _Session(status)
    return api


async def test_a_401_on_the_xbox_report_means_the_family_session_needs_a_sign_in() -> None:
    api = _api(401)

    assert await api.get_xbox_screentime_usage(CHILD, "UTC", "2026-11-07") is None

    assert api.family_context_state == "auth_required"
    assert api._web_csrf is None
    assert api.web_api_state == "auth_error"
    assert not api.has_no_xbox_data(CHILD)


@pytest.mark.parametrize("status", [403, 404])
async def test_a_child_without_an_xbox_leaves_the_session_alone(status: int) -> None:
    api = _api(status)

    assert await api.get_xbox_screentime_usage(CHILD, "UTC", "2026-11-07") is None

    assert api.family_context_state == "ready"
    assert api._web_csrf == "token"
    assert api.has_no_xbox_data(CHILD)


def _coordinator(hass, *, xbox_features: bool) -> FamilySafetyDataUpdateCoordinator:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"refresh_token": "x", "web_cookies": [{"name": "c", "value": "v"}]},
        options={CONF_XBOX_FEATURES: xbox_features, CONF_PLATFORMS: ["Windows", "Xbox"]},
    )
    entry.add_to_hass(hass)
    coord = FamilySafetyDataUpdateCoordinator(hass, entry)
    coord._has_web_credentials = True
    coord.web_api = SimpleNamespace(family_context_state="auth_required", mobile_schedule_ok=True)
    coord._create_auth_notification = AsyncMock()
    coord._request_reauth = MagicMock()
    return coord


async def test_with_the_xbox_features_a_lasting_refusal_asks_for_a_sign_in(hass) -> None:
    coord = _coordinator(hass, xbox_features=True)

    await coord._async_track_family_context()
    coord._request_reauth.assert_not_called()  # one poll may be a hiccup
    await coord._async_track_family_context()

    coord._request_reauth.assert_called_once_with("family_context_auth_required")
    coord._create_auth_notification.assert_awaited_once()


async def test_without_the_xbox_features_the_mobile_schedules_need_no_sign_in(hass) -> None:
    coord = _coordinator(hass, xbox_features=False)
    # A mobile-only entry has no web session to sign in again.
    coord._has_web_credentials = True

    for _ in range(3):
        await coord._async_track_family_context()

    coord._request_reauth.assert_not_called()
