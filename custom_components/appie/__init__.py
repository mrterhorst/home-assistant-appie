"""The Albert Heijn boodschappenlijst integration (no external service)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import AppieApiError, AppieClient
from .coordinator import AppieCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.TODO]


@dataclass
class AppieData:
    """Runtime data of one config entry."""

    client: AppieClient
    coordinator: AppieCoordinator
    options: dict[str, Any] = field(default_factory=dict)


type AppieConfigEntry = ConfigEntry[AppieData]


async def async_setup_entry(hass: HomeAssistant, entry: AppieConfigEntry) -> bool:
    async def _on_tokens_updated(tokens: dict[str, Any]) -> None:
        hass.config_entries.async_update_entry(entry, data={**entry.data, **tokens})

    client = AppieClient(
        async_get_clientsession(hass),
        tokens=dict(entry.data),
        on_tokens_updated=_on_tokens_updated,
    )
    coordinator = AppieCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = AppieData(client, coordinator, dict(entry.options))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_entry_updated))
    return True


async def _async_entry_updated(hass: HomeAssistant, entry: AppieConfigEntry) -> None:
    """Reload only when the options changed (token refreshes also update the entry)."""
    if entry.runtime_data.options != dict(entry.options):
        await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: AppieConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Revoke the session at AH when the integration is removed."""
    client = AppieClient(async_get_clientsession(hass), tokens=dict(entry.data))
    try:
        await client.logout()
    except AppieApiError as err:
        _LOGGER.warning("Could not log out at Albert Heijn (%s); the token simply expires", err)
