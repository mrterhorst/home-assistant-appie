"""Tests for the appie integration against a mocked AH API."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from http import HTTPStatus

import pytest
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMockResponse,
)

from custom_components.appie import coordinator as coord_mod
from custom_components.appie.api import AppieClient
from custom_components.appie.config_flow import _extract_code
from custom_components.appie.const import CONF_LINK_PRODUCTS, CONF_SCAN_INTERVAL, DOMAIN
from custom_components.appie.diagnostics import async_get_config_entry_diagnostics

API = "https://api.ah.nl"
LIST_URL = f"{API}/mobile-services/shoppinglist/v2/items"
TOKEN_URL = f"{API}/mobile-auth/v1/auth/token"
REFRESH_URL = f"{API}/mobile-auth/v1/auth/token/refresh"
LOGOUT_URL = f"{API}/mobile-auth/v1/auth/token/logout"
SEARCH_URL = f"{API}/mobile-services/product/search/v2"
ENTITY = "todo.boodschappenlijst"

ITEMS = {
    "items": [
        {"description": "melk", "quantity": 1, "strikedthrough": False},
        {"description": "melk", "quantity": 1, "strikedthrough": True},
        {"productId": 234096, "description": "Vaatwastabletten", "quantity": 2},
        {"productDetails": {"product": {"webshopId": 407171, "title": "AH Pindakaas"}}, "quantity": 1},
    ]
}
SEARCH = {
    "products": [
        {"webshopId": 1, "title": "Bolognese chips", "currentPrice": 1.0},
        {"webshopId": 2, "title": "Bolletje Beschuit", "currentPrice": 1.5, "isBonus": True,
         "bonusMechanism": "2 VOOR 2.50", "salesUnitSize": "13 stuks"},
    ]
}


def _future(hours: float = 1) -> str:
    return (datetime.now(UTC) + timedelta(hours=hours)).isoformat()


@pytest.fixture
def clock(monkeypatch):
    """Controllable monotonic clock for the coordinator's data age."""
    now = {"t": 1000.0}
    monkeypatch.setattr(coord_mod, "monotonic", lambda: now["t"])
    return now


def _entry(**options) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="Albert Heijn boodschappenlijst",
        unique_id="42",
        data={
            "access_token": "AT-old",
            "refresh_token": "RT-old",
            "member_id": "42",
            "expires_at": _future(),
        },
        options=options,
    )


async def _setup(hass, entry):
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _calls(aioclient_mock, url: str, method: str = "GET") -> list:
    return [c for c in aioclient_mock.mock_calls if c[0] == method and str(c[1]).split("?")[0] == url]


def _patches(aioclient_mock) -> list[list[dict]]:
    return [c[2]["items"] for c in _calls(aioclient_mock, LIST_URL, "PATCH")]


async def _result(client, msg_id: int | None = None) -> dict:
    """Read websocket messages until the result (skipping pushed item events)."""
    while True:
        msg = await client.receive_json()
        if msg["type"] == "result" and (msg_id is None or msg["id"] == msg_id):
            return msg


async def _items(hass) -> list[dict]:
    resp = await hass.services.async_call(
        "todo", "get_items", {"entity_id": ENTITY}, blocking=True, return_response=True
    )
    return resp[ENTITY]["items"]


# -- config flow / security ------------------------------------------------------


async def test_user_flow_and_no_secrets_in_log(hass: HomeAssistant, aioclient_mock, caplog):
    caplog.set_level(logging.DEBUG, logger="custom_components.appie")
    aioclient_mock.post(
        TOKEN_URL,
        json={"access_token": "SECRET-AT", "refresh_token": "SECRET-RT", "member_id": 42, "expires_in": 3600},
    )
    aioclient_mock.get(LIST_URL, json=ITEMS)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"callback": "appie://login-exit?code=SECRETCODE123"}
    )
    await hass.async_block_till_done()
    assert result["type"] == "create_entry"
    assert result["result"].unique_id == "42"
    assert "SECRET" not in caplog.text

    # Same account again: aborted, no duplicate entry.
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"callback": "SECRETCODE456"})
    assert result["type"] == "abort" and result["reason"] == "already_configured"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("appie://login-exit?code=abc12345-XYZ", "abc12345-XYZ"),
        ("appie://login-exit?code=abc%2B12345%3D", "abc+12345="),
        ("  abcdef123456  ", "abcdef123456"),
        ("appie://login-exit?state=1", None),
        ("short", None),
        ("abc<script>12345", None),
        ("", None),
    ],
)
def test_extract_code(raw, expected):
    assert _extract_code(raw) == expected


async def test_diagnostics_redacted(hass, aioclient_mock, clock):
    aioclient_mock.get(LIST_URL, json=ITEMS)
    entry = await _setup(hass, _entry())
    diag = await async_get_config_entry_diagnostics(hass, entry)
    text = str(diag)
    assert "AT-old" not in text and "RT-old" not in text and "'42'" not in text
    assert "melk" not in text and "Pindakaas" not in text
    assert diag["list"]["items"] == 4


async def test_logout_on_remove(hass, aioclient_mock, clock):
    aioclient_mock.get(LIST_URL, json=ITEMS)
    aioclient_mock.post(LOGOUT_URL, json={})
    entry = await _setup(hass, _entry())
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    calls = _calls(aioclient_mock, LOGOUT_URL, "POST")
    assert len(calls) == 1 and calls[0][2]["refreshToken"] == "RT-old"


# -- reading ---------------------------------------------------------------------


async def test_items_uids_quantity_and_titles(hass, aioclient_mock, clock):
    aioclient_mock.get(LIST_URL, json=ITEMS)
    await _setup(hass, _entry())
    items = await _items(hass)
    assert [i["uid"] for i in items] == ["text:melk", "text:melk#2", "product:234096", "product:407171"]
    assert [i["summary"] for i in items] == ["melk", "melk", "2x Vaatwastabletten", "AH Pindakaas"]
    assert items[1]["status"] == "completed"
    assert items[2]["description"] == "AH-product 234096"


# -- demand-driven refresh -------------------------------------------------------


async def test_refresh_on_open_and_while_open(hass, aioclient_mock, clock, hass_ws_client):
    aioclient_mock.get(LIST_URL, json=ITEMS)
    await _setup(hass, _entry(**{CONF_SCAN_INTERVAL: 0}))
    assert len(_calls(aioclient_mock, LIST_URL)) == 1  # initial load

    client = await hass_ws_client(hass)

    # Opened within 30 s of the last fetch: no extra AH call.
    await client.send_json_auto_id({"type": "todo/item/subscribe", "entity_id": ENTITY})
    msg = await _result(client)
    assert msg["success"]
    await hass.async_block_till_done()
    assert len(_calls(aioclient_mock, LIST_URL)) == 1
    await client.send_json_auto_id({"type": "unsubscribe_events", "subscription": msg["id"]})
    assert (await _result(client))["success"]

    # Opened again after the data went stale: fetch.
    clock["t"] += 100
    await client.send_json_auto_id({"type": "todo/item/subscribe", "entity_id": ENTITY})
    msg = await _result(client)
    sub_id = msg["id"]
    await hass.async_block_till_done()
    assert len(_calls(aioclient_mock, LIST_URL)) == 2

    # While open: refresh every minute.
    clock["t"] += 61
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=61))
    await hass.async_block_till_done()
    assert len(_calls(aioclient_mock, LIST_URL)) == 3

    # Closed: no more refreshes (background polling is off).
    await client.send_json_auto_id({"type": "unsubscribe_events", "subscription": sub_id})
    assert (await _result(client))["success"]
    await hass.async_block_till_done()
    for minutes in (2, 30, 120):
        clock["t"] += 600
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=minutes))
        await hass.async_block_till_done()
    assert len(_calls(aioclient_mock, LIST_URL)) == 3


async def test_background_interval(hass, aioclient_mock, clock):
    aioclient_mock.get(LIST_URL, json=ITEMS)
    await _setup(hass, _entry(**{CONF_SCAN_INTERVAL: 15}))
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=5))
    await hass.async_block_till_done()
    assert len(_calls(aioclient_mock, LIST_URL)) == 1
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=16))
    await hass.async_block_till_done()
    assert len(_calls(aioclient_mock, LIST_URL)) == 2


async def test_options_flow_reloads(hass, aioclient_mock, clock):
    aioclient_mock.get(LIST_URL, json=ITEMS)
    entry = await _setup(hass, _entry())
    assert entry.runtime_data.coordinator.update_interval == timedelta(minutes=60)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SCAN_INTERVAL: 0, CONF_LINK_PRODUCTS: True}
    )
    await hass.async_block_till_done()
    assert entry.options == {CONF_SCAN_INTERVAL: 0, CONF_LINK_PRODUCTS: True}
    assert entry.runtime_data.coordinator.update_interval is None


# -- auth --------------------------------------------------------------------------


async def test_401_refresh_and_retry_without_reload(hass, aioclient_mock, clock):
    aioclient_mock.get(LIST_URL, json=ITEMS)
    entry = await _setup(hass, _entry())
    coordinator = entry.runtime_data.coordinator

    calls = {"n": 0}

    async def list_side_effect(method, url, data):
        calls["n"] += 1
        if calls["n"] == 1:
            return AiohttpClientMockResponse(method, url, status=401, json={"code": "SESSION_EXPIRED"})
        return AiohttpClientMockResponse(method, url, json=ITEMS)

    aioclient_mock.clear_requests()
    aioclient_mock.get(LIST_URL, side_effect=list_side_effect)
    aioclient_mock.post(REFRESH_URL, json={"access_token": "AT-new", "refresh_token": "RT-new", "expires_in": 3600})

    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert coordinator.last_update_success
    assert entry.data["access_token"] == "AT-new"
    assert entry.data["refresh_token"] == "RT-new"
    assert entry.runtime_data.coordinator is coordinator  # token update did not reload


async def test_concurrent_requests_refresh_once(hass, aioclient_mock):
    """Two requests with an expiring token must not refresh twice (rotation)."""
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    aioclient_mock.post(REFRESH_URL, json={"access_token": "AT-new", "refresh_token": "RT-new", "expires_in": 3600})
    aioclient_mock.get(LIST_URL, json=ITEMS)
    client = AppieClient(
        async_get_clientsession(hass),
        tokens={"access_token": "AT", "refresh_token": "RT", "expires_at": _future(hours=-1)},
    )
    await asyncio.gather(client.get_shopping_list_items(), client.get_shopping_list_items())
    assert len(_calls(aioclient_mock, REFRESH_URL, "POST")) == 1


async def test_rejected_refresh_starts_reauth(hass, aioclient_mock, clock):
    entry = _entry()
    aioclient_mock.get(LIST_URL, status=HTTPStatus.UNAUTHORIZED, json={"code": "SESSION_EXPIRED"})
    aioclient_mock.post(REFRESH_URL, status=HTTPStatus.BAD_REQUEST, json={"error": "invalid_grant"})
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flow = next(f for f in hass.config_entries.flow.async_progress() if f["context"]["source"] == "reauth")

    aioclient_mock.clear_requests()
    aioclient_mock.post(TOKEN_URL, json={"access_token": "AT-2", "refresh_token": "RT-2", "member_id": 42, "expires_in": 3600})
    aioclient_mock.get(LIST_URL, json=ITEMS)
    result = await hass.config_entries.flow.async_configure(flow["flow_id"], {"callback": "NEWCODE123"})
    await hass.async_block_till_done()
    assert result["type"] == "abort" and result["reason"] == "reauth_successful"
    assert entry.data["access_token"] == "AT-2"
    assert entry.state is ConfigEntryState.LOADED


# -- writing via the to-do list -------------------------------------------------


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("melk", {"description": "melk", "quantity": 1, "originCode": "TXT"}),
        ("  2x   halfvolle  melk ", {"description": "halfvolle melk", "quantity": 2, "originCode": "TXT"}),
        ("eieren x 3", {"description": "eieren", "quantity": 3, "originCode": "TXT"}),
        ("4 seizoenen pizza", {"description": "4 seizoenen pizza", "quantity": 1, "originCode": "TXT"}),
        ("3x 441199", {"productId": 441199, "quantity": 3, "originCode": "PRD"}),
    ],
)
async def test_create_free_text_and_quantity(hass, aioclient_mock, clock, typed, expected):
    aioclient_mock.get(LIST_URL, json=ITEMS)
    aioclient_mock.patch(LIST_URL, json={})
    aioclient_mock.get(f"{API}/mobile-services/product/detail/v4/fir/441199", json={"productCard": {"title": "Melk"}})
    await _setup(hass, _entry())
    await hass.services.async_call("todo", "add_item", {"entity_id": ENTITY, "item": typed}, blocking=True)
    sent = _patches(aioclient_mock)[0][0]
    assert {k: sent[k] for k in expected} == expected
    assert sent["strikeThrough"] is False


async def test_create_with_product_linking_option(hass, aioclient_mock, clock):
    aioclient_mock.get(LIST_URL, json=ITEMS)
    aioclient_mock.patch(LIST_URL, json={})
    aioclient_mock.get(SEARCH_URL, json=SEARCH)
    await _setup(hass, _entry(**{CONF_LINK_PRODUCTS: True}))
    await hass.services.async_call("todo", "add_item", {"entity_id": ENTITY, "item": "Bolletje"}, blocking=True)
    await hass.services.async_call("todo", "add_item", {"entity_id": ENTITY, "item": "zeep"}, blocking=True)
    first, second = (p[0] for p in _patches(aioclient_mock))
    assert first["productId"] == 2 and first["description"] == "Bolletje Beschuit"
    assert second["originCode"] == "TXT" and "productId" not in second  # no confident match


async def test_empty_item_rejected(hass, aioclient_mock, clock):
    aioclient_mock.get(LIST_URL, json=ITEMS)
    await _setup(hass, _entry())
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call("todo", "add_item", {"entity_id": ENTITY, "item": " \x00 "}, blocking=True)
    assert _patches(aioclient_mock) == []


async def test_check_rename_quantity_and_delete(hass, aioclient_mock, clock):
    aioclient_mock.get(LIST_URL, json=ITEMS)
    aioclient_mock.patch(LIST_URL, json={})
    await _setup(hass, _entry())

    # Untick the second "melk".
    await hass.services.async_call(
        "todo", "update_item", {"entity_id": ENTITY, "item": "text:melk#2", "status": "needs_action"}, blocking=True
    )
    # Quantity change on a product: same identity, one upsert.
    await hass.services.async_call(
        "todo", "update_item", {"entity_id": ENTITY, "item": "product:234096", "rename": "5x Vaatwastabletten"},
        blocking=True,
    )
    # Rename free text: remove old + add new in one PATCH.
    await hass.services.async_call(
        "todo", "update_item", {"entity_id": ENTITY, "item": "text:melk", "rename": "2x havermelk"}, blocking=True
    )
    # Delete a product item.
    await hass.services.async_call(
        "todo", "remove_item", {"entity_id": ENTITY, "item": ["product:407171"]}, blocking=True
    )
    untick, qty, rename, delete = _patches(aioclient_mock)
    assert untick == [{"description": "melk", "quantity": 1, "type": "SHOPPABLE", "originCode": "TXT", "strikeThrough": False}]
    assert len(qty) == 1 and qty[0]["productId"] == 234096 and qty[0]["quantity"] == 5
    assert [(i["description"], i["quantity"]) for i in rename] == [("melk", 0), ("havermelk", 2)]
    assert delete[0]["productId"] == 407171 and delete[0]["quantity"] == 0


async def test_update_unknown_item(hass, aioclient_mock, clock):
    from homeassistant.components.todo import TodoItem, TodoItemStatus

    aioclient_mock.get(LIST_URL, json=ITEMS)
    await _setup(hass, _entry())
    entity = hass.data["entity_components"]["todo"].get_entity(ENTITY)
    with pytest.raises(ServiceValidationError):
        await entity.async_update_todo_item(
            TodoItem(uid="text:bestaat-niet", status=TodoItemStatus.COMPLETED)
        )


# -- services ----------------------------------------------------------------------


async def test_service_add_item_response(hass, aioclient_mock, clock):
    aioclient_mock.get(LIST_URL, json=ITEMS)
    aioclient_mock.patch(LIST_URL, json={})
    aioclient_mock.get(SEARCH_URL, json=SEARCH)
    await _setup(hass, _entry())
    resp = await hass.services.async_call(
        DOMAIN, "add_item", {"entity_id": ENTITY, "name": "Bolletje", "quantity": 2, "link_product": True},
        blocking=True, return_response=True,
    )
    assert resp[ENTITY] == {"added": "product", "product_id": 2, "name": "Bolletje Beschuit", "quantity": 2}

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "add_item", {"entity_id": ENTITY}, blocking=True)


async def test_service_search_products(hass, aioclient_mock, clock):
    aioclient_mock.get(LIST_URL, json=ITEMS)
    aioclient_mock.get(SEARCH_URL, json=SEARCH)
    await _setup(hass, _entry())
    resp = await hass.services.async_call(
        DOMAIN, "search_products", {"entity_id": ENTITY, "query": "beschuit", "limit": 5},
        blocking=True, return_response=True,
    )
    products = resp[ENTITY]["products"]
    assert products[1] == {
        "product_id": 2, "title": "Bolletje Beschuit", "brand": "", "unit_size": "13 stuks",
        "price": 1.5, "is_bonus": True, "bonus": "2 VOOR 2.50",
    }


async def test_no_order_endpoints(hass, aioclient_mock):
    """Only list/product/auth endpoints may be called; never orders/basket."""
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    from custom_components.appie.api import AppieApiError

    client = AppieClient(
        async_get_clientsession(hass), tokens={"access_token": "AT", "refresh_token": "RT", "expires_at": _future()}
    )
    for path in (
        "/mobile-services/order/v1/items?sortBy=DEFAULT",
        "/mobile-services/order/v1/summaries/active",
        "/mobile-services/v1/receipts",
        "/mobile-services/shoppinglist/v2/../../order/v1/items",
        "/mobile-services/product/%2e%2e/order/v1/items",
    ):
        with pytest.raises(AppieApiError, match="refusing"):
            await client._request("PUT", path, {})
    assert aioclient_mock.call_count == 0
    assert not hass.services.has_service(DOMAIN, "checkout")
