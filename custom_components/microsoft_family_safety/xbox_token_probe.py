"""Diagnostic: can the stored mobile sign-in also reach Xbox Live?

microsoft_family_safety.probe_xbox_token runs this inside Home Assistant, with
the refresh token the integration already holds, and answers with non-secret
facts only. It never returns or logs a token, a gamertag or an Xbox user id.

Steps, each one only if the previous one produced a token:

1. ``login.live.com/oauth20_token.srf``: refresh_token grant for the Family
   Safety client id, scope ``service::user.auth.xboxlive.com::MBI_SSL``.
   A refresh token in the answer is discarded unread: the integration keeps
   its own (Family-scope) refresh token, untouched.
2. ``user.auth.xboxlive.com/user/authenticate`` (RPS, ticket ``t=<token>``,
   then ``d=<token>`` if refused).
3. ``xsts.auth.xboxlive.com/xsts/authorize`` (relying party
   ``http://xboxlive.com``, sandbox RETAIL): status, XErr, and which
   DisplayClaims ``xui`` keys come back (their names, never their values).

The risk (see README): Microsoft documents that the identity platform does
not revoke a refresh token when it is used, and that a refresh token is not
tied to a scope, but does not document this for the legacy login.live.com
endpoint. If that endpoint revoked the old token on use, the integration
would need a new sign-in. That is why the service needs ``confirm_risk``,
and why it checks the stored token with a normal Family refresh afterwards.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)

TOKEN_URL = "https://login.live.com/oauth20_token.srf"
CLIENT_ID = "000000000004893A"
XBOX_SCOPE = "service::user.auth.xboxlive.com::MBI_SSL"
USER_AUTH_URL = "https://user.auth.xboxlive.com/user/authenticate"
XSTS_URL = "https://xsts.auth.xboxlive.com/xsts/authorize"
_TIMEOUT = aiohttp.ClientTimeout(total=30)
_XBL_HEADERS = {"x-xbl-contract-version": "1", "Accept": "application/json"}


async def _post(
    session: aiohttp.ClientSession, url: str, **kwargs: Any
) -> tuple[int | None, Any, dict[str, str], str | None]:
    """POST; (status, JSON or None, response headers, error class name)."""
    try:
        async with session.post(url, timeout=_TIMEOUT, **kwargs) as resp:
            try:
                payload = await resp.json(content_type=None)
            except (ValueError, aiohttp.ContentTypeError):
                payload = None
            return resp.status, payload, dict(resp.headers), None
    except (aiohttp.ClientError, asyncio.TimeoutError, TimeoutError) as err:
        return None, None, {}, type(err).__name__


def _seconds_until(value: Any) -> int | None:
    parsed = dt_util.parse_datetime(str(value)) if value else None
    if parsed is None:
        return None
    return int((dt_util.as_utc(parsed) - dt_util.utcnow()).total_seconds())


def _xerr(payload: Any, headers: dict[str, str]) -> Any:
    if isinstance(payload, dict) and payload.get("XErr") is not None:
        return payload["XErr"]
    for name, value in headers.items():
        if name.lower() == "x-err":
            return value
    return None


def _xbl_step(status: int | None, payload: Any, headers: dict[str, str], error: str | None) -> dict:
    step: dict[str, Any] = {"status": status, "xerr": _xerr(payload, headers)}
    if error:
        step["error"] = error
    if isinstance(payload, dict):
        for key in ("Message", "Redirect"):
            if isinstance(payload.get(key), str):
                step[key.lower()] = payload[key][:300]
        step["token_received"] = bool(payload.get("Token"))
        step["expires_in"] = _seconds_until(payload.get("NotAfter"))
    return step


async def async_probe_xbox_token(
    session: aiohttp.ClientSession, refresh_token: str
) -> dict[str, Any]:
    """Run the three steps; only non-secret results come back."""
    steps: dict[str, Any] = {}
    result: dict[str, Any] = {"steps": steps}

    status, payload, _headers, error = await _post(
        session,
        TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "client_id": CLIENT_ID,
            "scope": XBOX_SCOPE,
            "refresh_token": refresh_token,
        },
    )
    access: str | None = None
    token_step: dict[str, Any] = {"status": status}
    if error:
        token_step["error"] = error
    if isinstance(payload, dict):
        access = payload.get("access_token") if isinstance(payload.get("access_token"), str) else None
        token_step.update({
            "error": payload.get("error") if isinstance(payload.get("error"), str) else error,
            "error_description": (
                str(payload["error_description"])[:300]
                if payload.get("error_description") else None
            ),
            "scope": payload.get("scope") if isinstance(payload.get("scope"), str) else None,
            "token_type": payload.get("token_type") if isinstance(payload.get("token_type"), str) else None,
            "expires_in": payload.get("expires_in"),
            "access_token_received": access is not None,
            # Never stored: the integration keeps its own refresh token.
            "refresh_token_returned": bool(payload.get("refresh_token")),
            "refresh_token_discarded": True,
        })
    payload = None  # drop the tokens it held
    steps["msa_refresh"] = token_step
    _LOGGER.debug("Xbox token probe, MSA refresh: status %s", status)
    if not access:
        return result

    user_token: str | None = None
    attempts: list[dict[str, Any]] = []
    for prefix in ("t", "d"):
        status, body, headers, error = await _post(
            session,
            USER_AUTH_URL,
            json={
                "RelyingParty": "http://auth.xboxlive.com",
                "TokenType": "JWT",
                "Properties": {
                    "AuthMethod": "RPS",
                    "SiteName": "user.auth.xboxlive.com",
                    "RpsTicket": f"{prefix}={access}",
                },
            },
            headers=_XBL_HEADERS,
        )
        attempt = {"ticket_prefix": prefix, **_xbl_step(status, body, headers, error)}
        attempts.append(attempt)
        _LOGGER.debug("Xbox token probe, user authenticate (%s=): status %s", prefix, status)
        if status == 200 and isinstance(body, dict) and isinstance(body.get("Token"), str):
            user_token = body["Token"]
            break
    access = None
    steps["user_authenticate"] = attempts
    if not user_token:
        return result

    status, body, headers, error = await _post(
        session,
        XSTS_URL,
        json={
            "RelyingParty": "http://xboxlive.com",
            "TokenType": "JWT",
            "Properties": {"UserTokens": [user_token], "SandboxId": "RETAIL"},
        },
        headers=_XBL_HEADERS,
    )
    user_token = None
    xsts = _xbl_step(status, body, headers, error)
    keys: set[str] = set()
    if isinstance(body, dict):
        claims = body.get("DisplayClaims")
        xui = claims.get("xui") if isinstance(claims, dict) else None
        for item in xui if isinstance(xui, list) else []:
            if isinstance(item, dict):
                keys.update(str(k) for k in item)
    xsts["display_claims_xui_keys"] = sorted(keys)
    steps["xsts_authorize"] = xsts
    _LOGGER.debug("Xbox token probe, XSTS: status %s", status)
    return result
