"""Diagnostics for Microsoft Family Safety, with credentials and personal data removed.

Tokens, cookies, e-mail addresses and names are redacted by key. Account and
device ids are replaced by stable aliases ("account_1", "device_1") wherever
they appear, also inside dictionary keys and longer strings (an endpoint path,
a hold key), so the structure stays readable without identifying anyone.
"""
from __future__ import annotations

import re
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_ALLOWED_USERS,
    CONF_API_KEY,
    CONF_AUTH_URL,
    CONF_AUTH_USER_ID,
    CONF_REDIRECT_URL,
    CONF_REFRESH_TOKEN,
    CONF_TOKEN,
    CONF_WEB_COOKIES,
    CONF_WEB_FAMILY_REFERER,
    CONF_WEB_FAMILY_TOKEN,
    CONF_XBOX_SIGNED_IN_AT,
    CONF_XBOX_XUIDS,
    DOMAIN,
)

TO_REDACT = {
    # credentials
    CONF_TOKEN,
    CONF_REFRESH_TOKEN,
    CONF_REDIRECT_URL,
    CONF_WEB_COOKIES,
    CONF_WEB_FAMILY_TOKEN,
    CONF_WEB_FAMILY_REFERER,
    CONF_API_KEY,
    CONF_AUTH_URL,
    "access_token",
    "cookies",
    # people
    CONF_AUTH_USER_ID,
    CONF_ALLOWED_USERS,
    CONF_XBOX_XUIDS,
    "first_name",
    "surname",
    "profile_picture",
    "email",
    "emailAddress",
    "displayName",
    "requester",
    "device_name",
    "title",
    # browsing history-like lists
    "exceptions",
    "Exceptions",
    "blockedSites",
    "allowedSites",
    "BlockedSites",
    "AllowedSites",
}

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")


def _aliases(data: dict[str, Any]) -> dict[str, str]:
    """Account and device ids -> aliases, longest first so none is a prefix of another."""
    aliases: dict[str, str] = {}
    for index, account_id in enumerate((data.get("accounts") or {}), start=1):
        aliases[str(account_id)] = f"account_{index}"
    for index, device_id in enumerate((data.get("devices") or {}), start=1):
        aliases.setdefault(str(device_id), f"device_{index}")
    return dict(sorted(aliases.items(), key=lambda item: -len(item[0])))


def _anonymise(value: Any, aliases: dict[str, str]) -> Any:
    if isinstance(value, dict):
        return {
            _anonymise(str(key), aliases): _anonymise(item, aliases)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [_anonymise(item, aliases) for item in value]
    if isinstance(value, int) and not isinstance(value, bool) and str(value) in aliases:
        return aliases[str(value)]
    if isinstance(value, str):
        for real, alias in aliases.items():
            if real and real in value:
                value = value.replace(real, alias)
        return _EMAIL.sub("**REDACTED**", value)
    return value


def _account_summary(account: dict[str, Any]) -> dict[str, Any]:
    """An account's data without the long app list."""
    summary = {k: v for k, v in account.items() if k != "applications"}
    summary["application_count"] = len(account.get("applications") or [])
    return summary


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return redacted diagnostics for a config entry."""
    coordinator = (hass.data.get(DOMAIN) or {}).get(entry.entry_id)
    data: dict[str, Any] = dict(getattr(coordinator, "data", None) or {})
    aliases = _aliases(data)
    result: dict[str, Any] = {
        "entry": {
            "version": entry.version,
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": async_redact_data(dict(entry.options), TO_REDACT),
        },
    }
    if coordinator is None:
        return _anonymise(result, aliases)
    result["connection"] = coordinator.connection_state()
    result["data"] = async_redact_data(
        {
            "accounts": {
                key: _account_summary(value)
                for key, value in (data.get("accounts") or {}).items()
                if isinstance(value, dict)
            },
            "devices": data.get("devices") or {},
            "pending_request_count": len(data.get("pending_requests") or []),
        },
        TO_REDACT,
    )
    result["platform_holds"] = getattr(coordinator, "_platform_holds", {})
    result["saved_screentime_accounts"] = len(getattr(coordinator, "_saved_screentime", {}))
    result["options_in_effect"] = {
        "strict_mode": getattr(coordinator, "_strict_mode", None),
        "app_usage_sensors": getattr(coordinator, "app_usage_sensors", None),
        "web_session_enabled": coordinator.web_session_enabled,
    }
    # Non-secret state of the Xbox Family Settings sign-in and xapprove.
    store = getattr(coordinator, "_xbox_signin_store", None)
    if store is not None:
        credential = await store.async_load()
        refusal = getattr(coordinator, "_xapprove_refusal", None)
        result["xbox_xapprove"] = {
            "option_enabled": getattr(coordinator, "_xapprove_enabled", None),
            "xbox_signed_in": bool(isinstance(credential, dict) and credential.get("refresh_token")),
            "xbox_signed_in_at": entry.options.get(CONF_XBOX_SIGNED_IN_AT),
            "method": getattr(getattr(coordinator, "_xapprove_method", None), "name", None),
            "refused": dict(refusal) if isinstance(refusal, dict) else None,
        }
    return _anonymise(result, aliases)
