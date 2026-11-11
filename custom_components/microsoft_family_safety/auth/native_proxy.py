"""Native browser-assisted Microsoft Family Safety authentication.

The user's regular browser renders Microsoft's mobile OAuth sign-in while Home
Assistant acts as a short-lived reverse proxy. The flow ends on the
``oauth20_desktop.srf`` redirect, whose code becomes the mobile refresh token.
Microsoft session cookies stay in the server-side httpx cookie jar for the
lifetime of the flow only. Passwords and MFA values are never persisted or
logged.
"""
from __future__ import annotations

import asyncio
import base64
import html
import json
import logging
import re
import secrets
import time
from http.cookies import SimpleCookie
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, unquote, urljoin, urlsplit, urlunsplit

import httpx
from aiohttp import web
from yarl import URL

from homeassistant.components.http import HomeAssistantView
from homeassistant.components.http.security_filter import FILTERS
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import UnknownFlow
from homeassistant.helpers.httpx_client import create_async_httpx_client

from ..const import DOMAIN

_LOGGER = logging.getLogger(__name__)

AUTH_PROXY_PATH = "/auth/microsoft_family_safety/proxy"
AUTH_CALLBACK_PATH = "/auth/microsoft_family_safety/callback"
_PROXY_REGISTRY = f"{DOMAIN}_native_auth_proxies"
_VIEWS_REGISTERED = f"{DOMAIN}_native_auth_views_registered"
_CALLBACK_TOKENS = f"{DOMAIN}_native_auth_callback_tokens"
_HOST_MARKER = "__ms_host__"

#: The only hosts served from the Home Assistant origin: the pages of the sign-in
#: itself. Their scripts run same-origin with the frontend, so nothing else gets
#: in. Assets, telemetry and fingerprinting hosts stay direct browser requests.
_PROXIED_HOSTS = frozenset(
    {
        "account.live.com",
        "account.microsoft.com",
        "login.live.com",
        "login.microsoft.com",
        "login.microsoftonline.com",
    }
)
#: A redirect to these leaves the proxy for the real site (support and privacy
#: links, sign-up); anywhere else is refused.
_MICROSOFT_DOMAIN_ROOTS = ("live.com", "microsoft.com", "microsoftonline.com")
#: Query parameter carrying a whole upstream query, see _hide_query_from_filter.
_HIDDEN_QUERY = "__ms_q"
_PROXY_LIFETIME_SECONDS = 600
#: Cookies issued by Akamai Bot Manager in front of Microsoft's sites. They are
#: bound to the fingerprint of the browser that obtained them and make Microsoft
#: hang up on any other client that replays them, so they are never exported.
_BOT_MANAGER_COOKIE_NAMES: frozenset[str] = frozenset(
    {"bm_sv", "bm_sz", "bm_mi", "bm_s", "bm_so", "bm_ss", "bm_lso", "ak_bmsc", "_abck", "akavpau_ppsd"}
)

_AUTH_COOKIE_NAMES = {
    "MSPAuth",
    "MSPProf",
    "WLSSC",
    "RPSAuth",
    "RPSSecAuth",
}

#: ``MAX_LINE_SIZE`` in homeassistant/components/http/server.py, passed to
#: aiohttp as ``max_field_size``: a single request header above it answers
#: 400 before any view runs. The browser's Cookie header for the proxy path
#: has to stay well below it (#60).
_BROWSER_COOKIE_HEADER_LIMIT = 24570
#: Largest cookie value worth mirroring to the browser. Microsoft's page
#: scripts read small markers (market, correlation vectors, telemetry ids);
#: the multi-kilobyte tokens are HttpOnly anyway and only matter upstream.
_BROWSER_COOKIE_VALUE_LIMIT = 2048

_HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "host",
    "cookie",
    "content-length",
    "accept-encoding",
}

_RESPONSE_HEADERS_TO_DROP = _HOP_BY_HOP_HEADERS | {
    "set-cookie",
    "content-encoding",
    "content-security-policy",
    "content-security-policy-report-only",
    "x-frame-options",
    "location",
}

_REWRITABLE_ATTRS = (
    "href",
    "src",
    "action",
    "formaction",
    "data-url",
)


def _normalized_host(host: str | None) -> str:
    # Microsoft's MSAL sign-in page builds some links with an explicit port
    # (login.microsoftonline.com:443); the port is not part of the host.
    return (host or "").split(":", 1)[0].rstrip(".").lower()


def _is_allowed_host(host: str | None) -> bool:
    """Return whether host is one of the sign-in hosts served through the proxy."""
    return _normalized_host(host) in _PROXIED_HOSTS


def _is_microsoft_host(host: str | None) -> bool:
    """Return whether host belongs to a Microsoft domain."""
    normalized = _normalized_host(host)
    return any(
        normalized == root or normalized.endswith(f".{root}")
        for root in _MICROSOFT_DOMAIN_ROOTS
    )


def _trips_security_filter(path_and_query: str) -> bool:
    """Return whether Home Assistant's security filter would refuse this request."""
    previous = None
    while path_and_query != previous:
        previous, path_and_query = path_and_query, unquote(path_and_query)
    return FILTERS.search(path_and_query) is not None


def _hide_query_from_filter(url: str) -> str:
    """Carry a query the security filter refuses as one opaque parameter.

    Microsoft's silent-SSO redirects hold values such as ``epctrc=/w/...`` that
    look like a file injection, so the browser would get a bare 400 on its way
    back to the proxy. The query only travels through Home Assistant to reach
    Microsoft, and _reveal_query restores it before that. The filter stays in
    force: it scans the encoded form, and Home Assistant itself never reads it.
    """
    parts = urlsplit(url)
    if not parts.query or not _trips_security_filter(f"{parts.path}?{parts.query}"):
        return url
    hidden = base64.urlsafe_b64encode(parts.query.encode()).decode().rstrip("=")
    return urlunsplit(parts._replace(query=f"{_HIDDEN_QUERY}={hidden}"))


def _reveal_query(hidden: str) -> str:
    """Return the query that _hide_query_from_filter encoded."""
    padded = hidden + "=" * (-len(hidden) % 4)
    return base64.b64decode(padded, altchars=b"-_", validate=True).decode()


def _reveal_url_query(url: str) -> str:
    """Restore a hidden query inside a full URL; anything else is returned as is."""
    parts = urlsplit(url)
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    if len(pairs) != 1 or pairs[0][0] != _HIDDEN_QUERY:
        return url
    try:
        return urlunsplit(parts._replace(query=_reveal_query(pairs[0][1])))
    except ValueError:
        return url


def _cookie_dict(cookie: Any) -> dict[str, Any]:
    """Serialize a CookieJar cookie into the Playwright-compatible shape."""
    result: dict[str, Any] = {
        "name": cookie.name,
        "value": cookie.value,
        "domain": cookie.domain,
        "path": cookie.path or "/",
        "secure": bool(cookie.secure),
        "httpOnly": bool(cookie.has_nonstandard_attr("HttpOnly")),
    }
    if cookie.expires is not None:
        result["expires"] = cookie.expires
    if cookie.get_nonstandard_attr("SameSite"):
        result["sameSite"] = cookie.get_nonstandard_attr("SameSite")
    return result


class MicrosoftFamilyAuthProxy:
    """Short-lived, single-flow reverse proxy for Microsoft's consumer login."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        callback_url: str,
        token: str | None = None,
        start_url: str,
        initial_cookies: list[dict[str, Any]] | None = None,
    ) -> None:
        self.hass = hass
        self.token = token or secrets.token_urlsafe(24)
        # The token proves to the callback view that the browser went through
        # this proxy; server-side uses have no callback.
        self.callback_url = (
            str(URL(callback_url).update_query(token=self.token)) if callback_url else ""
        )
        self.proxy_path = f"{AUTH_PROXY_PATH}/{self.token}"
        self._start_url = URL(start_url)
        self._last_url = self._start_url
        self._oauth_redirect_url: str | None = None
        self._complete = False
        self._flow_id = URL(callback_url).query.get("flow_id")
        self._closed = False
        self._expiry_task: asyncio.Task | None = None
        self._discovered_hosts: set[str] = set()
        if self._start_url.host and _is_allowed_host(self._start_url.host):
            self._discovered_hosts.add(self._start_url.host.lower())
        # Create a dedicated HA-managed httpx client. Home Assistant provides
        # a pre-built SSL context here, avoiding certifi/SSL filesystem work in
        # the event loop while keeping a separate cookie jar for this auth flow.
        # auto_cleanup=False because this short-lived proxy closes the client
        # explicitly when the flow finishes/expires.
        self._client = create_async_httpx_client(
            hass,
            auto_cleanup=False,
            follow_redirects=False,
            timeout=httpx.Timeout(connect=30.0, read=120.0, write=30.0, pool=30.0),
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                )
            },
        )
        from http.cookiejar import Cookie as _JarCookie

        for cookie in initial_cookies or []:
            name = cookie.get("name")
            value = cookie.get("value")
            domain = cookie.get("domain")
            if not name or value is None or not domain:
                continue
            # httpx.Cookies.set() drops the expiry, so a persistent Microsoft
            # cookie re-exported from this jar would look like a session one.
            # Build the jar cookie directly to keep expires/secure/HttpOnly.
            expires = cookie.get("expires")
            try:
                expires_at = int(expires) if expires not in (None, -1, "") and float(expires) > 0 else None
            except (TypeError, ValueError):
                expires_at = None
            rest: dict[str, str] = {}
            if cookie.get("httpOnly"):
                rest["HttpOnly"] = ""
            if cookie.get("sameSite"):
                rest["SameSite"] = str(cookie["sameSite"])
            try:
                self._client.cookies.jar.set_cookie(
                    _JarCookie(
                        version=0,
                        name=name,
                        value=str(value),
                        port=None,
                        port_specified=False,
                        domain=domain,
                        domain_specified=True,
                        domain_initial_dot=domain.startswith("."),
                        path=cookie.get("path") or "/",
                        path_specified=True,
                        secure=bool(cookie.get("secure", True)),
                        expires=expires_at,
                        discard=expires_at is None,
                        comment=None,
                        comment_url=None,
                        rest=rest,
                        rfc2109=False,
                    )
                )
            except Exception as err:
                _LOGGER.debug("Could not preload Microsoft auth cookie %s: %s", name, err)

        _LOGGER.debug(
            "Created native Microsoft Family auth proxy: start=%s%s "
            "initial_cookie_count=%d",
            self._start_url.host,
            self._start_url.path,
            len(initial_cookies or []),
        )

    @property
    def complete(self) -> bool:
        return self._complete

    def _mark_complete(self) -> None:
        """Mark the browser authentication phase complete."""
        self._complete = True

    def _capture_oauth_redirect(self, url: URL, source: str) -> None:
        """The registered desktop redirect ends the proxy.

        Capture the code (or error) and send the browser to the callback
        instead of letting it navigate to the desktop page.
        """
        if not (
            url.host == "login.live.com"
            and url.path == "/oauth20_desktop.srf"
            and ("code" in url.query or "error" in url.query)
        ):
            return
        self._oauth_redirect_url = str(url)
        if "code" in url.query:
            self._mark_complete()
            _LOGGER.info(
                "Native Microsoft Family mobile OAuth redirect captured from %s: "
                "code_present=True cookie_count=%d",
                source, len(self.export_cookies()),
            )
        else:
            _LOGGER.warning(
                "Microsoft Family mobile OAuth returned an error %s: error=%s",
                source, url.query.get("error", "unknown"),
            )
        raise web.HTTPFound(location=self.callback_url)

    @property
    def flow_id(self) -> str | None:
        return self._flow_id

    @property
    def access_path(self) -> str:
        return self.proxy_path

    @property
    def oauth_redirect_url(self) -> str | None:
        """Return the intercepted Microsoft desktop OAuth redirect URL."""
        return self._oauth_redirect_url

    def export_cookies(self) -> list[dict[str, Any]]:
        """Return the Microsoft/Live cookies of this sign-in, without bot-manager ones."""
        cookies: list[dict[str, Any]] = []
        for cookie in self._client.cookies.jar:
            if cookie.name in _BOT_MANAGER_COOKIE_NAMES:
                continue
            domain = (cookie.domain or "").lstrip(".").lower()
            if domain == "microsoft.com" or domain.endswith(".microsoft.com"):
                cookies.append(_cookie_dict(cookie))
            elif domain == "live.com" or domain.endswith(".live.com"):
                cookies.append(_cookie_dict(cookie))
        return cookies

    async def async_close(self) -> None:
        if self._closed:
            return
        self._closed = True
        task = self._expiry_task
        self._expiry_task = None
        if task and task is not asyncio.current_task():
            task.cancel()
        # create_async_httpx_client wraps instance.aclose() to warn integrations
        # that close shared HA clients. This client is intentionally dedicated
        # (auto_cleanup=False), so call the httpx class implementation directly.
        await httpx.AsyncClient.aclose(self._client)

    def diagnostic_summary(self) -> dict[str, Any]:
        """Return a secret-free diagnostic snapshot for auth-flow logging."""
        exported = self.export_cookies()
        auth_cookie_count = sum(
            1 for cookie in self._client.cookies.jar if cookie.name in _AUTH_COOKIE_NAMES
        )
        return {
            "complete": self._complete,
            "exported_cookie_count": len(exported),
            "auth_cookie_count": auth_cookie_count,
            "last_host": self._last_url.host if self._last_url else None,
            "last_path": self._last_url.path if self._last_url else None,
        }

    def _remember_host(self, host: str | None, source: str) -> bool:
        """Remember a dynamically discovered trusted Microsoft auth host."""
        if not _is_allowed_host(host):
            return False
        normalized = _normalized_host(host)
        if normalized not in self._discovered_hosts:
            self._discovered_hosts.add(normalized)
            _LOGGER.debug(
                "Proxying Microsoft auth host %s via %s",
                normalized,
                source,
            )
        return True

    def _target_from_request(self, request: web.Request) -> URL:
        """Resolve a browser proxy request back to its Microsoft origin."""
        tail = request.match_info.get("tail", "") or ""
        tail = tail.lstrip("/")

        if not tail:
            target = self._start_url
        else:
            marker = f"{_HOST_MARKER}/"
            if tail.startswith(marker):
                encoded = tail[len(marker) :]
                host, slash, path = encoded.partition("/")
                # Drop an explicit :443 (or any port): Microsoft's MSAL page
                # emits such links and the upstream target is always https.
                host = host.split(":", 1)[0].rstrip(".").lower()
                if not self._remember_host(host, "proxy route"):
                    raise web.HTTPForbidden(
                        text="Microsoft authentication host not allowed"
                    )
                try:
                    target = URL.build(
                        scheme="https",
                        host=host,
                        path=f"/{path}" if slash else "/",
                    )
                except ValueError as err:  # "a@b", "x#y": a 403, not a 500
                    raise web.HTTPForbidden(
                        text="Microsoft authentication host not allowed"
                    ) from err
            else:
                # Defensive fallback for relative requests that escaped browser
                # rewriting. Resolve them against the actual last Microsoft URL.
                upstream_base = str(self._last_url)
                resolved = urlsplit(urljoin(upstream_base, tail))
                host = (resolved.hostname or "").lower()
                if not self._remember_host(host, "relative proxy request"):
                    raise web.HTTPForbidden(
                        text="Microsoft authentication host not allowed"
                    )
                target = URL(
                    urlunsplit(
                        (
                            "https",
                            host,
                            resolved.path or "/",
                            resolved.query,
                            "",
                        )
                    )
                )

        query_items = list(request.query.items())
        if hidden := request.query.get(_HIDDEN_QUERY):
            try:
                query_items = parse_qsl(_reveal_query(hidden), keep_blank_values=True)
            except ValueError as err:
                raise web.HTTPBadRequest(text="Invalid proxied query") from err

        if query_items:
            proxy_origin = f"{request.scheme}://{request.host}"
            restored_query = [
                (key, self._deproxy_value(value, proxy_origin))
                for key, value in query_items
            ]
            target = target.with_query(restored_query)
        return target

    def _proxy_bases(self, proxy_origin: str) -> tuple[str, str]:
        """Return absolute and path-only proxy bases for this flow."""
        return (
            f"{proxy_origin.rstrip('/')}{self.proxy_path}",
            self.proxy_path,
        )

    def _direct_deproxy_url(self, value: str, proxy_origin: str) -> str:
        """Undo one direct HA-proxy URL mapping without decoding payload data."""
        if not value:
            return value

        for base in self._proxy_bases(proxy_origin):
            marker = f"{base}/{_HOST_MARKER}/"
            if value.startswith(marker):
                encoded = value[len(marker) :]
                host, slash, rest = encoded.partition("/")
                host = host.rstrip(".").lower()
                if not self._remember_host(host, "deproxy"):
                    return value
                return _reveal_url_query(
                    f"https://{host}/" + rest if slash else f"https://{host}/"
                )

            # The root proxy URL represents the current upstream page. This is
            # needed when Microsoft derives postBackUrl from window.location.
            if value == base:
                return str(self._last_url)
            if value.startswith(base + "?") or value.startswith(base + "#"):
                suffix = value[len(base) :]
                current = urlsplit(str(self._last_url))
                if suffix.startswith("?"):
                    fragment = ""
                    query_and_fragment = suffix[1:]
                    if "#" in query_and_fragment:
                        query, fragment = query_and_fragment.split("#", 1)
                    else:
                        query = query_and_fragment
                    return urlunsplit(
                        (
                            current.scheme,
                            current.netloc,
                            current.path,
                            query,
                            fragment,
                        )
                    )
                return urlunsplit(
                    (
                        current.scheme,
                        current.netloc,
                        current.path,
                        current.query,
                        suffix[1:],
                    )
                )
        return value

    def _restore_url_query(
        self,
        value: str,
        proxy_origin: str,
        *,
        depth: int,
    ) -> str:
        """Recursively restore proxied URLs nested in another URL's query."""
        if depth > 6:
            return value
        try:
            parsed = urlsplit(value)
        except ValueError:
            return value
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return value
        if not parsed.query:
            return value

        pairs = parse_qsl(parsed.query, keep_blank_values=True)
        restored = [
            (key, self._deproxy_value(item, proxy_origin, depth=depth + 1))
            for key, item in pairs
        ]
        new_query = urlencode(restored, doseq=True)
        if new_query == parsed.query:
            return value
        return urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path, new_query, parsed.fragment)
        )

    def _deproxy_value(
        self,
        value: str,
        proxy_origin: str,
        *,
        depth: int = 0,
    ) -> str:
        """Dynamically restore proxy URLs, including nested/encoded URLs.

        This is intentionally field-name agnostic: postBackUrl, returnUrl,
        redirect_uri and future Microsoft fields are treated identically.
        """
        if not isinstance(value, str) or not value or depth > 6:
            return value

        direct = self._direct_deproxy_url(value, proxy_origin)
        if direct != value:
            return self._restore_url_query(
                direct, proxy_origin, depth=depth + 1
            )

        # Even an otherwise normal Microsoft URL can carry an encoded proxy URL
        # in one of its query parameters. Parse/rebuild it recursively instead
        # of maintaining a list of parameter names.
        restored_url = self._restore_url_query(
            value, proxy_origin, depth=depth + 1
        )
        if restored_url != value:
            return restored_url

        # Handle an additional URL-encoding layer. Only accept the decoded form
        # when it actually exposes our own proxy namespace, avoiding arbitrary
        # decoding of unrelated Microsoft values.
        try:
            decoded = unquote(value)
        except Exception:
            decoded = value
        if decoded != value and (
            self.proxy_path in decoded
            or f"/{_HOST_MARKER}/" in decoded
        ):
            restored = self._deproxy_value(
                decoded, proxy_origin, depth=depth + 1
            )
            if restored != decoded:
                # Preserve the remaining percent-encoding layer. Callers such
                # as parse_qsl/urlencode already handle their own outer layer.
                return quote(restored, safe="")

        return value

    def _deproxy_object(self, obj: Any, proxy_origin: str) -> Any:
        """Recursively restore proxied URL strings in JSON-like objects."""
        if isinstance(obj, str):
            return self._deproxy_value(obj, proxy_origin)
        if isinstance(obj, list):
            return [self._deproxy_object(item, proxy_origin) for item in obj]
        if isinstance(obj, dict):
            return {
                key: self._deproxy_object(value, proxy_origin)
                for key, value in obj.items()
            }
        return obj

    def _deproxy_request_body(
        self, body: bytes | None, content_type: str, proxy_origin: str
    ) -> bytes | None:
        """Restore Microsoft protocol URLs embedded in browser request bodies."""
        if not body:
            return body

        media_type = content_type.split(";", 1)[0].strip().lower()
        try:
            if media_type == "application/x-www-form-urlencoded":
                text = body.decode("utf-8")
                pairs = parse_qsl(text, keep_blank_values=True)
                restored = [
                    (key, self._deproxy_value(value, proxy_origin))
                    for key, value in pairs
                ]
                if restored != pairs:
                    _LOGGER.debug(
                        "Restored proxied Microsoft URL value(s) in form request body"
                    )
                    return urlencode(restored, doseq=True).encode("utf-8")
                return body

            if media_type == "application/json" or media_type.endswith("+json"):
                parsed = json.loads(body.decode("utf-8"))
                restored = self._deproxy_object(parsed, proxy_origin)
                if restored != parsed:
                    _LOGGER.debug(
                        "Restored proxied Microsoft URL value(s) in JSON request body"
                    )
                    return json.dumps(
                        restored, separators=(",", ":"), ensure_ascii=False
                    ).encode("utf-8")
        except (UnicodeDecodeError, ValueError, TypeError):
            # Preserve the original body if it is not safely parseable.
            return body

        return body

    def _request_headers(self, request: web.Request, target: URL) -> dict[str, str]:
        """Copy browser headers without HA cookies or hop-by-hop metadata."""
        headers: dict[str, str] = {}
        for key, value in request.headers.items():
            lower = key.lower()
            if lower in _HOP_BY_HOP_HEADERS:
                continue
            if lower.startswith("x-forwarded-"):
                continue
            if lower.startswith("sec-fetch-"):
                continue
            if lower in {"origin", "referer"}:
                continue
            headers[key] = value

        source_host = (
            self._last_url.host
            if self._last_url.host and _is_allowed_host(self._last_url.host)
            else target.host
        )
        if request.method.upper() not in {"GET", "HEAD", "OPTIONS"}:
            headers["Origin"] = f"https://{source_host}"
        if self._last_url.host and _is_allowed_host(self._last_url.host):
            ref_path = self._last_url.raw_path_qs or "/"
            headers["Referer"] = f"https://{self._last_url.host}{ref_path}"
        return headers

    def _proxyize_url(
        self,
        value: str,
        current_url: str,
        proxy_origin: str,
        *,
        allow_relative: bool = False,
        source: str = "response",
    ) -> str:
        """Virtualize one Microsoft URL as an absolute HA proxy URL."""
        if not isinstance(value, str) or not value:
            return value
        if value.startswith(("#", "javascript:", "data:", "mailto:", "tel:")):
            return value

        proxy_origin = proxy_origin.rstrip("/")
        absolute_base, relative_base = self._proxy_bases(proxy_origin)
        if value.startswith(absolute_base + "/"):
            return _hide_query_from_filter(value)
        if value.startswith(relative_base + "/"):
            return _hide_query_from_filter(proxy_origin + value)

        candidate = html.unescape(value)
        if candidate.startswith("//"):
            candidate = "https:" + candidate
        elif allow_relative and not urlsplit(candidate).scheme:
            candidate = urljoin(current_url, candidate)

        try:
            parsed = urlsplit(candidate)
        except ValueError:
            return value
        host = (parsed.hostname or "").rstrip(".").lower()
        if parsed.scheme not in {"http", "https"} or not self._remember_host(host, source):
            return value

        path = parsed.path or "/"
        proxied = f"{absolute_base}/{_HOST_MARKER}/{host}{path}"
        if parsed.query:
            proxied += f"?{parsed.query}"
        if parsed.fragment:
            proxied += f"#{parsed.fragment}"
        return _hide_query_from_filter(proxied)

    def _proxy_response_object(
        self,
        obj: Any,
        current_url: str,
        proxy_origin: str,
    ) -> Any:
        """Dynamically virtualize Microsoft URLs in browser-facing JSON."""
        if isinstance(obj, str):
            return self._proxyize_url(
                obj,
                current_url,
                proxy_origin,
                allow_relative=False,
                source="JSON response",
            )
        if isinstance(obj, list):
            return [
                self._proxy_response_object(item, current_url, proxy_origin)
                for item in obj
            ]
        if isinstance(obj, dict):
            return {
                key: self._proxy_response_object(value, current_url, proxy_origin)
                for key, value in obj.items()
            }
        return obj

    def _rewrite_json_response(
        self, body: bytes, current_url: str, proxy_origin: str
    ) -> bytes:
        """Virtualize trusted Microsoft URLs in a JSON response."""
        try:
            parsed = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, TypeError):
            return body
        rewritten = self._proxy_response_object(parsed, current_url, proxy_origin)
        if rewritten == parsed:
            return body
        host = urlsplit(current_url).hostname or "unknown"
        _LOGGER.debug(
            "Virtualized Microsoft auth URL value(s) in JSON response from %s",
            host,
        )
        return json.dumps(
            rewritten, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")

    def _rewrite_html(self, body: str, current_url: str, proxy_origin: str) -> str:
        """Virtualize Microsoft browser navigations and install runtime routing."""
        proxy_origin = proxy_origin.rstrip("/")
        current_host = urlsplit(current_url).hostname or "account.microsoft.com"

        # Attribute URLs are actual browser navigation/resource targets, so they
        # can safely be virtualized. Protocol values elsewhere are handled by the
        # generic response/request transformers instead of a field-name list.
        attr_pattern = re.compile(
            rf"(?P<prefix>\b(?:{'|'.join(_REWRITABLE_ATTRS)})\s*=\s*[\"'])(?P<url>[^\"']+)(?P<quote>[\"'])",
            re.IGNORECASE,
        )

        def attr_repl(match: re.Match[str]) -> str:
            url = self._proxyize_url(
                match.group("url"),
                current_url,
                proxy_origin,
                allow_relative=True,
                source="HTML attribute",
            )
            return f"{match.group('prefix')}{url}{match.group('quote')}"

        body = attr_pattern.sub(attr_repl, body)

        # Dynamically rewrite absolute Microsoft URL literals in inline config.
        # Only the sign-in hosts are rewritten. Keeping the result absolute preserves
        # Microsoft code that calls new URL(value) without a base URL.
        plain_url_prefix = re.compile(
            r"(?P<prefix>https?:)?//(?P<host>[A-Za-z0-9.-]+)",
            re.IGNORECASE,
        )

        def plain_repl(match: re.Match[str]) -> str:
            host = match.group("host").rstrip(".").lower()
            if not self._remember_host(host, "inline HTML/JS"):
                return match.group(0)
            return (
                f"{proxy_origin}{self.proxy_path}/{_HOST_MARKER}/{host}"
            )

        body = plain_url_prefix.sub(plain_repl, body)

        escaped_url_prefix = re.compile(
            r"https?:\\/\\/(?P<host>[A-Za-z0-9.-]+)",
            re.IGNORECASE,
        )

        def escaped_repl(match: re.Match[str]) -> str:
            host = match.group("host").rstrip(".").lower()
            if not self._remember_host(host, "escaped inline HTML/JS"):
                return match.group(0)
            prefix = f"{proxy_origin}{self.proxy_path}/{_HOST_MARKER}/{host}"
            return prefix.replace("/", r"\/")

        body = escaped_url_prefix.sub(escaped_repl, body)

        proxy_path_js = json.dumps(self.proxy_path)
        proxy_origin_js = json.dumps(proxy_origin)
        host_js = json.dumps(current_host)
        hosts_js = json.dumps(sorted(_PROXIED_HOSTS))
        hidden_js = json.dumps(_HIDDEN_QUERY)
        shim = f"""<script>
(function(){{
  const po={proxy_origin_js}, pp={proxy_path_js}, ch={host_js}, hosts={hosts_js}, hq={hidden_js};
  function trusted(h){{
    return hosts.includes((h||"").toLowerCase().replace(/\\\\.$/,""));
  }}
  // Same rule as _hide_query_from_filter: Home Assistant's filter would refuse it.
  const refused=/[a-z0-9_]=\\/([a-z0-9_.]\\/\\/?)+|\\.\\.\\/|proc\\/self\\/environ|<.*script.*>|union.*select.*\\(|union.*all.*select|concat.*\\(/i;
  function hide(u){{
    const q=u.indexOf("?");
    if(q<0) return u;
    const f=u.indexOf("#",q), end=f<0?u.length:f;
    let d=u.slice(q+1,end);
    try {{ for(let p;p!==d;) {{ p=d; d=decodeURIComponent(d); }} }} catch(e) {{}}
    if(!refused.test(d)) return u;
    const b=btoa(u.slice(q+1,end)).replace(/\\+/g,"-").replace(/\\//g,"_").replace(/=+$/,"");
    return u.slice(0,q)+"?"+hq+"="+b+u.slice(end);
  }}
  function rw(u){{
    if(typeof u!=="string" || !u) return u;
    if(u.startsWith(po+pp+"/")) return hide(u);
    if(u.startsWith(pp+"/")) return hide(po+u);
    try {{
      const p=new URL(u,"https://"+ch+"/");
      if(!trusted(p.hostname)) return u;
      return hide(po+pp+"/{_HOST_MARKER}/"+p.hostname.toLowerCase()+p.pathname+p.search+p.hash);
    }} catch(e) {{ return u; }}
  }}
  const fo=window.fetch;
  if(fo) window.fetch=function(input,init){{
    if(typeof input==="string") input=rw(input);
    else if(input && input.url) input=new Request(rw(input.url),input);
    return fo.call(this,input,init);
  }};
  const xo=XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open=function(method,url){{
    if(typeof url==="string") arguments[1]=rw(url);
    return xo.apply(this,arguments);
  }};
  if(navigator.sendBeacon){{
    const bo=navigator.sendBeacon.bind(navigator);
    navigator.sendBeacon=function(url,data){{ return bo(rw(String(url)),data); }};
  }}
  const wo=window.open;
  if(wo) window.open=function(url){{
    if(typeof url==="string") arguments[0]=rw(url);
    return wo.apply(this,arguments);
  }};
  try {{
    const la=Location.prototype.assign, lr=Location.prototype.replace;
    Location.prototype.assign=function(url){{ return la.call(this,rw(String(url))); }};
    Location.prototype.replace=function(url){{ return lr.call(this,rw(String(url))); }};
  }} catch(e) {{}}
  // location.href= cannot be wrapped; the Navigation API sees it where available.
  if(window.navigation) navigation.addEventListener("navigate",e=>{{
    if(e.formData || !e.cancelable) return;
    const to=hide(e.destination.url);
    if(to!==e.destination.url){{ e.preventDefault(); location.replace(to); }}
  }});
  for(const n of ["pushState","replaceState"]){{
    try {{
      const ho=history[n].bind(history);
      history[n]=function(state,title,url){{
        return ho(state,title,url==null?url:rw(String(url)));
      }};
    }} catch(e) {{}}
  }}
  function fixOne(el){{
    if(!el || !el.getAttribute) return;
    for(const a of ["href","src","action","formaction","data-url"]){{
      if(el.hasAttribute(a)) el.setAttribute(a,rw(el.getAttribute(a)));
    }}
  }}
  function fix(root){{
    fixOne(root);
    const nodes=(root&&root.querySelectorAll)?root.querySelectorAll("[href],[src],[action],[formaction],[data-url]"):[];
    for(const el of nodes) fixOne(el);
  }}
  document.addEventListener("click",e=>{{
    const a=e.target&&e.target.closest?e.target.closest("a[href]"):null;
    if(a) a.setAttribute("href",rw(a.getAttribute("href")));
  }},true);
  document.addEventListener("submit",e=>{{
    const f=e.target;
    if(f&&f.getAttribute&&f.hasAttribute("action"))
      f.setAttribute("action",rw(f.getAttribute("action")));
  }},true);
  try {{
    const fs=HTMLFormElement.prototype.submit;
    HTMLFormElement.prototype.submit=function(){{ fixOne(this); return fs.call(this); }};
    if(HTMLFormElement.prototype.requestSubmit){{
      const fr=HTMLFormElement.prototype.requestSubmit;
      HTMLFormElement.prototype.requestSubmit=function(){{ fixOne(this); return fr.apply(this,arguments); }};
    }}
  }} catch(e) {{}}
  document.addEventListener("DOMContentLoaded",()=>{{
    fix(document);
    new MutationObserver(ms=>ms.forEach(m=>m.addedNodes.forEach(fix)))
      .observe(document.documentElement,{{subtree:true,childList:true}});
  }});
}})();
</script>"""

        head_match = re.search(r"<head\b[^>]*>", body, re.IGNORECASE)
        if head_match:
            pos = head_match.end()
            return body[:pos] + shim + body[pos:]
        return shim + body


    def _browser_cookie_headers(self, response: httpx.Response, request: web.Request) -> list[str]:
        """Mirror Microsoft cookies onto the scoped HA proxy origin.

        HTTP redirects are deliberately exposed to the browser. Mirror the
        current jar as well as explicit deletion/update Set-Cookie headers for
        every hop so Microsoft's browser JavaScript sees a consistent cookie
        view on the scoped HA proxy origin while the upstream jar remains
        authoritative.

        Only cookies that page JavaScript can read are mirrored. The browser's
        Cookie header is never forwarded upstream (``_HOP_BY_HOP_HEADERS``), so
        an HttpOnly cookie on the proxy origin serves nobody: it only grows the
        Cookie header the browser sends back on every proxied request, and
        Home Assistant's HTTP server rejects any header field above
        ``_BROWSER_COOKIE_HEADER_LIMIT`` bytes before the view runs (#60). The
        same goes for Akamai Bot Manager cookies and for oversized values.
        Those are removed from the browser instead when it still holds them.
        """
        result: list[str] = []
        secure_proxy = request.scheme == "https"
        final_host = (URL(str(response.url)).host or "").lower()
        seen: set[str] = set()
        browser_cookies = self._browser_cookie_names(request)
        excluded: dict[str, str] = {}

        def append_cookie(
            name: str,
            value: str,
            *,
            http_only: bool = False,
            secure: bool = False,
            same_site: str = "",
            max_age: str = "",
            expires: str = "",
        ) -> None:
            # One browser origin cannot preserve Microsoft's domain partitioning.
            # For the active Microsoft host, the most recent value per name is
            # sufficient; the server-side jar remains authoritative upstream.
            key = name.lower()
            if key in seen:
                return
            seen.add(key)
            if http_only:
                excluded[key] = name
                return
            if name in _BOT_MANAGER_COOKIE_NAMES:
                excluded[key] = name
                return
            if len(value) > _BROWSER_COOKIE_VALUE_LIMIT:
                _LOGGER.debug(
                    "Not mirroring Microsoft cookie %s to the proxy origin: %d bytes",
                    name,
                    len(value),
                )
                excluded[key] = name
                return
            parts = [f"{name}={value}", f"Path={self.proxy_path}/"]
            if max_age:
                parts.append(f"Max-Age={max_age}")
            if expires:
                # A deletion sent as "Expires=1970" must delete here too.
                parts.append(f"Expires={expires}")
            if secure_proxy and secure:
                parts.append("Secure")
            same_site = (same_site or "").strip()
            if same_site and not (same_site.lower() == "none" and not secure_proxy):
                parts.append(f"SameSite={same_site}")
            result.append("; ".join(parts))

        # Explicit Set-Cookie headers first so deletes/updates win over the jar.
        try:
            raw_headers = response.headers.get_list("set-cookie")
        except Exception:
            raw = response.headers.get("set-cookie")
            raw_headers = [raw] if raw else []
        for raw in raw_headers:
            if not raw:
                continue
            parsed = SimpleCookie()
            try:
                parsed.load(raw)
            except Exception:
                continue
            for name, morsel in parsed.items():
                append_cookie(
                    name,
                    morsel.value,
                    http_only=bool(morsel["httponly"]),
                    secure=bool(morsel["secure"]),
                    same_site=morsel["samesite"] or "",
                    max_age=morsel["max-age"] or "",
                    expires=morsel["expires"] or "",
                )

        # Include the current server-side cookie jar as well. Redirects are now
        # browser-visible, so each hop is mirrored independently while the
        # authoritative upstream jar preserves Microsoft's real domain scoping.
        for cookie in self._client.cookies.jar:
            domain = (cookie.domain or "").lstrip(".").lower()
            if not domain or not final_host:
                continue
            if not (final_host == domain or final_host.endswith("." + domain)):
                continue
            append_cookie(
                cookie.name,
                cookie.value,
                http_only=bool(cookie.has_nonstandard_attr("HttpOnly")),
                secure=bool(cookie.secure),
                same_site=cookie.get_nonstandard_attr("SameSite") or "",
            )

        # Purge from the browser what should not live on the proxy origin:
        # cookies excluded above that an earlier hop (or an earlier version)
        # mirrored, Akamai cookies that Microsoft's page scripts write
        # client-side, and oversized values. A deletion is only sent for a
        # cookie the browser actually presented, so the response stays small.
        for key, (name, value_length) in browser_cookies.items():
            if key in seen and key not in excluded:
                continue
            if (
                key in excluded
                or name in _BOT_MANAGER_COOKIE_NAMES
                or value_length > _BROWSER_COOKIE_VALUE_LIMIT
            ):
                result.append(f"{name}=; Path={self.proxy_path}/; Max-Age=0")
        return result

    @staticmethod
    def _browser_cookie_names(request: web.Request) -> dict[str, tuple[str, int]]:
        """Return the cookies the browser sent on the proxy origin, by lower-cased name."""
        header = request.headers.get("Cookie") or ""
        found: dict[str, tuple[str, int]] = {}
        for chunk in header.split(";"):
            name, sep, value = chunk.strip().partition("=")
            name = name.strip()
            if not name or not sep:
                continue
            key = name.lower()
            if key not in found:
                found[key] = (name, len(value))
        if len(header) > _BROWSER_COOKIE_HEADER_LIMIT // 2:
            _LOGGER.warning(
                "Browser sends %d bytes of cookies on the Microsoft auth proxy path "
                "(%d names); Home Assistant rejects requests above %d bytes",
                len(header),
                len(found),
                _BROWSER_COOKIE_HEADER_LIMIT,
            )
        return found

    def _response_headers(self, response: httpx.Response) -> Mapping[str, str]:
        result: dict[str, str] = {}
        for key, value in response.headers.items():
            if key.lower() in _RESPONSE_HEADERS_TO_DROP:
                continue
            result[key] = value
        result["Cache-Control"] = "no-store"
        return result

    async def handle(self, request: web.Request) -> web.StreamResponse:
        if self._closed:
            raise web.HTTPGone(text="Authentication flow is closed")

        target = self._target_from_request(request)
        headers = self._request_headers(request, target)
        body = await request.read() if request.can_read_body else None
        proxy_origin = f"{request.scheme}://{request.host}"
        body = self._deproxy_request_body(
            body,
            request.headers.get("Content-Type", ""),
            proxy_origin,
        )

        _LOGGER.debug(
            "Native Family auth proxy request: %s https://%s%s",
            request.method,
            target.host,
            target.path,
        )
        try:
            response = await self._client.request(
                request.method,
                str(target),
                headers=headers,
                content=body if body else None,
            )
        except httpx.TimeoutException as err:
            _LOGGER.warning(
                "Microsoft authentication request timed out: %s https://%s%s: %s",
                request.method,
                target.host,
                target.path,
                err,
            )
            raise web.HTTPGatewayTimeout(text="Microsoft authentication request timed out") from err
        except httpx.HTTPError as err:
            # The host matters: a DNS filter or a missing IPv6 route shows up
            # here as a failure on one Microsoft telemetry or probe host only.
            _LOGGER.warning(
                "Microsoft authentication request failed: %s https://%s%s: %s",
                request.method,
                target.host,
                target.path,
                err,
            )
            raise web.HTTPBadGateway(text="Microsoft authentication request failed") from err

        current = URL(str(response.url))
        if not self._remember_host(current.host, "upstream response"):
            _LOGGER.warning("Microsoft auth response came from unexpected host %s", current.host)
            raise web.HTTPBadGateway(text="Unexpected Microsoft authentication response")
        # Only a navigation moves the "current page": a telemetry XHR finishing
        # just before the sign-in form posts must not become its Referer.
        if request.headers.get("Sec-Fetch-Dest", "document") in ("document", "iframe"):
            self._last_url = current
        _LOGGER.debug(
            "Native Family auth proxy response: HTTP %s https://%s%s",
            response.status_code,
            current.host,
            current.path,
        )

        # Preserve Microsoft's redirect state in the real browser. Silent SSO
        # callbacks rely on browser-visible navigation/location semantics;
        # following redirects inside Home Assistant collapses that state and can
        # leave complete-client-signin-oauth-silent as a blank top-level page.
        if response.status_code in {301, 302, 303, 307, 308}:
            location = response.headers.get("location")
            if not location:
                raise web.HTTPBadGateway(text="Microsoft redirect did not include Location")
            resolved = URL(urljoin(str(current), location))
            if not self._remember_host(resolved.host, "upstream redirect"):
                if not _is_microsoft_host(resolved.host):
                    _LOGGER.warning("Microsoft auth redirected to unexpected host %s", resolved.host)
                    raise web.HTTPBadGateway(text="Unexpected Microsoft authentication redirect")
                # Named so that a sign-in stuck on a new Microsoft host can be reported.
                _LOGGER.info(
                    "Microsoft auth redirect leaves the proxy for %s", resolved.host
                )
                raise web.HTTPFound(location=str(resolved))

            # The mobile API's registered desktop redirect is terminal for this
            # proxy. Capture the code/error before the browser navigates there.
            self._capture_oauth_redirect(resolved, "redirect")

            proxied_location = self._proxyize_url(
                str(resolved),
                str(current),
                proxy_origin,
                allow_relative=False,
                source="upstream redirect",
            )
            if proxied_location == str(resolved):
                raise web.HTTPBadGateway(text="Microsoft redirect could not be proxied")

            outgoing = web.Response(
                status=response.status_code,
                headers=self._response_headers(response),
            )
            outgoing.headers["Location"] = proxied_location
            mirrored = self._browser_cookie_headers(response, request)
            for cookie_header in mirrored:
                outgoing.headers.add("Set-Cookie", cookie_header)
            if mirrored:
                _LOGGER.debug(
                    "Mirrored %d Microsoft auth cookie(s) to scoped proxy origin",
                    len(mirrored),
                )
            _LOGGER.debug(
                "Forwarding Microsoft redirect to browser: HTTP %s https://%s%s",
                response.status_code,
                resolved.host,
                resolved.path,
            )
            return outgoing

        final = current

        # Some Microsoft variants return the desktop redirect as a normal 200
        # document instead of an HTTP redirect.
        self._capture_oauth_redirect(final, "200 response")

        content_type = response.headers.get("content-type", "")
        content = response.content
        if "text/html" in content_type.lower():
            encoding = response.encoding or "utf-8"
            text = content.decode(encoding, errors="replace")
            text = self._rewrite_html(
                text,
                str(final),
                proxy_origin,
            )
            # Add user-facing status UI only to top-level browser documents.
            # Hidden Microsoft iframes also pass through this proxy, so avoid
            # injecting visible controls into those documents.
            sec_fetch_dest = request.headers.get("Sec-Fetch-Dest", "").lower()
            is_top_level_document = sec_fetch_dest in {"", "document"}
            auth_ui = ""
            if is_top_level_document:
                # Keep the auth-proxy status UI in English regardless of browser or
                # Home Assistant locale. This makes screenshots/log-assisted testing
                # consistent across installations.
                if final.host in {
                    "login.live.com",
                    "login.microsoft.com",
                    "login.microsoftonline.com",
                    "account.microsoft.com",
                }:
                    notice_title = "Home Assistant · Microsoft Family Safety"
                    notice_text = (
                        "This window is part of the Home Assistant setup. After the "
                        "Microsoft sign-in, the remaining steps continue automatically. "
                        "Please keep this window open."
                    )
                    auth_ui = (
                        '<style id="ha-family-auth-notice-style">'
                        '@keyframes haFamNoticeSpin{to{transform:rotate(360deg)}}'
                        '#ha-family-auth-notice{position:fixed;z-index:2147483647;'
                        'left:50%;top:22px;transform:translateX(-50%);'
                        'width:min(560px,calc(100vw - 32px));box-sizing:border-box;'
                        'padding:12px 16px;border-radius:12px;background:rgba(255,255,255,.97);'
                        'color:#202124;box-shadow:0 4px 18px rgba(0,0,0,.22);'
                        'font:13px/1.45 sans-serif;pointer-events:none}'
                        '#ha-family-auth-notice-row{display:flex;align-items:center;gap:11px}'
                        '#ha-family-auth-notice-spinner{width:20px;height:20px;flex:0 0 auto;'
                        'border:3px solid #dadce0;border-top-color:#1a73e8;border-radius:50%;'
                        'animation:haFamNoticeSpin .9s linear infinite}'
                        '#ha-family-auth-notice b{display:block;font-size:14px;margin-bottom:2px}'
                        '</style><div id="ha-family-auth-notice" role="status" aria-live="polite">'
                        '<div id="ha-family-auth-notice-row">'
                        '<span id="ha-family-auth-notice-spinner"></span><div><b>'
                        + html.escape(notice_title)
                        + '</b><span>'
                        + html.escape(notice_text)
                        + '</span></div></div></div>'
                    )

            if auth_ui:
                if re.search(r"</body\s*>", text, re.IGNORECASE):
                    text = re.sub(
                        r"</body\s*>", auth_ui + "</body>", text,
                        count=1, flags=re.IGNORECASE,
                    )
                else:
                    text += auth_ui
            content = text.encode("utf-8")
            content_type = re.sub(
                r"charset=[^;]+", "charset=utf-8", content_type, flags=re.IGNORECASE
            )
        elif (
            "application/json" in content_type.lower()
            or "+json" in content_type.lower()
        ):
            content = self._rewrite_json_response(
                content,
                str(final),
                proxy_origin,
            )

        outgoing = web.Response(
            status=response.status_code,
            body=content,
            headers=self._response_headers(response),
            content_type=None,
        )
        mirrored = self._browser_cookie_headers(response, request)
        for cookie_header in mirrored:
            outgoing.headers.add("Set-Cookie", cookie_header)
        if mirrored:
            _LOGGER.debug(
                "Mirrored %d Microsoft auth cookie(s) to scoped proxy origin",
                len(mirrored),
            )
        return outgoing


class MicrosoftFamilyAuthorizationProxyView(HomeAssistantView):
    """Dispatch auth proxy requests by an unguessable per-flow token."""

    url = f"{AUTH_PROXY_PATH}/{{token}}"
    extra_urls = [f"{AUTH_PROXY_PATH}/{{token}}/{{tail:.*}}"]
    name = "api:microsoft_family_safety:auth_proxy"
    requires_auth = False

    async def _handle(
        self,
        request: web.Request,
        token: str | None = None,
        tail: str | None = None,
    ) -> web.StreamResponse:
        """Dispatch a proxied auth request.

        HomeAssistantView passes route variables as keyword arguments. The
        optional ``tail`` argument is intentionally accepted for ``extra_urls``
        even though target resolution reads it from ``request.match_info``.
        """
        hass: HomeAssistant = request.app["hass"]
        token = token or request.match_info.get("token")
        if not token:
            raise web.HTTPBadRequest(text="Missing authentication flow token")
        proxy = hass.data.get(_PROXY_REGISTRY, {}).get(token)
        if not isinstance(proxy, MicrosoftFamilyAuthProxy):
            raise web.HTTPNotFound(text="Authentication flow not found")
        return await proxy.handle(request)

    get = _handle
    post = _handle
    put = _handle
    patch = _handle
    delete = _handle
    options = _handle
    head = _handle


#: How long the frontend keeps being re-notified after the browser callback,
#: matching the proxy lifetime. On Android the Home Assistant app (or tab) is
#: in the background while Chrome shows the sign-in; its websocket is paused or
#: dropped and every flow event fired meanwhile is lost. The old three pings in
#: the first 7 s were all missed, so returning to the app showed a dialog that
#: never moved (#50). Pinging until the flow leaves the step reaches the
#: frontend as soon as its websocket reconnects.
#: Companion-app deep link that brings the Home Assistant app back to the
#: front on the integration page, closing the in-app sign-in browser.
_APP_RETURN_URL = (
    "homeassistant://navigate/config/integrations/integration/" + DOMAIN
)

_RENOTIFY_MAX_SECONDS = 600.0
_RENOTIFY_INTERVAL_SECONDS = 3.0
#: Steps on which the flow waits for the frontend to fetch or configure it.
_FRONTEND_WAIT_STEPS = frozenset({"check_mobile_proxy", "finish_mobile_proxy"})


#: Steps the server may advance by itself once the frontend has left them alone
#: for _AUTO_ADVANCE_AFTER_SECONDS. The Home Assistant Android app closes the
#: setup dialog when it opens the sign-in in its in-app browser, so nobody is
#: left to fetch the next step and the sign-in was lost (#50).
_AUTO_ADVANCE_STEPS = frozenset({"finish_mobile_proxy"})
_AUTO_ADVANCE_AFTER_SECONDS = 20.0


async def _async_renotify_frontend(
    hass: HomeAssistant, flow_id: str, expected_step: str | None
) -> None:
    """Nudge, then if needed drive, a flow the frontend no longer follows.

    First the data_entry_flow_progressed notification is repeated, which is
    enough when the frontend merely missed an event (backgrounded app). If the
    flow then sits on one of _AUTO_ADVANCE_STEPS for
    _AUTO_ADVANCE_AFTER_SECONDS, nobody is following it any more and the
    server advances it itself. The grace period keeps the frontend the normal
    consumer: advancing right away from the callback raced it and produced
    UnknownFlow before.
    """
    from homeassistant.data_entry_flow import (
        EVENT_DATA_ENTRY_FLOW_PROGRESSED,
        UnknownFlow,
    )

    loop = asyncio.get_running_loop()
    deadline = loop.time() + _RENOTIFY_MAX_SECONDS
    delays = iter((0.75, 2.0, 4.0))
    sent = 0
    last_step: str | None = None
    step_since = loop.time()
    while True:
        await asyncio.sleep(next(delays, _RENOTIFY_INTERVAL_SECONDS))
        if loop.time() > deadline:
            _LOGGER.debug(
                "Stopped re-notifying Microsoft Family flow %s after %d events: "
                "still on step %s", flow_id, sent, expected_step,
            )
            return
        try:
            current = hass.config_entries.flow.async_get(flow_id)
        except UnknownFlow:
            return
        step = current.get("step_id")
        if step != expected_step and step not in _FRONTEND_WAIT_STEPS:
            _LOGGER.debug(
                "Microsoft Family flow %s moved to step %s after %d re-notifications",
                flow_id, step, sent,
            )
            return
        now = loop.time()
        if step != last_step:
            last_step, step_since = step, now
        elif (
            step in _AUTO_ADVANCE_STEPS
            and now - step_since >= _AUTO_ADVANCE_AFTER_SECONDS
        ):
            _LOGGER.info(
                "Home Assistant frontend is not following Microsoft Family flow %s "
                "(step %s for %.0fs, typical of the Android app); advancing it "
                "server-side", flow_id, step, now - step_since,
            )
            try:
                await hass.config_entries.flow.async_configure(flow_id, None)
            except UnknownFlow:
                return
            except Exception as err:  # noqa: BLE001 - never crash the helper
                _LOGGER.warning(
                    "Could not advance Microsoft Family flow %s from step %s: %r",
                    flow_id, step, err,
                )
                return
            step_since = loop.time()
            continue
        hass.bus.async_fire_internal(
            EVENT_DATA_ENTRY_FLOW_PROGRESSED,
            {
                "handler": current.get("handler", DOMAIN),
                "flow_id": flow_id,
                "refresh": True,
            },
        )
        sent += 1


def _callback_token_matches(hass: HomeAssistant, flow_id: str, token: str) -> bool:
    """Return whether token is a live proxy token issued for this flow."""
    matches = False
    now = time.monotonic()
    # Every entry is compared: this view has no Home Assistant authentication.
    for known, (known_flow_id, expires) in hass.data.get(_CALLBACK_TOKENS, {}).items():
        if (
            secrets.compare_digest(known.encode(), token.encode())
            and known_flow_id == flow_id
            and now < expires
        ):
            matches = True
    return matches


class MicrosoftFamilyAuthorizationCallbackView(HomeAssistantView):
    """Resume the config flow once Microsoft authentication is complete."""

    url = AUTH_CALLBACK_PATH
    name = "api:microsoft_family_safety:auth_callback"
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        flow_id = request.query.get("flow_id")
        if not flow_id:
            raise web.HTTPBadRequest(text="Missing flow_id")
        if not _callback_token_matches(hass, flow_id, request.query.get("token", "")):
            raise web.HTTPForbidden(text="Invalid authentication callback")
        try:
            try:
                before = hass.config_entries.flow.async_get(flow_id)
                if before.get("handler") != DOMAIN:
                    raise web.HTTPForbidden(text="Invalid authentication callback")
                before_step = before.get("step_id")
                before_source = (before.get("context") or {}).get("source")
            except UnknownFlow:
                before_step = None
                before_source = None

            matching_proxy_summaries = []
            for candidate in hass.data.get(_PROXY_REGISTRY, {}).values():
                if not isinstance(candidate, MicrosoftFamilyAuthProxy):
                    continue
                if f"flow_id={flow_id}" not in candidate.callback_url:
                    continue
                matching_proxy_summaries.append(candidate.diagnostic_summary())

            _LOGGER.debug(
                "Native auth callback entering HA flow: flow_id=%s current_step=%s "
                "source=%s proxies=%s user_agent=%s",
                flow_id,
                before_step,
                before_source,
                matching_proxy_summaries,
                request.headers.get("User-Agent", "")[:160],
            )

            # The browser's sign-in is complete: advance the external step.
            result = await hass.config_entries.flow.async_configure(
                flow_id=flow_id, user_input={"native_callback": True}
            )
        except UnknownFlow as err:
            in_progress = [
                {
                    "flow_id": item.get("flow_id"),
                    "step_id": item.get("step_id"),
                    "source": (item.get("context") or {}).get("source"),
                }
                for item in hass.config_entries.flow.async_progress()
                if item.get("handler") == DOMAIN
            ]
            _LOGGER.error(
                "Could not resume native auth flow %s: UnknownFlow; "
                "current Microsoft Family flows=%s",
                flow_id,
                in_progress,
            )
            raise web.HTTPBadRequest(text="Authentication flow is no longer active") from err
        except web.HTTPException:
            raise
        except Exception as err:  # Flow may already have been closed/expired.
            _LOGGER.exception(
                "Could not resume native auth flow %s (%s)",
                flow_id,
                type(err).__name__,
            )
            raise web.HTTPBadRequest(text="Authentication flow is no longer active") from err

        result_type = (
            getattr(result.get("type"), "value", result.get("type"))
            if isinstance(result, dict)
            else None
        )
        _LOGGER.debug(
            "Native auth callback HA flow result: flow_id=%s result_type=%s "
            "step_id=%s has_redirect_url=%s",
            flow_id,
            result_type,
            result.get("step_id") if isinstance(result, dict) else None,
            bool(result.get("url")) if isinstance(result, dict) else False,
        )

        # An EXTERNAL_STEP -> EXTERNAL_STEP transition means the sign-in has to
        # start over (Microsoft answered the OAuth redirect with an error).
        # Continue in the already-open browser tab; no second popup is required.
        next_url = result.get("url") if isinstance(result, dict) else None
        if next_url:
            _LOGGER.debug(
                "Native Microsoft Family auth callback advancing flow in same browser tab"
            )
            raise web.HTTPFound(location=str(next_url))

        if result_type == "external_done":
            # This is the canonical Home Assistant external-step handoff.
            # async_external_step_done stores the declared next step and the flow
            # manager notifies the frontend because the step id changed. The
            # frontend is then the consumer that advances to finish_mobile_proxy.
            # Never call async_configure() a second time here: the frontend may
            # already have consumed EXTERNAL_STEP_DONE, which races the callback
            # and produces UnknownFlow / "flow not found".
            _LOGGER.debug(
                "Native Microsoft Family callback returned external_done; "
                "waiting for Home Assistant frontend to advance to the declared next step"
            )

            # Some browsers miss the first data_entry_flow_progressed event while
            # focus moves between the Microsoft popup and the HA dialog.  Do not
            # configure the flow a second time (that caused the v34 UnknownFlow
            # race).  Instead, re-send the *notification* only while the flow is
            # still parked on this exact EXTERNAL_STEP_DONE next-step id.  HA's
            # frontend GET then remains the sole consumer of the transition.
            hass.async_create_task(
                _async_renotify_frontend(hass, flow_id, result.get("step_id")),
                "Microsoft Family Safety auth flow frontend re-notify",
            )
        else:
            _LOGGER.info(
                "Native Microsoft Family Home Assistant flow callback returned %s",
                result_type or "unknown",
            )

        _LOGGER.debug(
            "Returning Microsoft Family authentication completion page to browser: "
            "flow_id=%s result_type=%s auto_close=True",
            flow_id, result_type or "unknown",
        )
        return web.Response(
            text=(
                "<!doctype html><html><head><meta charset='utf-8'>"
                "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                "<meta name='color-scheme' content='light dark'>"
                # Explicit colours: the Home Assistant Android app shows this page
                # in its dark in-app browser, which darkened the background but
                # kept the default black text, so the page looked blank.
                "<style>body{font-family:system-ui,sans-serif;margin:24px;"
                "background:#fff;color:#1c1c1c}"
                "@media (prefers-color-scheme:dark){body{background:#111;color:#e8e8e8}}"
                "</style>"
                "<title>Microsoft Family Safety</title></head><body>"
                "<h2>Authentication completed</h2>"
                "<p>Home Assistant has received the authentication result. "
                "This window can now be closed.</p>"
                # The Home Assistant companion apps open this page in an in-app
                # browser that ignores window.close(); going back walks through
                # every Microsoft page again. Their own URL scheme brings the app
                # back to the front instead (#50).
                "<p id='ha-back' style='display:none'><a href='" + _APP_RETURN_URL + "' "
                "style='display:inline-block;padding:12px 18px;border-radius:8px;"
                "background:#03a9f4;color:#fff;text-decoration:none'>"
                "Back to Home Assistant</a></p>"
                # The Android app's in-app browser reports a plain Android WebView
                # ("; wv)"), not "Home Assistant/" (observed 2026-09-25).
                "<script>(function(){var app=/Home ?Assistant\\/|; wv\\)/i.test(navigator.userAgent);"
                "var mobile=app||/Android|iPhone|iPad/i.test(navigator.userAgent);"
                "if(mobile){document.getElementById('ha-back').style.display='block';}"
                "if(app){setTimeout(function(){window.location.replace('" + _APP_RETURN_URL + "');},1500);}"
                "setTimeout(function(){try{window.close();}catch(e){}},300);"
                "setTimeout(function(){var p=document.createElement('p');"
                "p.textContent='If this window did not close automatically, close it "
                "and switch back to Home Assistant: the setup finishes on its own "
                "within about 20 seconds.';"
                "document.body.appendChild(p);},1200);})();</script>"
                "</body></html>"
            ),
            content_type="text/html",
        )


def ensure_native_auth_views(hass: HomeAssistant) -> None:
    """Register the shared proxy/callback views once."""
    if hass.data.get(_VIEWS_REGISTERED):
        return
    hass.http.register_view(MicrosoftFamilyAuthorizationProxyView())
    hass.http.register_view(MicrosoftFamilyAuthorizationCallbackView())
    hass.data[_VIEWS_REGISTERED] = True
    hass.data.setdefault(_PROXY_REGISTRY, {})


def register_native_proxy(hass: HomeAssistant, proxy: MicrosoftFamilyAuthProxy) -> None:
    ensure_native_auth_views(hass)
    hass.data.setdefault(_PROXY_REGISTRY, {})[proxy.token] = proxy
    # Kept apart from the registry: the browser's final callback can arrive after
    # the flow has already closed this proxy.
    now = time.monotonic()
    tokens = hass.data.setdefault(_CALLBACK_TOKENS, {})
    for known in [known for known, (_, expires) in tokens.items() if expires <= now]:
        del tokens[known]
    tokens[proxy.token] = (proxy.flow_id, now + _PROXY_LIFETIME_SECONDS)

    async def expire() -> None:
        await asyncio.sleep(_PROXY_LIFETIME_SECONDS)
        registry = hass.data.get(_PROXY_REGISTRY, {})
        if registry.pop(proxy.token, None) is proxy:
            _LOGGER.debug("Expired native Family auth proxy flow")
        await proxy.async_close()

    proxy._expiry_task = hass.async_create_task(expire())


async def unregister_native_proxy(hass: HomeAssistant, proxy: MicrosoftFamilyAuthProxy) -> None:
    registry = hass.data.get(_PROXY_REGISTRY, {})
    registry.pop(proxy.token, None)
    await proxy.async_close()


_OAUTH_MAX_HOPS = 8


async def async_fetch_oauth_redirect(
    hass: HomeAssistant, start_url: str, cookies: list[dict[str, Any]]
) -> str | None:
    """Drive an OAuth authorization server-side from fresh sign-in cookies.

    Right after a Microsoft account sign-in, ``oauth20_authorize.srf`` (here
    for the Xbox Family Settings client) answers with one redirect straight
    to ``oauth20_desktop.srf?code=...``. Returns that redirect URL, or
    ``None`` when Microsoft insists on an interactive step. The proxy class
    is reused unregistered, purely for its cookie jar; no route is ever
    exposed for it.
    """
    proxy = MicrosoftFamilyAuthProxy(
        hass,
        callback_url="",
        start_url=start_url,
        initial_cookies=cookies,
    )
    try:
        current = URL(start_url)
        for hop in range(_OAUTH_MAX_HOPS):
            if not _is_allowed_host(current.host):
                _LOGGER.debug(
                    "Server-side OAuth authorization left the Microsoft hosts at hop %d (%s)",
                    hop, current.host,
                )
                return None
            response = await proxy._client.get(str(current))
            location = response.headers.get("location")
            if response.status_code in (301, 302, 303, 307, 308) and location:
                target = URL(location)
                current = target if target.is_absolute() else current.join(target)
                if current.host == "login.live.com" and current.path == "/oauth20_desktop.srf":
                    if "code" in current.query:
                        _LOGGER.debug(
                            "OAuth authorization completed server-side in %d hop(s)", hop + 1
                        )
                        return str(current)
                    _LOGGER.debug(
                        "Server-side OAuth authorization ended in an error redirect: error=%s",
                        current.query.get("error", "unknown"),
                    )
                    return None
                continue
            final = URL(str(response.url))
            if (
                final.host == "login.live.com"
                and final.path == "/oauth20_desktop.srf"
                and "code" in final.query
            ):
                return str(final)
            _LOGGER.debug(
                "Server-side OAuth authorization stopped at %s%s status=%s after %d hop(s); "
                "an interactive step is needed",
                final.host, final.path, response.status_code, hop + 1,
            )
            return None
        _LOGGER.debug("Server-side OAuth authorization gave up after %d hops", _OAUTH_MAX_HOPS)
        return None
    finally:
        await proxy.async_close()
