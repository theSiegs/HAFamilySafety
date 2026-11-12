"""Config flow for Microsoft Family Safety integration."""
from __future__ import annotations

import asyncio
import logging
from ipaddress import ip_address
from typing import Any
from urllib.parse import unquote

import voluptuous as vol
from yarl import URL

from homeassistant import config_entries
from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_RECONFIGURE
import homeassistant.helpers.config_validation as cv
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.network import NoURLAvailableError, get_url
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)
from homeassistant.util import dt as dt_util

from .const import (
    AUTH_PROXY_LIFETIME_SECONDS,
    CONF_ALLOWED_USERS,
    CONF_STRICT_MODE,
    DEFAULT_STRICT_MODE,
    CONF_APP_USAGE_SENSORS,
    CONF_XBOX_FEATURES,
    CONF_XBOX_LINK,
    CONF_XBOX_REFRESH_TOKEN,
    DEFAULT_APP_USAGE_SENSORS,
    DEFAULT_XBOX_FEATURES,
    LEGACY_WEB_AUTH_KEYS,
    AVAILABLE_PLATFORMS,
    CONF_ALLOW_INSECURE_HTTP_AUTH,
    CONF_AUTH_USER_ID,
    CONF_PLATFORMS,
    CONF_REDIRECT_URL,
    CONF_REFRESH_TOKEN,
    CONF_SIGNED_IN_AT,
    CONF_UPDATE_INTERVAL,
    DEFAULT_PLATFORMS,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    ERROR_AUTH_FAILED,
    INTEGRATION_NAME,
    MS_AUTH_PARAMS,
    MS_LOGIN_URL,
)

_LOGGER = logging.getLogger(__name__)


def _build_auth_url() -> str:
    """Build the Microsoft Family Safety mobile OAuth URL."""
    return str(URL(MS_LOGIN_URL).with_query(MS_AUTH_PARAMS))


async def validate_redirect_url(hass: HomeAssistant, redirect_url: str) -> dict[str, Any]:
    """Exchange the intercepted Microsoft OAuth redirect for a refresh token."""
    try:
        from ._pyfamilysafety_compat import create_authenticator

        redirect_url = unquote(redirect_url)
        parsed_redirect = URL(redirect_url)
        _LOGGER.debug(
            "Validating captured Microsoft Family OAuth redirect: host=%s path=%s "
            "has_code=%s has_error=%s query_keys=%s",
            parsed_redirect.host,
            parsed_redirect.path,
            "code" in parsed_redirect.query,
            "error" in parsed_redirect.query,
            sorted(parsed_redirect.query.keys()),
        )
        authenticator = await create_authenticator(
            hass,
            redirect_url,
            use_refresh_token=False,
        )
        if not authenticator.refresh_token:
            _LOGGER.warning(
                "Microsoft Family OAuth redirect validated but no refresh token was returned"
            )
            raise InvalidAuth("Microsoft did not return a refresh token")
        _LOGGER.debug(
            "Microsoft Family OAuth redirect validation succeeded: user_id_present=%s "
            "refresh_token_present=%s",
            bool(authenticator.user_id),
            bool(authenticator.refresh_token),
        )
        return {
            "title": INTEGRATION_NAME,
            "refresh_token": authenticator.refresh_token,
            "user_id": str(authenticator.user_id or ""),
        }
    except InvalidAuth:
        raise
    except Exception as err:
        _LOGGER.error("Authentication validation failed: %s", err)
        raise InvalidAuth(f"Cannot connect: {err}") from err


async def _allowed_users_selector(
    hass: HomeAssistant, current: list[str] | None
) -> tuple[SelectSelector, list[str]]:
    """Selector of the people of this Home Assistant, and the saved choice still valid.

    System accounts (Supervisor, add-ons, cloud) and deactivated users are left
    out. The saved list is filtered on the users that still exist, otherwise
    the selector refuses an id that was deleted and the form cannot be saved.
    """
    users = [
        u for u in await hass.auth.async_get_users()
        if not u.system_generated and u.is_active
    ]
    options = [
        SelectOptionDict(
            value=u.id,
            label=f"{u.name} (administrator)" if u.is_admin else (u.name or u.id),
        )
        for u in sorted(users, key=lambda u: (u.name or "").lower())
    ]
    known = {u.id for u in users}
    selected = [user_id for user_id in (current or []) if user_id in known]
    return (
        SelectSelector(
            SelectSelectorConfig(
                options=options, multiple=True, mode=SelectSelectorMode.LIST
            )
        ),
        selected,
    )


def _wants_xbox(platforms: Any) -> bool:
    """Ticking Xbox among the monitored platforms turns the Xbox features on.

    They are the Xbox entities (Xbox lock, Xbox schedule and limits) and the
    Xbox Family Settings link (see _mobile_only).
    """
    return any(str(p).lower() == "xbox" for p in (platforms or []))


def _is_local_http_url(url: URL) -> bool:
    """Return True only for HTTP URLs that are clearly local/private."""
    if url.scheme != "http" or not url.host:
        return False
    host = url.host.lower().rstrip(".")
    if host in {"localhost", "homeassistant", "homeassistant.local"} or host.endswith(".local"):
        return True
    try:
        address = ip_address(host)
    except ValueError:
        return False
    return address.is_private or address.is_loopback or address.is_link_local


def _browser_hass_url(hass: HomeAssistant, allow_insecure_http: bool) -> str:
    """Return a browser-reachable HA URL for a temporary sign-in proxy.

    Used by the main sign-in and by the Xbox link. HTTPS is always preferred.
    Plain HTTP is permitted only when the user explicitly opted in and the URL
    is clearly local/private. Raises ``NativeAuthLocalHttpRequired`` when the
    opt-in is on but no URL is local, ``NativeAuthHttpsRequired`` otherwise.
    """
    candidates: list[str] = []
    for kwargs in ({"prefer_external": True}, {"allow_external": False}):
        try:
            candidate = get_url(hass, **kwargs)
        except NoURLAvailableError:
            continue
        if candidate not in candidates:
            candidates.append(candidate)

    for candidate in candidates:
        if URL(candidate).scheme == "https":
            return candidate

    if allow_insecure_http:
        for candidate in reversed(candidates):
            url = URL(candidate)
            if _is_local_http_url(url):
                _LOGGER.warning(
                    "Microsoft Family Safety authentication is using an "
                    "UNENCRYPTED local Home Assistant HTTP proxy at %s. "
                    "Microsoft credentials and session data are not protected "
                    "against interception on the local network.",
                    url.host,
                )
                return candidate
        raise NativeAuthLocalHttpRequired

    raise NativeAuthHttpsRequired


class FamilySafetyConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Microsoft Family Safety."""

    VERSION = 1

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> FamilySafetyOptionsFlow:
        """Get the options flow for this handler."""
        return FamilySafetyOptionsFlow(config_entry)

    def __init__(self) -> None:
        self._pending_data: dict[str, Any] | None = None
        self._pending_options: dict[str, Any] | None = None
        self._pending_title: str = INTEGRATION_NAME
        self._mobile_proxy: Any | None = None
        #: Existing entry of the same Microsoft account found by a user flow;
        #: the sign-in then updates it instead of aborting (#50).
        self._update_entry: config_entries.ConfigEntry | None = None
        self._allow_insecure_http_auth: bool = False
        #: Token exchange (and Xbox link) of a completed browser sign-in; see
        #: async_step_finish_mobile_proxy.
        self._finish_task: asyncio.Task[None] | None = None
        self._finish_abort: str | None = None

    def _is_existing_entry_auth_flow(self) -> bool:
        """Return whether this flow updates an existing config entry."""
        return self.source in (SOURCE_REAUTH, SOURCE_RECONFIGURE)

    def _get_existing_entry(self):
        """Return the config entry targeted by reauth or reconfigure."""
        if self.source == SOURCE_REAUTH:
            return self._get_reauth_entry()
        if self.source == SOURCE_RECONFIGURE:
            return self._get_reconfigure_entry()
        raise HomeAssistantError(
            f"No existing config entry is associated with {self.source!r} flow"
        )

    def _browser_hass_url(self) -> str:
        """Return a browser-reachable HA URL for the temporary auth proxy."""
        return _browser_hass_url(self.hass, self._allow_insecure_http_auth)

    def _user_schema(self, user_input: dict[str, Any] | None = None) -> vol.Schema:
        """Build the initial setup schema."""
        values = user_input or {}
        return vol.Schema(
            {
                vol.Optional(
                    CONF_UPDATE_INTERVAL,
                    default=values.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL),
                ): vol.All(vol.Coerce(int), vol.Range(min=30, max=3600)),
                vol.Optional(
                    CONF_PLATFORMS,
                    default=values.get(CONF_PLATFORMS, DEFAULT_PLATFORMS),
                ): cv.multi_select({p: p for p in AVAILABLE_PLATFORMS}),
                vol.Optional(
                    CONF_ALLOW_INSECURE_HTTP_AUTH,
                    default=values.get(CONF_ALLOW_INSECURE_HTTP_AUTH, False),
                ): cv.boolean,
            }
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Collect basic options, then start browser-assisted Microsoft OAuth."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._allow_insecure_http_auth = bool(
                user_input.get(CONF_ALLOW_INSECURE_HTTP_AUTH, False)
            )
            self._pending_options = {
                CONF_UPDATE_INTERVAL: user_input.get(
                    CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL
                ),
                CONF_PLATFORMS: user_input.get(CONF_PLATFORMS, DEFAULT_PLATFORMS),
                CONF_XBOX_FEATURES: _wants_xbox(
                    user_input.get(CONF_PLATFORMS, DEFAULT_PLATFORMS)
                ),
                CONF_ALLOW_INSECURE_HTTP_AUTH: self._allow_insecure_http_auth,
            }
            try:
                return await self.async_step_start_mobile_auth()
            except NativeAuthLocalHttpRequired:
                errors["base"] = "native_http_not_local"
            except NativeAuthHttpsRequired:
                # The mobile token is all the integration needs, so the manual
                # redirect step is enough on an HTTP-only instance.
                return await self.async_step_auth()

        return self.async_show_form(
            step_id="user",
            data_schema=self._user_schema(user_input),
            # Compatibility placeholders for clients that still have the pre-v5
            # translation cached. The current v5+ text does not reference them.
            description_placeholders={
                "auth_url": "automatically handled by Home Assistant",
                "addon_status": "native Home Assistant browser authentication",
            },
            errors=errors,
        )

    async def async_step_start_mobile_auth(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Start the browser-assisted Microsoft sign-in.

        The proxy serves the mobile OAuth sign-in of the Family Safety app
        (client 000000000004893A) and captures its ``oauth20_desktop.srf``
        redirect, whose code becomes the long-lived mobile refresh token.
        """
        hass_url = URL(self._browser_hass_url())
        try:
            from .auth.native_proxy import (
                AUTH_CALLBACK_PATH,
                MicrosoftFamilyAuthProxy,
                register_native_proxy,
            )
        except Exception as err:
            _LOGGER.exception("Native Family Safety auth module could not be loaded")
            return self.async_abort(
                reason="native_auth_unavailable",
                description_placeholders={"error": str(err)},
            )

        callback_url = str(
            hass_url.with_path(AUTH_CALLBACK_PATH).with_query({"flow_id": self.flow_id})
        )
        self._mobile_proxy = MicrosoftFamilyAuthProxy(
            self.hass,
            callback_url=callback_url,
            start_url=_build_auth_url(),
        )
        register_native_proxy(self.hass, self._mobile_proxy)
        proxy_url = str(hass_url.with_path(self._mobile_proxy.access_path))
        _LOGGER.debug("Starting native Microsoft Family mobile OAuth proxy")
        return self.async_external_step(step_id="check_mobile_proxy", url=proxy_url)

    def _mobile_only(self) -> bool:
        """Return whether this flow is for an entry without the Xbox features.

        Such an entry does not link the Xbox Family Settings sign-in.
        """
        if self._is_existing_entry_auth_flow():
            options = self._get_existing_entry().options
        else:
            options = self._pending_options or {}
        return not options.get(CONF_XBOX_FEATURES, DEFAULT_XBOX_FEATURES)

    async def async_step_check_mobile_proxy(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Wait on the external step until the browser sign-in has completed."""
        proxy = self._mobile_proxy
        _LOGGER.debug(
            "Native auth flow %s handling mobile phase: proxy_present=%s "
            "complete=%s redirect_present=%s",
            self.flow_id,
            proxy is not None,
            bool(proxy and proxy.complete),
            bool(proxy and proxy.oauth_redirect_url),
        )
        if proxy is not None and not (proxy.complete and proxy.oauth_redirect_url):
            return self.async_external_step(
                step_id="check_mobile_proxy",
                url=str(URL(self._browser_hass_url()).with_path(proxy.access_path)),
            )
        return self.async_external_step_done(next_step_id="finish_mobile_proxy")

    async def _apply_mobile_oauth_info(self, info: dict[str, Any]) -> str | None:
        """Turn a validated mobile OAuth result into pending entry data.

        Returns an abort reason when the result cannot be used (a different
        Microsoft account during reauth), ``None`` otherwise.
        """
        refresh_token = info["refresh_token"]
        new_user_id = str(info.get("user_id") or "")

        if self._is_existing_entry_auth_flow():
            entry = self._get_existing_entry()
            old_user_id = str(entry.data.get(CONF_AUTH_USER_ID, ""))
            if old_user_id and new_user_id and old_user_id != new_user_id:
                return "wrong_account"

            self._prepare_existing_entry_update(entry, refresh_token, new_user_id)
            return None

        unique_id = new_user_id or refresh_token[:20]
        # A flow left open for the same account (typically the reauth started
        # by an existing entry) must not turn a successful sign-in into
        # "already_in_progress" (#50): the existing entry is updated instead.
        await self.async_set_unique_id(unique_id, raise_on_progress=False)
        existing = self.hass.config_entries.async_entry_for_domain_unique_id(
            DOMAIN, unique_id
        )
        if existing is not None:
            _LOGGER.info(
                "Microsoft account already configured; the new sign-in will "
                "update the existing entry instead of creating a second one"
            )
            self._update_entry = existing
            self._prepare_existing_entry_update(existing, refresh_token, new_user_id)
            return None

        data: dict[str, Any] = {
            CONF_REFRESH_TOKEN: refresh_token,
            CONF_SIGNED_IN_AT: dt_util.utcnow().isoformat(),
        }
        if new_user_id:
            data[CONF_AUTH_USER_ID] = new_user_id

        self._pending_data = data
        self._pending_title = info["title"]
        return None

    def _prepare_existing_entry_update(
        self, entry: config_entries.ConfigEntry, refresh_token: str, user_id: str
    ) -> None:
        """Build pending data that renews ``entry`` with the new refresh token."""
        new_data: dict[str, Any] = {
            CONF_REFRESH_TOKEN: refresh_token,
            CONF_SIGNED_IN_AT: dt_util.utcnow().isoformat(),
        }
        if user_id:
            new_data[CONF_AUTH_USER_ID] = user_id

        self._pending_data = new_data
        merged_options = dict(entry.options)
        if self._pending_options:
            merged_options.update(self._pending_options)
        self._pending_options = merged_options
        self._allow_insecure_http_auth = bool(
            merged_options.get(CONF_ALLOW_INSECURE_HTTP_AUTH, False)
        )
        self._pending_title = entry.title

    async def _async_xbox_token_from_sign_in(
        self, cookies: list[dict[str, Any]]
    ) -> str | None:
        """Link the Xbox Family Settings client from the sign-in just completed.

        With the Microsoft account cookies of that sign-in, its
        ``oauth20_authorize.srf`` answers with a single redirect carrying the
        code, without another sign-in page. Only tried for an entry with the
        Xbox features whose Xbox link is missing or refused; the cookies are
        used for this one request and never stored. Returns the refresh token,
        or None when the link has to be made from the integration options.
        """
        if self._mobile_only() or not cookies:
            return None
        entry = self._update_entry
        if entry is None and self._is_existing_entry_auth_flow():
            entry = self._get_existing_entry()
        if entry is not None and await self._async_xbox_link_usable(entry):
            return None
        from .auth.native_proxy import async_fetch_oauth_redirect
        from .xbox_family_api import XboxFamilyAPI, build_authorize_url

        url, verifier = build_authorize_url()
        try:
            result = await async_fetch_oauth_redirect(self.hass, url, cookies)
            if result is None:
                _LOGGER.info(
                    "Xbox Family Settings could not be linked from the sign-in; link it "
                    "from the integration options"
                )
                return None
            body = await XboxFamilyAPI(self.hass, "sign_in").async_exchange_redirect(
                result, verifier
            )
        except Exception as err:  # noqa: BLE001 - optional, the options can link it
            _LOGGER.info("Linking Xbox Family Settings from the sign-in failed: %r", err)
            return None
        _LOGGER.info("Xbox Family Settings linked from the sign-in")
        return str(body["refresh_token"])

    async def _async_xbox_link_usable(self, entry: config_entries.ConfigEntry) -> bool:
        """Whether ``entry`` already has an Xbox link the sign-in must not replace.

        The link may be another parent's, made from the options. A loaded
        entry knows whether Microsoft refused it. An entry whose setup failed
        (its mobile token refused at startup, before this reauth) has no
        coordinator, so its stored link is read here: a stored token counts as
        usable, since a refusal is only known once the token has been tried.
        """
        from .xbox_family_api import XboxFamilyAPI

        coordinator = self.hass.data.get(DOMAIN, {}).get(entry.entry_id)
        xbox_family = getattr(coordinator, "xbox_family", None)
        if xbox_family is None:
            xbox_family = XboxFamilyAPI(self.hass, entry.entry_id)
            await xbox_family.async_load()
        return xbox_family.usable

    async def async_step_finish_mobile_proxy(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Exchange the captured OAuth code (and link Xbox) as one tracked task.

        This step is entered several times for one sign-in: by the frontend,
        by the re-notification helper, and by Home Assistant when the task
        ends. Home Assistant has no per-flow lock, so a second entry while the
        exchange is still running must attach to it: starting again would find
        the proxy gone and abort a sign-in that is about to succeed (the
        single-use code is already spent by then).
        """
        if self._finish_task is None:
            proxy = self._mobile_proxy
            if proxy is None or not proxy.complete or not proxy.oauth_redirect_url:
                return self.async_abort(reason="native_oauth_failed")
            self._mobile_proxy = None
            self._finish_task = self.hass.async_create_task(
                self._async_complete_sign_in(
                    proxy, proxy.oauth_redirect_url, proxy.export_cookies()
                ),
                "Microsoft Family Safety sign-in completion",
            )
        if not self._finish_task.done():
            return self.async_show_progress(
                step_id="finish_mobile_proxy",
                progress_action="sign_in",
                progress_task=self._finish_task,
            )
        return self.async_show_progress_done(next_step_id="finish_sign_in")

    async def _async_complete_sign_in(
        self, proxy: Any, redirect_url: str, cookies: list[dict[str, Any]]
    ) -> None:
        """Turn the captured code into pending entry data; failures become an abort."""
        try:
            from .auth.native_proxy import unregister_native_proxy

            await unregister_native_proxy(self.hass, proxy)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Could not close mobile OAuth proxy cleanly: %s", err)
        try:
            info = await validate_redirect_url(self.hass, redirect_url)
            self._finish_abort = await self._apply_mobile_oauth_info(info)
            if self._finish_abort is None and (
                xbox_token := await self._async_xbox_token_from_sign_in(cookies)
            ):
                assert self._pending_data is not None
                self._pending_data[CONF_XBOX_REFRESH_TOKEN] = xbox_token
        except InvalidAuth:
            self._finish_abort = "native_oauth_failed"
        except Exception:  # noqa: BLE001 - surface as an abort, never a stuck flow
            _LOGGER.exception("Completing the Microsoft sign-in failed")
            self._finish_abort = "native_oauth_failed"

    async def async_step_finish_sign_in(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Store the result of the completed sign-in."""
        if self._finish_abort:
            return self.async_abort(reason=self._finish_abort)
        return self._async_store_sign_in()

    def _async_store_sign_in(self) -> FlowResult:
        """Create the entry, or update the one this sign-in renews."""
        if self._pending_data is None:
            return self.async_abort(reason="native_auth_state_lost")
        if self._is_existing_entry_auth_flow():
            return self._async_finish_reauth()
        if self._update_entry is not None:
            return self._async_finish_existing_account_update()
        return self.async_create_entry(
            title=self._pending_title,
            data=dict(self._pending_data),
            options=self._pending_options or {},
        )

    @staticmethod
    def _renewed_data(
        entry: config_entries.ConfigEntry, pending: dict[str, Any]
    ) -> dict[str, Any]:
        """The entry's data with the new sign-in, without any earlier web session."""
        data = {k: v for k, v in entry.data.items() if k not in LEGACY_WEB_AUTH_KEYS}
        data.update(pending)
        return data

    def _async_finish_existing_account_update(self) -> FlowResult:
        """Store a user-flow sign-in into the account's existing entry (#50).

        Other flows still open for the same account (usually the reauth the
        entry started) are aborted: their purpose is fulfilled and leaving
        them would keep a stale repair prompt around.
        """
        entry = self._update_entry
        assert entry is not None and self._pending_data is not None
        for flow in self._async_in_progress(include_uninitialized=True):
            if flow["flow_id"] != self.flow_id and flow["context"].get(
                "unique_id"
            ) == entry.unique_id:
                self.hass.config_entries.flow.async_abort(flow["flow_id"])
        _LOGGER.info(
            "Microsoft Family Safety sign-in completed for an already configured "
            "account; updating and reloading the existing entry"
        )
        # The update listener only reloads on an options change; new credentials
        # need one too, and core no longer lets the helper do it for us (2026.12).
        result = self.async_update_and_abort(
            entry,
            data=self._renewed_data(entry, self._pending_data),
            options=self._pending_options or entry.options,
            reason="reauth_successful",
        )
        self.hass.config_entries.async_schedule_reload(entry.entry_id)
        return result

    def _async_finish_reauth(self) -> FlowResult:
        """Store the renewed credentials of a reauthentication or reconfigure."""
        if self._pending_data is None:
            return self.async_abort(reason="native_auth_state_lost")

        entry = self._get_existing_entry()
        _LOGGER.info(
            "Microsoft Family Safety %s completed; updating and reloading config entry",
            "reconfiguration" if self.source == SOURCE_RECONFIGURE else "reauthentication",
        )
        # The update listener only reloads on an options change; new credentials
        # need one too, and core no longer lets the helper do it for us.
        result = self.async_update_and_abort(
            entry,
            data=self._renewed_data(entry, self._pending_data),
            options=self._pending_options or entry.options,
        )
        self.hass.config_entries.async_schedule_reload(entry.entry_id)
        return result

    async def async_step_auth(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manual OAuth fallback for HTTP-only Home Assistant installations.

        Serves new entries, reauthentication and reconfigure alike, with the
        same account checks as the browser sign-in. There are no sign-in
        cookies here, so the Xbox Family Settings link is left to the options.
        """
        errors: dict[str, str] = {}
        if user_input is not None:
            redirect_url = user_input.get(CONF_REDIRECT_URL, "").strip()
            if not redirect_url:
                errors["base"] = "no_redirect_url"
            else:
                try:
                    info = await validate_redirect_url(self.hass, redirect_url)
                except InvalidAuth:
                    errors["base"] = ERROR_AUTH_FAILED
                except Exception:
                    _LOGGER.exception("Unexpected exception during manual authentication")
                    errors["base"] = "unknown"
                else:
                    if reason := await self._apply_mobile_oauth_info(info):
                        return self.async_abort(reason=reason)
                    return self._async_store_sign_in()

        return self.async_show_form(
            step_id="auth",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_REDIRECT_URL): str,
                }
            ),
            description_placeholders={"auth_url": _build_auth_url()},
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manually renew Microsoft credentials from HA's Reconfigure action."""
        entry = self._get_reconfigure_entry()
        self._pending_options = dict(entry.options)
        self._allow_insecure_http_auth = bool(
            entry.options.get(CONF_ALLOW_INSECURE_HTTP_AUTH, False)
        )
        self._pending_title = entry.title
        return await self.async_step_reauth_full_confirm(user_input)

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> FlowResult:
        """Begin a linked Home Assistant reauthentication flow."""
        entry = self._get_existing_entry()
        self._pending_options = dict(entry.options)
        self._allow_insecure_http_auth = bool(
            entry.options.get(CONF_ALLOW_INSECURE_HTTP_AUTH, False)
        )
        self._pending_title = entry.title
        # Use a new step id so stale frontend translations from the prototype
        # cannot inject the old manual-{auth_url} text into reauth.
        return await self.async_step_reauth_full_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Compatibility alias for reauth flows started by older test builds."""
        return await self.async_step_reauth_full_confirm(user_input)

    async def async_step_reauth_full_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Confirm and start the Microsoft sign-in again."""
        entry = self._get_existing_entry()
        current_allow_http = bool(
            entry.options.get(CONF_ALLOW_INSECURE_HTTP_AUTH, False)
        )
        self._pending_title = entry.title

        if user_input is None:
            return self.async_show_form(
                step_id="reauth_full_confirm",
                data_schema=vol.Schema(
                    {
                        vol.Optional(
                            CONF_ALLOW_INSECURE_HTTP_AUTH,
                            default=current_allow_http,
                        ): cv.boolean,
                    }
                ),
                # Compatibility placeholders are deliberately supplied even though
                # the current translation does not reference them. This prevents
                # FORMATJS MISSING_VALUE failures if a browser still has one of the
                # pre-v10 translations cached.
                description_placeholders={
                    "auth_url": _build_auth_url(),
                    "addon_status": "native Home Assistant browser authentication",
                },
            )

        self._allow_insecure_http_auth = bool(
            user_input.get(CONF_ALLOW_INSECURE_HTTP_AUTH, current_allow_http)
        )
        self._pending_options = dict(entry.options)
        self._pending_options[CONF_ALLOW_INSECURE_HTTP_AUTH] = (
            self._allow_insecure_http_auth
        )

        try:
            return await self.async_step_start_mobile_auth()
        except NativeAuthLocalHttpRequired:
            return self.async_abort(reason="native_http_not_local")
        except NativeAuthHttpsRequired:
            # The mobile token is all the entry needs: the manual step works.
            return await self.async_step_auth()


#: How long the Xbox link waits for the browser sign-in before offering the
#: manual paste step instead: as long as the proxy stays open, so a parent
#: still busy with a second factor is not switched to the paste step while
#: the sign-in is open.
XBOX_LINK_PROXY_TIMEOUT = AUTH_PROXY_LIFETIME_SECONDS


def _options_browser_hass_url(hass: HomeAssistant, allow_insecure_http: bool) -> str | None:
    """The Home Assistant URL for the Xbox link proxy, or None.

    Same rule as the main sign-in (``_browser_hass_url``). None sends the
    caller to the manual paste step.
    """
    try:
        return _browser_hass_url(hass, allow_insecure_http)
    except (NativeAuthHttpsRequired, NativeAuthLocalHttpRequired):
        return None


class FamilySafetyOptionsFlow(config_entries.OptionsFlow):
    """Handle options for Microsoft Family Safety."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize options flow."""
        self._config_entry = config_entry
        self._pending_options: dict[str, Any] = {}
        self._xbox_link_url: str | None = None
        self._xbox_verifier: str | None = None
        self._xbox_proxy: Any = None
        self._xbox_link_task: asyncio.Task[None] | None = None
        self._xbox_progress_url: str | None = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            data = dict(user_input)
            link_xbox = bool(data.pop(CONF_XBOX_LINK, False))
            data[CONF_XBOX_FEATURES] = _wants_xbox(data.get(CONF_PLATFORMS))
            if link_xbox and data[CONF_XBOX_FEATURES]:
                self._pending_options = data
                return await self.async_step_xbox_link()
            return self.async_create_entry(title="", data=data)
        current_interval = self._config_entry.options.get(
            CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL
        )
        current_platforms = self._config_entry.options.get(
            CONF_PLATFORMS, DEFAULT_PLATFORMS
        )
        current_allow_insecure_http_auth = self._config_entry.options.get(
            CONF_ALLOW_INSECURE_HTTP_AUTH, False
        )
        # Entries created before the option keep the Xbox features until the
        # user unticks Xbox here.
        if self._config_entry.options.get(
            CONF_XBOX_FEATURES, DEFAULT_XBOX_FEATURES
        ) and not _wants_xbox(current_platforms):
            current_platforms = [*current_platforms, "Xbox"]
        users_selector, current_allowed = await _allowed_users_selector(
            self.hass, self._config_entry.options.get(CONF_ALLOWED_USERS)
        )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_UPDATE_INTERVAL,
                        default=current_interval,
                    ): vol.All(vol.Coerce(int), vol.Range(min=30, max=3600)),
                    vol.Optional(
                        CONF_PLATFORMS,
                        default=current_platforms,
                    ): cv.multi_select(
                        {p: p for p in AVAILABLE_PLATFORMS}
                    ),
                    vol.Optional(
                        CONF_ALLOWED_USERS,
                        default=current_allowed,
                    ): users_selector,
                    vol.Optional(
                        CONF_STRICT_MODE,
                        default=self._config_entry.options.get(
                            CONF_STRICT_MODE, DEFAULT_STRICT_MODE
                        ),
                    ): cv.boolean,
                    vol.Optional(
                        CONF_APP_USAGE_SENSORS,
                        default=self._config_entry.options.get(
                            CONF_APP_USAGE_SENSORS, DEFAULT_APP_USAGE_SENSORS
                        ),
                    ): vol.All(vol.Coerce(int), vol.Range(min=0, max=30)),
                    vol.Optional(
                        CONF_ALLOW_INSECURE_HTTP_AUTH,
                        default=current_allow_insecure_http_auth,
                    ): cv.boolean,
                    vol.Optional(CONF_XBOX_LINK, default=False): cv.boolean,
                }
            ),
        )

    async def async_step_xbox_link(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Link the Xbox Family Settings sign-in through the native OAuth proxy.

        The user signs in once in the browser, through the same temporary proxy
        as the main sign-in, which captures the oauth20_desktop.srf code itself:
        there is no address to copy. The browser callback only shows a close
        page for an options flow; this step's own progress task notices the
        captured code. Without a usable Home Assistant URL, or when the sign-in
        does not complete, the manual paste step takes over.
        """
        if self._xbox_link_task is None:
            from .auth.native_proxy import (
                AUTH_CALLBACK_PATH,
                MicrosoftFamilyAuthProxy,
                register_native_proxy,
            )
            from .xbox_family_api import build_authorize_url

            hass_url = _options_browser_hass_url(
                self.hass,
                bool(self._pending_options.get(CONF_ALLOW_INSECURE_HTTP_AUTH)),
            )
            if hass_url is None:
                _LOGGER.info(
                    "No HTTPS Home Assistant URL for the Xbox link proxy; "
                    "using the manual paste step"
                )
                return await self.async_step_xbox_link_manual()
            auth_url, self._xbox_verifier = build_authorize_url()
            callback_url = str(
                URL(hass_url)
                .with_path(AUTH_CALLBACK_PATH)
                .with_query({"flow_id": self.flow_id})
            )
            self._xbox_proxy = MicrosoftFamilyAuthProxy(
                self.hass,
                callback_url=callback_url,
                start_url=auth_url,
            )
            register_native_proxy(self.hass, self._xbox_proxy)
            self._xbox_progress_url = str(
                URL(hass_url).with_path(self._xbox_proxy.access_path)
            )
            self._xbox_link_task = self.hass.async_create_task(
                self._async_wait_xbox_redirect()
            )
        if not self._xbox_link_task.done():
            # Also when the frontend asks again while the sign-in is still
            # open (the app coming back from the browser): keep waiting.
            return self.async_show_progress(
                step_id="xbox_link",
                progress_action="xbox_link",
                description_placeholders={"url": self._xbox_progress_url or ""},
                progress_task=self._xbox_link_task,
            )
        return self.async_show_progress_done(next_step_id="finish_xbox_proxy")

    async def _async_wait_xbox_redirect(self) -> None:
        """Wait until the proxy saw Microsoft's redirect (a code or an error)."""
        proxy = self._xbox_proxy
        deadline = self.hass.loop.time() + XBOX_LINK_PROXY_TIMEOUT
        while proxy is not None and proxy.oauth_redirect_url is None:
            if self.hass.loop.time() > deadline:
                _LOGGER.info("Xbox link proxy timed out waiting for the sign-in")
                return
            await asyncio.sleep(1)

    async def async_step_finish_xbox_proxy(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Link from the code the proxy captured, or fall back to the paste step."""
        from .xbox_family_api import XboxFamilyAPI, XboxFamilyError

        proxy = self._xbox_proxy
        redirect_url = proxy.oauth_redirect_url if proxy and proxy.complete else None
        await self._async_close_xbox_proxy()
        self._xbox_link_task = None
        if not (redirect_url and self._xbox_verifier):
            _LOGGER.info(
                "The Xbox link proxy saw no sign-in code (%s); using the manual paste step",
                "error redirect" if redirect_url or (proxy and proxy.oauth_redirect_url)
                else "no redirect",
            )
            return await self.async_step_xbox_link_manual()
        # As in the manual step: through the running coordinator's instance, so
        # the new token is used at once.
        coordinator = self.hass.data.get(DOMAIN, {}).get(self._config_entry.entry_id)
        api = getattr(coordinator, "xbox_family", None) or XboxFamilyAPI(
            self.hass, self._config_entry.entry_id
        )
        try:
            await api.async_link_from_redirect(redirect_url, self._xbox_verifier, "proxy")
        except XboxFamilyError as err:
            _LOGGER.warning("Xbox Family Settings link via the proxy failed: %s", err)
            return await self.async_step_xbox_link_manual()
        if coordinator is not None:
            coordinator.async_xbox_linked()
        return self.async_create_entry(title="", data=self._pending_options)

    async def _async_close_xbox_proxy(self) -> None:
        """Close the Xbox link proxy, if one is open."""
        from .auth.native_proxy import unregister_native_proxy

        proxy, self._xbox_proxy = self._xbox_proxy, None
        if proxy is None:
            return
        try:
            await unregister_native_proxy(self.hass, proxy)
        except Exception as err:  # noqa: BLE001 - cleanup only
            _LOGGER.debug("Could not close the Xbox link proxy cleanly: %s", err)

    @callback
    def async_remove(self) -> None:
        """Close the proxy when the dialog is closed during the sign-in."""
        if self._xbox_proxy is not None:
            self.hass.async_create_task(self._async_close_xbox_proxy())

    async def async_step_xbox_link_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Link the Xbox Family Settings sign-in by pasting the redirect.

        Same manual step as the integration's old "paste the redirect URL":
        sign in on the link, then paste the oauth20_desktop.srf address the
        browser lands on. The code in it is single-use and needs the PKCE
        verifier kept here, so the address is useless to anyone else.
        """
        from .xbox_family_api import XboxFamilyAPI, XboxFamilyError, build_authorize_url

        errors: dict[str, str] = {}
        if user_input is not None and self._xbox_verifier:
            # Link through the running coordinator's instance when there is
            # one: a token saved by a separate client would only be read at
            # the next reload, and the options are usually unchanged.
            coordinator = self.hass.data.get(DOMAIN, {}).get(self._config_entry.entry_id)
            api = getattr(coordinator, "xbox_family", None) or XboxFamilyAPI(
                self.hass, self._config_entry.entry_id
            )
            try:
                await api.async_link_from_redirect(
                    user_input["redirect_url"], self._xbox_verifier, "manual"
                )
            except XboxFamilyError as err:
                _LOGGER.warning("Xbox Family Settings link failed: %s", err)
                errors["base"] = "xbox_link_failed"
                self._xbox_link_url = None
            else:
                if coordinator is not None:
                    coordinator.async_xbox_linked()
                return self.async_create_entry(title="", data=self._pending_options)
        if self._xbox_link_url is None:
            self._xbox_link_url, self._xbox_verifier = build_authorize_url()
        return self.async_show_form(
            step_id="xbox_link_manual",
            data_schema=vol.Schema({vol.Required("redirect_url"): str}),
            description_placeholders={
                "url": self._xbox_link_url,
                "redirect_prefix": "https://login.live.com/oauth20_desktop.srf?code=",
            },
            errors=errors,
        )


class InvalidAuth(HomeAssistantError):
    """Error to indicate authentication failure."""


class NativeAuthHttpsRequired(HomeAssistantError):
    """Native password proxying requires HTTPS unless local HTTP was opted into."""


class NativeAuthLocalHttpRequired(HomeAssistantError):
    """The opted-in HTTP proxy URL was not clearly local/private."""
