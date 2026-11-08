"""Diagnostics never carry tokens, cookies, names, e-mail addresses or ids."""

from __future__ import annotations

import json

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.microsoft_family_safety.const import DOMAIN
from custom_components.microsoft_family_safety.coordinator import (
    FamilySafetyDataUpdateCoordinator,
)
from custom_components.microsoft_family_safety.diagnostics import (
    async_get_config_entry_diagnostics,
)

CHILD = "1234567890123456"
DEVICE = "abcdef-0123"
SECRETS = [
    "refresh-secret", "cookie-secret", "family-token-secret", "api-key-secret",
    CHILD, DEVICE, "Alex", "Rivera", "alex@example.com", "https://pics.example/alex.png",
    "blocked.example", "Alex's laptop", "parent-user-id",
]


async def test_diagnostics_are_redacted(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "refresh_token": "refresh-secret",
            "web_cookies": [{"name": "MSPAuth", "value": "cookie-secret"}],
            "web_family_token": "family-token-secret",
            "auth_user_id": "parent-user-id",
        },
        options={"api_key": "api-key-secret", "update_interval": 300},
    )
    entry.add_to_hass(hass)
    coord = FamilySafetyDataUpdateCoordinator(hass, entry)
    coord.data = {
        "accounts": {CHILD: {
            "user_id": int(CHILD),
            "first_name": "Alex",
            "surname": "Rivera",
            "profile_picture": "https://pics.example/alex.png",
            "devices": [DEVICE],
            "applications": [{"app_id": "appx:x", "app_name": "X"}],
            "web_browsing": {"enabled": True, "exceptions": [{"website": "https://blocked.example"}]},
            "note": "contact alex@example.com",
        }},
        "devices": {DEVICE: {"device_name": "Alex's laptop", "account_id": CHILD}},
        "pending_requests": [{"puid": CHILD}],
    }
    coord._platform_holds = {f"{CHILD}|xbox": {"account_id": CHILD, "prior": {"type": "none"}}}
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coord

    result = await async_get_config_entry_diagnostics(hass, entry)
    text = json.dumps(result)

    for secret in SECRETS:
        assert secret not in text, secret
    assert "account_1" in result["data"]["accounts"]
    assert result["data"]["accounts"]["account_1"]["application_count"] == 1
    assert "account_1|xbox" in result["platform_holds"]
    assert result["entry"]["options"]["update_interval"] == 300


async def test_diagnostics_show_the_xbox_sign_in_without_secrets(hass, hass_storage):
    entry = MockConfigEntry(
        domain=DOMAIN, data={"refresh_token": "refresh-secret"},
        options={"xbox_xapprove": True, "xbox_signed_in_at": "2026-11-08T12:00:00+00:00"},
    )
    entry.add_to_hass(hass)
    coord = FamilySafetyDataUpdateCoordinator(hass, entry)
    coord.data = {"accounts": {}, "devices": {}}
    await coord._xbox_signin_store.async_save({
        "refresh_token": "xbox-refresh-secret", "proof_key": "PRIVATE-KEY-SECRET",
        "device_id": "device-id-secret",
    })
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coord

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["xbox_xapprove"]["option_enabled"] is True
    assert result["xbox_xapprove"]["xbox_signed_in"] is True
    assert result["xbox_xapprove"]["xbox_signed_in_at"] == "2026-11-08T12:00:00+00:00"
    text = json.dumps(result)
    for secret in ("xbox-refresh-secret", "PRIVATE-KEY-SECRET", "device-id-secret", "refresh-secret"):
        assert secret not in text
