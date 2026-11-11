"""No trips to Microsoft's sign-in pages while nothing needs the web session.

With the schedules on the mobile API and Xbox on the Xbox Family Settings
sign-in, the Family web session is only a convenience (website lists). Once it
expires (Microsoft enforces 24 h on some accounts, #52), every request that
lands on login.live.com or login.microsoftonline.com, and above all the silent
"Continue" renewal posted there, counts as a sign-in attempt on the parent's
account and can send them a security code. These tests pin that the client
stops at the first such landing and stays stopped, across restarts, until a
new interactive sign-in.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.microsoft_family_safety.api_client import FamilySafetyWebAPI
from custom_components.microsoft_family_safety.const import (
    CONF_WEB_COOKIES,
    CONF_XBOX_FEATURES,
    DOMAIN,
)
from custom_components.microsoft_family_safety.coordinator import (
    _WEB_PARKED,
    FamilySafetyDataUpdateCoordinator,
)

LOGIN_PAGE = "https://login.live.com/oauth20_authorize.srf?client_id=x"
CONTINUE_FORM = (
    '<html><body><form name="fmHF" id="fmHF" action="https://account.microsoft.com/auth/complete-signin" '
    'method="post"><input type="hidden" name="t" value="x"></form></body></html>'
)


class _Landing:
    def __init__(self, url: str, body: str) -> None:
        self.status = 200
        self.url = url
        self.headers: dict[str, str] = {}
        self._body = body

    async def text(self) -> str:
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> bool:
        return False


class _LoginSession:
    """Every page lands on login.live.com's silent "Continue" form."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.cookie_jar = MagicMock()

    def get(self, url, **kwargs):
        self.calls.append(("GET", str(url)))
        return _Landing(LOGIN_PAGE, CONTINUE_FORM)

    def post(self, url, **kwargs):
        self.calls.append(("POST", str(url)))
        return _Landing(LOGIN_PAGE, CONTINUE_FORM)

    def request(self, method, url, **kwargs):
        self.calls.append((method, str(url)))
        return _Landing(LOGIN_PAGE, CONTINUE_FORM)


def _expired_api(*, login_allowed: bool) -> tuple[FamilySafetyWebAPI, _LoginSession]:
    api = FamilySafetyWebAPI(authenticator=object())
    api._web_cookies = [{"name": "MSPAuth", "value": "x", "domain": ".live.com", "path": "/"}]
    api.family_context_state = "unknown"
    api.sync_web_cookies_from_session = lambda: False
    session = _LoginSession()
    api._get_web_session = lambda: session
    api.login_pages_allowed = login_allowed
    return api, session


async def test_without_need_the_renewal_form_is_never_posted() -> None:
    api, session = _expired_api(login_allowed=False)

    await api._warm_family_context()

    assert not [c for c in session.calls if c[0] == "POST"]
    assert api.family_context_state == "auth_required"
    assert api.web_parked is True


async def test_once_parked_nothing_goes_to_microsoft_again() -> None:
    api, session = _expired_api(login_allowed=False)
    await api._warm_family_context()
    calls = len(session.calls)

    api._family_auth_required_until = 0.0  # well past the short memo
    await api._warm_family_context()
    await api.async_check_web_session()

    assert len(session.calls) == calls
    assert api.family_context_state in ("auth_required", "expired")
    assert api.web_session_state == "expired"


async def test_the_account_probe_parks_too() -> None:
    api, session = _expired_api(login_allowed=False)

    assert await api.async_check_web_session() is False

    assert not [c for c in session.calls if c[0] == "POST"]
    assert api.web_parked is True


async def test_with_need_the_renewal_is_still_tried() -> None:
    # Xbox without the Xbox Family Settings link still depends on the session.
    api, session = _expired_api(login_allowed=True)
    api._async_renew_account_session = AsyncMock(return_value=False)

    await api._warm_family_context()

    api._async_renew_account_session.assert_awaited()
    assert api.web_parked is False


def _coordinator(hass, *, xbox: bool = True) -> FamilySafetyDataUpdateCoordinator:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"refresh_token": "x", CONF_WEB_COOKIES: [{"name": "MSPAuth"}]},
        options={CONF_XBOX_FEATURES: xbox},
    )
    entry.add_to_hass(hass)
    return FamilySafetyDataUpdateCoordinator(hass, entry)


async def test_xbox_on_xapprove_allows_no_sign_in_pages(hass) -> None:
    coord = _coordinator(hass)
    coord.xbox_family = SimpleNamespace(usable=True, linked=True)
    coord.web_api = SimpleNamespace(mobile_schedule_ok=True)

    for polls in (1, 2, 5):
        coord._polls_started = polls
        assert coord._login_pages_allowed() is False


async def test_a_restart_alone_never_allows_sign_in_pages(hass) -> None:
    # The first poll has not reached the mobile schedules yet.
    coord = _coordinator(hass)
    coord.xbox_family = SimpleNamespace(usable=True, linked=True)
    coord.web_api = SimpleNamespace(mobile_schedule_ok=False)
    coord._polls_started = 1

    assert coord._login_pages_allowed() is False


async def test_xbox_without_the_link_still_allows_them(hass) -> None:
    coord = _coordinator(hass)
    coord.xbox_family = SimpleNamespace(usable=False, linked=False)
    coord.web_api = SimpleNamespace(mobile_schedule_ok=True)
    coord._polls_started = 1

    assert coord._login_pages_allowed() is True


async def test_the_parked_state_survives_a_restart(hass) -> None:
    coord = _coordinator(hass)
    api, _session = _expired_api(login_allowed=False)
    api.web_parked = True
    coord.web_api = api
    coord._runtime_auth_state = {}

    await coord._async_persist_runtime_auth(web_cookies=api._web_cookies)
    assert coord._runtime_auth_state[_WEB_PARKED] is True

    # A new coordinator after the restart, same runtime state.
    restarted = _coordinator(hass)
    restarted._runtime_auth_state = dict(coord._runtime_auth_state)
    fresh, _ = _expired_api(login_allowed=False)
    restarted.web_api = fresh
    await restarted._async_load_web_cookies()

    assert fresh.web_parked is True
