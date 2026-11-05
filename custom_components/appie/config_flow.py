"""Config flow for the Albert Heijn boodschappenlijst integration.

Login is manual: we show the plain AH login URL, the user logs in in
their own browser, and pastes back the resulting `appie://login-exit?
code=...` URL (captured via the browser's DevTools, since the browser
itself can't follow a custom URL scheme) or just the bare code. We then
exchange that code for tokens.

This replaces an earlier version that tried to automate this with a local
reverse-proxy server; that turned out to be unreliable, so this manual
step is simpler and more robust.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import unquote

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import CLIENT_ID, AppieApiError, AppieClient
from .const import (
    CONF_LINK_PRODUCTS,
    CONF_SCAN_INTERVAL,
    DEFAULT_LINK_PRODUCTS,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

LOGIN_URL = (
    f"https://login.ah.nl/login?client_id={CLIENT_ID}"
    "&response_type=code&redirect_uri=appie://login-exit"
)

CONF_CALLBACK = "callback"

_CODE_RE = re.compile(r"(?:^|[?&])code=([^&\s#]+)")
# OAuth codes are URL-/base64-safe tokens; reject anything else before sending it on.
_CODE_CHARS = re.compile(r"^[A-Za-z0-9._~+/=\-]{8,4096}$")


def _extract_code(raw: str) -> str | None:
    """Pull the authorization code out of whatever the user pasted.

    Accepts a full `appie://login-exit?code=...` URL, a URL-encoded
    variant, or just the bare code value.
    """
    raw = raw.strip()
    if not raw:
        return None
    match = _CODE_RE.search(raw)
    if match:
        code = unquote(match.group(1))
    elif raw.startswith(("http", "appie")):
        return None  # looks like a URL but we couldn't find a code= param
    else:
        code = raw
    return code if _CODE_CHARS.match(code) else None


class AppieConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for the Albert Heijn integration."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        return AppieOptionsFlow()

    async def _async_login(
        self, user_input: dict[str, Any], errors: dict[str, str]
    ) -> dict[str, Any] | None:
        """Exchange the pasted code for tokens; fill errors on failure."""
        code = _extract_code(user_input[CONF_CALLBACK])
        if not code:
            errors["base"] = "invalid_code"
            return None
        client = AppieClient(async_get_clientsession(self.hass))
        try:
            return await client.exchange_code(code)
        except AppieApiError as err:
            # Never log the code or the response: both are secrets.
            _LOGGER.error("Failed to exchange AH login code for tokens: HTTP %s", err.status)
            errors["base"] = "cannot_connect"
            return None

    def _form(self, step_id: str, errors: dict[str, str]) -> config_entries.ConfigFlowResult:
        return self.async_show_form(
            step_id=step_id,
            data_schema=vol.Schema({vol.Required(CONF_CALLBACK): str}),
            description_placeholders={"login_url": LOGIN_URL},
            errors=errors,
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            tokens = await self._async_login(user_input, errors)
            if tokens is not None:
                if tokens.get("member_id"):
                    await self.async_set_unique_id(str(tokens["member_id"]))
                    self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title="Albert Heijn boodschappenlijst", data=tokens
                )
        return self._form("user", errors)

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> config_entries.ConfigFlowResult:
        """Triggered when AH rejects the stored refresh token."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            tokens = await self._async_login(user_input, errors)
            if tokens is not None:
                entry = self._get_reauth_entry()
                if (
                    entry.unique_id
                    and tokens.get("member_id")
                    and str(tokens["member_id"]) != entry.unique_id
                ):
                    return self.async_abort(reason="wrong_account")
                return self.async_update_reload_and_abort(
                    entry, data={**entry.data, **tokens}
                )
        return self._form("reauth_confirm", errors)


class AppieOptionsFlow(config_entries.OptionsFlow):
    """Options: background refresh and product linking."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        options = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_SCAN_INTERVAL,
                        default=options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                    ): vol.All(vol.Coerce(int), vol.Range(min=0, max=MAX_SCAN_INTERVAL)),
                    vol.Required(
                        CONF_LINK_PRODUCTS,
                        default=options.get(CONF_LINK_PRODUCTS, DEFAULT_LINK_PRODUCTS),
                    ): bool,
                }
            ),
        )
