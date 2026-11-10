"""The Xbox Family Settings link captures the OAuth code via the native proxy.

The options flow opens the Microsoft sign-in through the integration's local
proxy, which grabs the oauth20_desktop.srf code automatically, so the user never
copies a URL. A missing HTTPS URL or a failed sign-in falls back to the manual
paste step.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.data_entry_flow import FlowResultType, UnknownFlow
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.microsoft_family_safety.config_flow import (
    FamilySafetyOptionsFlow,
    _options_browser_hass_url,
)
from custom_components.microsoft_family_safety.const import DOMAIN
from custom_components.microsoft_family_safety.auth.native_proxy import (
    _is_native_options_flow,
)
from custom_components.microsoft_family_safety.xbox_family_api import XboxFamilyError

CODE_URL = "https://login.live.com/oauth20_desktop.srf?code=abc123"


def _flow(hass) -> FamilySafetyOptionsFlow:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"refresh_token": "x", "web_cookies": [{"name": "c", "value": "v"}]},
        options={"platforms": ["Windows", "Xbox"], "xbox_features": True},
    )
    entry.add_to_hass(hass)
    flow = FamilySafetyOptionsFlow(entry)
    flow.hass = hass
    flow.handler = entry.entry_id
    flow._pending_options = {"xbox_features": True}
    return flow


def test_options_browser_hass_url_requires_https(hass) -> None:
    with patch(
        "custom_components.microsoft_family_safety.config_flow.get_url",
        return_value="https://ha.example",
    ):
        assert _options_browser_hass_url(hass) == "https://ha.example"
    with patch(
        "custom_components.microsoft_family_safety.config_flow.get_url",
        return_value="http://10.0.0.2:8123",
    ):
        assert _options_browser_hass_url(hass) is None


def test_is_native_options_flow() -> None:
    hass = MagicMock()
    hass.config_entries.flow.async_get.side_effect = UnknownFlow("x")
    hass.config_entries.options.async_get.return_value = {"handler": "entry"}
    assert _is_native_options_flow(hass, "fid") is True

    hass2 = MagicMock()
    hass2.config_entries.flow.async_get.return_value = {"handler": DOMAIN}
    assert _is_native_options_flow(hass2, "fid") is False

    hass3 = MagicMock()
    hass3.config_entries.flow.async_get.side_effect = UnknownFlow("x")
    hass3.config_entries.options.async_get.side_effect = UnknownFlow("x")
    assert _is_native_options_flow(hass3, "fid") is False


async def test_finish_links_from_the_captured_code(hass) -> None:
    flow = _flow(hass)
    flow._xbox_proxy = SimpleNamespace(
        oauth_redirect_url=CODE_URL, complete=True, token="t", access_path="/p"
    )
    flow._xbox_verifier = "verifier"
    with patch(
        "custom_components.microsoft_family_safety.auth.native_proxy.unregister_native_proxy",
        new=AsyncMock(),
    ), patch(
        "custom_components.microsoft_family_safety.xbox_family_api.XboxFamilyAPI.async_link_from_redirect",
        new=AsyncMock(),
    ) as link:
        result = await flow.async_step_finish_xbox_proxy()

    link.assert_awaited_once()
    assert link.await_args.args[0] == CODE_URL
    assert link.await_args.args[2] == "proxy"
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert flow._xbox_proxy is None


async def test_finish_falls_back_to_manual_without_a_code(hass) -> None:
    flow = _flow(hass)
    flow._xbox_proxy = SimpleNamespace(oauth_redirect_url=None, complete=False)
    flow._xbox_verifier = "verifier"
    with patch(
        "custom_components.microsoft_family_safety.auth.native_proxy.unregister_native_proxy",
        new=AsyncMock(),
    ):
        result = await flow.async_step_finish_xbox_proxy()

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "xbox_link_manual"


async def test_finish_falls_back_to_manual_on_link_error(hass) -> None:
    flow = _flow(hass)
    flow._xbox_proxy = SimpleNamespace(
        oauth_redirect_url=CODE_URL, complete=True, token="t", access_path="/p"
    )
    flow._xbox_verifier = "verifier"
    with patch(
        "custom_components.microsoft_family_safety.auth.native_proxy.unregister_native_proxy",
        new=AsyncMock(),
    ), patch(
        "custom_components.microsoft_family_safety.xbox_family_api.XboxFamilyAPI.async_link_from_redirect",
        new=AsyncMock(side_effect=XboxFamilyError("nope")),
    ):
        result = await flow.async_step_finish_xbox_proxy()

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "xbox_link_manual"


async def test_reentry_advances_to_finish_when_captured(hass) -> None:
    flow = _flow(hass)
    flow._xbox_link_task = object()  # non-None marks the re-entry after the wait
    flow._xbox_proxy = SimpleNamespace(complete=True, oauth_redirect_url=CODE_URL)

    result = await flow.async_step_xbox_link()

    assert result["type"] == FlowResultType.SHOW_PROGRESS_DONE
    assert result["step_id"] == "finish_xbox_proxy"


async def test_reentry_falls_back_to_manual_when_not_captured(hass) -> None:
    flow = _flow(hass)
    flow._xbox_link_task = object()
    flow._xbox_proxy = SimpleNamespace(complete=False, oauth_redirect_url=None)

    result = await flow.async_step_xbox_link()

    assert result["type"] == FlowResultType.SHOW_PROGRESS_DONE
    assert result["step_id"] == "xbox_link_manual"


async def test_no_https_url_uses_manual_paste(hass) -> None:
    flow = _flow(hass)
    with patch(
        "custom_components.microsoft_family_safety.config_flow._options_browser_hass_url",
        return_value=None,
    ):
        result = await flow.async_step_xbox_link()

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "xbox_link_manual"
