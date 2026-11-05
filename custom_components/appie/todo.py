"""A Home Assistant to-do list backed directly by the Albert Heijn API."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta

from homeassistant.components.todo import (
    TodoItem,
    TodoItemStatus,
    TodoListEntity,
    TodoListEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import AppieApiError, AppieAuthError
from .const import DOMAIN, OPEN_MAX_AGE, OPEN_REFRESH_INTERVAL
from .coordinator import AppieCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: AppieCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    async_add_entities([AppieTodoListEntity(coordinator, entry)])


def _to_todo_item(item: dict) -> TodoItem:
    return TodoItem(
        uid=item["id"],
        summary=item.get("name") or f"Product #{item.get('product_id')}",
        status=TodoItemStatus.COMPLETED if item.get("checked") else TodoItemStatus.NEEDS_ACTION,
    )


class AppieTodoListEntity(CoordinatorEntity[AppieCoordinator], TodoListEntity):
    """Boodschappenlijst backed by Albert Heijn."""

    _attr_has_entity_name = True
    _attr_name = "Boodschappenlijst"
    _attr_supported_features = (
        TodoListEntityFeature.CREATE_TODO_ITEM
        | TodoListEntityFeature.UPDATE_TODO_ITEM
        | TodoListEntityFeature.DELETE_TODO_ITEM
    )

    def __init__(self, coordinator: AppieCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_shopping_list"
        self._viewers = 0
        self._open_timer: CALLBACK_TYPE | None = None

    # -- demand-driven refresh -------------------------------------------
    #
    # Every to-do card / to-do panel subscribes to the list over the
    # websocket (`todo/item/subscribe`) while it is shown, and unsubscribes
    # when it is closed. Hooking that subscription gives us "fetch when
    # someone opens the list" without polling AH around the clock.
    #
    # Note: TodoListEntity marks async_subscribe_updates as @final. That is a
    # typing-only marker (not enforced at runtime); we only wrap it and
    # always delegate to the base implementation.

    @callback
    def async_subscribe_updates(  # type: ignore[misc]
        self, listener: Callable[[list[TodoItem] | None], None]
    ) -> CALLBACK_TYPE:
        unsubscribe = super().async_subscribe_updates(listener)
        self._viewers += 1
        self._sync_open_timer()
        self._schedule_refresh_if_stale(OPEN_MAX_AGE)

        done = False

        @callback
        def _unsubscribe() -> None:
            nonlocal done
            if done:
                return
            done = True
            unsubscribe()
            self._viewers = max(self._viewers - 1, 0)
            self._sync_open_timer()

        return _unsubscribe

    @callback
    def _schedule_refresh_if_stale(self, max_age: float) -> None:
        if self.hass is None:
            return
        self._entry.async_create_background_task(
            self.hass,
            self.coordinator.async_refresh_if_stale(max_age),
            "appie refresh on open",
        )

    @callback
    def _sync_open_timer(self) -> None:
        """Run a refresh timer only while at least one view has the list open."""
        if self._viewers > 0 and self._open_timer is None and self.hass is not None:

            @callback
            def _tick(_now: datetime) -> None:
                # Slightly below the interval so jitter never skips a tick.
                self._schedule_refresh_if_stale(OPEN_REFRESH_INTERVAL - 5)

            self._open_timer = async_track_time_interval(
                self.hass, _tick, timedelta(seconds=OPEN_REFRESH_INTERVAL)
            )
        elif self._viewers == 0 and self._open_timer is not None:
            self._open_timer()
            self._open_timer = None

    async def async_will_remove_from_hass(self) -> None:
        if self._open_timer is not None:
            self._open_timer()
            self._open_timer = None
        await super().async_will_remove_from_hass()

    # -- state ---------------------------------------------------------------

    @property
    def todo_items(self) -> list[TodoItem] | None:
        if self.coordinator.data is None:
            return None
        return [_to_todo_item(item) for item in self.coordinator.data]

    # -- writes --------------------------------------------------------------

    def _raise(self, err: AppieApiError, what: str) -> None:
        if isinstance(err, AppieAuthError):
            self._entry.async_start_reauth(self.hass)
            raise HomeAssistantError(
                "Albert Heijn login expired; log in again via the repair/reauth prompt"
            ) from err
        raise HomeAssistantError(f"Albert Heijn: could not {what}: {err}") from err

    async def async_create_todo_item(self, item: TodoItem) -> None:
        summary = item.summary.strip() if item.summary else ""
        try:
            if summary.isdigit():
                # Typed a bare number — treat it as a real AH product ID
                # and link it directly, skipping search/free-text entirely.
                await self.coordinator.client.add_item(product_id=int(summary), quantity=1)
            else:
                # Added as free text by default — product-linked items via
                # search are less reliable. Use the appie.add_item service
                # with free_text: false for search-based product linking.
                await self.coordinator.client.add_item(name=summary, quantity=1, free_text=True)
        except AppieApiError as err:
            self._raise(err, f"add '{summary}'")
        await self.coordinator.async_request_refresh()

    async def async_update_todo_item(self, item: TodoItem) -> None:
        checked = item.status == TodoItemStatus.COMPLETED
        try:
            await self.coordinator.client.check_item(item.uid, checked)
        except AppieApiError as err:
            self._raise(err, "update the item")
        await self.coordinator.async_request_refresh()

    async def async_delete_todo_items(self, uids: list[str]) -> None:
        try:
            await self.coordinator.client.delete_items(list(uids))
        except AppieApiError as err:
            self._raise(err, "delete the items")
        await self.coordinator.async_request_refresh()
