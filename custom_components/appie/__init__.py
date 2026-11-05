"""The Albert Heijn boodschappenlijst integration (no external service)."""

from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import AppieApiError, AppieAuthError, AppieClient
from .const import (
    ATTR_FREE_TEXT,
    ATTR_NAME,
    ATTR_PRODUCT_ID,
    ATTR_QUANTITY,
    DOMAIN,
    SERVICE_ADD_ITEM,
    SERVICE_CHECKOUT,
    SERVICE_CLEAR_LIST,
)
from .coordinator import AppieCoordinator

PLATFORMS = ["todo"]

ADD_ITEM_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_NAME): str,
        vol.Optional(ATTR_PRODUCT_ID): int,
        vol.Optional(ATTR_QUANTITY, default=1): int,
        vol.Optional(ATTR_FREE_TEXT, default=True): bool,
    }
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    session = async_get_clientsession(hass)

    async def _on_tokens_updated(tokens: dict) -> None:
        hass.config_entries.async_update_entry(entry, data={**entry.data, **tokens})

    client = AppieClient(session, tokens=dict(entry.data), on_tokens_updated=_on_tokens_updated)

    coordinator = AppieCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        "client": client,
        "coordinator": coordinator,
        "options": dict(entry.options),
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _register_services(hass)
    entry.async_on_unload(entry.add_update_listener(_async_entry_updated))
    return True


async def _async_entry_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload only when the options changed.

    Token refreshes also update the entry (data), and those must not
    trigger a reload.
    """
    entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if entry_data is not None and entry_data["options"] != dict(entry.options):
        await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
        if not hass.data[DOMAIN]:
            for service in (SERVICE_ADD_ITEM, SERVICE_CHECKOUT, SERVICE_CLEAR_LIST):
                hass.services.async_remove(DOMAIN, service)
    return unload_ok


def _register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_ADD_ITEM):
        return

    def _first_entry_data() -> dict:
        if not hass.data.get(DOMAIN):
            raise HomeAssistantError("Albert Heijn is not set up")
        return next(iter(hass.data[DOMAIN].values()))

    def _first_client() -> AppieClient:
        return _first_entry_data()["client"]

    def _wrap(err: AppieApiError) -> HomeAssistantError:
        if isinstance(err, AppieAuthError):
            for entry in hass.config_entries.async_entries(DOMAIN):
                entry.async_start_reauth(hass)
            return HomeAssistantError("Albert Heijn login expired; log in again")
        return HomeAssistantError(f"Albert Heijn: {err}")

    async def _refresh_all() -> None:
        for entry_data in hass.data[DOMAIN].values():
            await entry_data["coordinator"].async_request_refresh()

    async def handle_add_item(call: ServiceCall) -> None:
        client = _first_client()
        try:
            await client.add_item(
                name=call.data.get(ATTR_NAME),
                product_id=call.data.get(ATTR_PRODUCT_ID),
                quantity=call.data.get(ATTR_QUANTITY, 1),
                free_text=call.data.get(ATTR_FREE_TEXT, True),
            )
        except AppieApiError as err:
            raise _wrap(err) from err
        finally:
            await _refresh_all()

    async def handle_checkout(call: ServiceCall) -> None:
        try:
            await _first_client().shopping_list_to_order()
        except AppieApiError as err:
            raise _wrap(err) from err

    async def handle_clear_list(call: ServiceCall) -> None:
        try:
            await _first_client().clear_list()
        except AppieApiError as err:
            raise _wrap(err) from err
        finally:
            await _refresh_all()

    hass.services.async_register(DOMAIN, SERVICE_ADD_ITEM, handle_add_item, schema=ADD_ITEM_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_CHECKOUT, handle_checkout)
    hass.services.async_register(DOMAIN, SERVICE_CLEAR_LIST, handle_clear_list)
