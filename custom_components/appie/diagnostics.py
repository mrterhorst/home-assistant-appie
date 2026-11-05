"""Diagnostics: shareable without leaking tokens or the list contents."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import AppieConfigEntry

TO_REDACT = {"access_token", "refresh_token", "member_id"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: AppieConfigEntry
) -> dict[str, Any]:
    coordinator = entry.runtime_data.coordinator
    items = coordinator.data or []
    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        },
        "coordinator": {
            "last_update_success": coordinator.last_update_success,
            "update_interval": str(coordinator.update_interval),
            "data_age_seconds": coordinator.data_age,
        },
        "list": {
            "items": len(items),
            "checked": sum(1 for item in items if item.checked),
            "product_linked": sum(1 for item in items if item.is_product),
            "max_quantity": max((item.quantity for item in items), default=0),
        },
    }
