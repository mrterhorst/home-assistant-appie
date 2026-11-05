"""Coordinator for the AH shopping list.

Fetching is demand-driven: the list is read from AH when someone opens it
in Home Assistant (see todo.py), periodically while it stays open, after
changes made from Home Assistant, and optionally on a slow background
interval so automations and voice assistants see a reasonably fresh list.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from time import monotonic

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import AppieApiError, AppieAuthError, AppieClient, ListItem
from .const import CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)


def background_interval(entry: ConfigEntry) -> timedelta | None:
    """Return the configured background interval, or None when disabled."""
    minutes = int(entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
    return timedelta(minutes=minutes) if minutes > 0 else None


class AppieCoordinator(DataUpdateCoordinator[list[ListItem]]):
    """Holds the AH shopping list and refreshes it on demand."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: AppieClient) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name="appie shopping list",
            update_interval=background_interval(entry),
        )
        self.client = client
        self._last_fetch: float | None = None

    @property
    def data_age(self) -> float | None:
        """Seconds since the last successful fetch, or None if never fetched."""
        if self._last_fetch is None:
            return None
        return monotonic() - self._last_fetch

    async def async_refresh_if_stale(self, max_age: float) -> None:
        """Refresh unless the data is younger than max_age seconds."""
        age = self.data_age
        if age is not None and age < max_age:
            return
        await self.async_request_refresh()

    async def _async_update_data(self) -> list[ListItem]:
        try:
            items = await self.client.get_shopping_list_items()
        except AppieAuthError as err:
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN, translation_key="auth_expired"
            ) from err
        except AppieApiError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="api_error",
                translation_placeholders={"error": str(err)},
            ) from err
        self._last_fetch = monotonic()
        return items
