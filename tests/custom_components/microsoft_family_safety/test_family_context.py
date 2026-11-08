"""The Family web context never asks for a sign-in on the Xbox's behalf.

The Xbox runs on the Xbox Family Settings sign-in (xapprove) and today's
Xbox schedule; neither needs the Family web session. So a Family context that
keeps landing on a login page asks for a sign-in only when the schedules
themselves depend on it (the mobile schedule API not answering), and the
coordinator never warms the Family context for the Xbox.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.microsoft_family_safety.const import (
    CONF_PLATFORMS,
    CONF_XBOX_FEATURES,
    DOMAIN,
)
from custom_components.microsoft_family_safety.coordinator import (
    FamilySafetyDataUpdateCoordinator,
)


def _coordinator(hass, *, web_session: bool, mobile_schedule_ok: bool) -> FamilySafetyDataUpdateCoordinator:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"refresh_token": "x", "web_cookies": [{"name": "c", "value": "v"}]},
        options={CONF_XBOX_FEATURES: web_session, CONF_PLATFORMS: ["Windows"]},
    )
    entry.add_to_hass(hass)
    coord = FamilySafetyDataUpdateCoordinator(hass, entry)
    coord._has_web_credentials = True
    coord.web_api = SimpleNamespace(
        family_context_state="auth_required", mobile_schedule_ok=mobile_schedule_ok
    )
    coord._create_auth_notification = AsyncMock()
    coord._request_reauth = MagicMock()
    return coord


@pytest.mark.parametrize("web_session", [True, False])
async def test_working_mobile_schedules_need_no_sign_in(hass, web_session: bool) -> None:
    coord = _coordinator(hass, web_session=web_session, mobile_schedule_ok=True)

    for _ in range(3):
        await coord._async_track_family_context()

    coord._request_reauth.assert_not_called()


async def test_schedules_on_the_web_session_still_ask_after_a_lasting_refusal(hass) -> None:
    coord = _coordinator(hass, web_session=True, mobile_schedule_ok=False)

    await coord._async_track_family_context()
    coord._request_reauth.assert_not_called()  # one poll may be a hiccup
    await coord._async_track_family_context()

    coord._request_reauth.assert_called_once_with("family_context_auth_required")


def test_the_family_context_is_never_warmed_for_the_xbox() -> None:
    assert not hasattr(FamilySafetyDataUpdateCoordinator, "_async_ensure_family_context")
    assert not hasattr(FamilySafetyDataUpdateCoordinator, "_apply_xbox_lock_state")
