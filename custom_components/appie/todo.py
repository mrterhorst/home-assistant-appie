"""A Home Assistant to-do list backed directly by the Albert Heijn API."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import NoReturn, TypeVar

import voluptuous as vol
from homeassistant.components.todo import (
    TodoItem,
    TodoItemStatus,
    TodoListEntity,
    TodoListEntityFeature,
)
from homeassistant.core import (
    CALLBACK_TYPE,
    HomeAssistant,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_platform
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import AppieConfigEntry
from .api import (
    AppieApiError,
    AppieAuthError,
    AppieClient,
    AppieNotFoundError,
    ListItem,
)
from .const import (
    ATTR_LIMIT,
    ATTR_LINK_PRODUCT,
    ATTR_NAME,
    ATTR_PRODUCT_ID,
    ATTR_QUANTITY,
    ATTR_QUERY,
    CONF_LINK_PRODUCTS,
    DEFAULT_LINK_PRODUCTS,
    DOMAIN,
    MAX_NAME_LENGTH,
    MAX_QUANTITY,
    MAX_SEARCH_RESULTS,
    OPEN_MAX_AGE,
    OPEN_REFRESH_INTERVAL,
    SERVICE_ADD_ITEM,
    SERVICE_CLEAR_LIST,
    SERVICE_SEARCH_PRODUCTS,
)
from .coordinator import AppieCoordinator
from .textparse import clean_text, format_summary, parse_entry

_LOGGER = logging.getLogger(__name__)
_T = TypeVar("_T")

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant, entry: AppieConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([AppieTodoListEntity(entry.runtime_data.coordinator, entry)])

    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        SERVICE_ADD_ITEM,
        {
            vol.Optional(ATTR_NAME): vol.All(cv.string, vol.Length(min=1, max=MAX_NAME_LENGTH)),
            vol.Optional(ATTR_PRODUCT_ID): vol.All(vol.Coerce(int), vol.Range(min=1)),
            vol.Optional(ATTR_QUANTITY, default=1): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=MAX_QUANTITY)
            ),
            vol.Optional(ATTR_LINK_PRODUCT, default=False): cv.boolean,
        },
        "async_service_add_item",
        supports_response=SupportsResponse.OPTIONAL,
    )
    platform.async_register_entity_service(
        SERVICE_SEARCH_PRODUCTS,
        {
            vol.Required(ATTR_QUERY): vol.All(cv.string, vol.Length(min=1, max=MAX_NAME_LENGTH)),
            vol.Optional(ATTR_LIMIT, default=5): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=MAX_SEARCH_RESULTS)
            ),
        },
        "async_service_search_products",
        supports_response=SupportsResponse.ONLY,
    )
    platform.async_register_entity_service(SERVICE_CLEAR_LIST, {}, "async_service_clear_list")


def _to_todo_item(item: ListItem) -> TodoItem:
    return TodoItem(
        uid=item.id,
        summary=format_summary(item.name or f"Product {item.product_id}", item.quantity),
        status=TodoItemStatus.COMPLETED if item.checked else TodoItemStatus.NEEDS_ACTION,
        description=f"AH-product {item.product_id}" if item.product_id else None,
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

    def __init__(self, coordinator: AppieCoordinator, entry: AppieConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_shopping_list"
        self._viewers = 0
        self._open_timer: CALLBACK_TYPE | None = None

    @property
    def _client(self) -> AppieClient:
        return self.coordinator.client

    @property
    def _link_products(self) -> bool:
        return bool(self._entry.options.get(CONF_LINK_PRODUCTS, DEFAULT_LINK_PRODUCTS))

    # -- demand-driven refresh -------------------------------------------
    #
    # Every to-do card / to-do panel subscribes to the list over the
    # websocket (`todo/item/subscribe`) while it is shown, and unsubscribes
    # when it is closed. Hooking that subscription gives "fetch when someone
    # opens the list" without polling AH around the clock.
    #
    # TodoListEntity marks async_subscribe_updates as @final. That is a
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

    def _current(self, uid: str) -> ListItem | None:
        return next((item for item in self.coordinator.data or [] if item.id == uid), None)

    # -- error handling ----------------------------------------------------------

    def _fail(self, err: Exception) -> NoReturn:
        if isinstance(err, AppieAuthError):
            self._entry.async_start_reauth(self.hass)
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="auth_expired"
            ) from err
        if isinstance(err, AppieNotFoundError):
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="item_not_found"
            ) from err
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="api_error",
            translation_placeholders={"error": str(err)},
        ) from err

    async def _write(self, call: Awaitable[_T]) -> _T:
        """Run a write, map errors, then refresh the list."""
        try:
            result = await call
        except AppieApiError as err:
            self._fail(err)
        await self.coordinator.async_request_refresh()
        return result

    # -- to-do list operations ---------------------------------------------------

    async def async_create_todo_item(self, item: TodoItem) -> None:
        entry = parse_entry(item.summary)
        if not entry.name and not entry.product_id:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="empty_item")
        await self._write(
            self._client.add_item(
                name=entry.name,
                product_id=entry.product_id,
                quantity=entry.quantity,
                link_product=self._link_products,
            )
        )

    async def async_update_todo_item(self, item: TodoItem) -> None:
        current = self._current(item.uid)
        checked = None
        if item.status is not None:
            checked = item.status == TodoItemStatus.COMPLETED

        name: str | None = None
        quantity: int | None = None
        product_id: int | None = None
        if current is not None and item.summary is not None:
            old_summary = format_summary(current.name or f"Product {current.product_id}", current.quantity)
            if clean_text(item.summary) != old_summary:
                entry = parse_entry(item.summary)
                quantity = entry.quantity
                if entry.product_id:
                    if entry.product_id != current.product_id:
                        product_id = entry.product_id
                elif entry.name and entry.name != current.name:
                    name = entry.name
                elif not entry.name:
                    raise ServiceValidationError(
                        translation_domain=DOMAIN, translation_key="empty_item"
                    )

        await self._write(
            self._client.update_item(
                item.uid, name=name, quantity=quantity, product_id=product_id, checked=checked
            )
        )

    async def async_delete_todo_items(self, uids: list[str]) -> None:
        await self._write(self._client.delete_items(list(uids)))

    # -- services ------------------------------------------------------------------

    async def async_service_add_item(
        self,
        quantity: int = 1,
        link_product: bool = False,
        name: str | None = None,
        product_id: int | None = None,
    ) -> ServiceResponse:
        name = clean_text(name)
        if not name and not product_id:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="name_or_product_required"
            )
        return await self._write(
            self._client.add_item(
                name=name, product_id=product_id, quantity=quantity, link_product=link_product
            )
        )

    async def async_service_search_products(self, query: str, limit: int = 5) -> ServiceResponse:
        try:
            products = await self._client.search_products(clean_text(query), limit=limit)
        except AppieApiError as err:
            self._fail(err)
        return {"products": [product.as_dict() for product in products]}

    async def async_service_clear_list(self) -> None:
        await self._write(self._client.clear_list())
