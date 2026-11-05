"""Tests for the appie integration against a mocked AH API."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from http import HTTPStatus

import pytest
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMockResponse
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.appie import coordinator as coord_mod
from custom_components.appie.const import CONF_SCAN_INTERVAL, DOMAIN

LIST_URL = "https://api.ah.nl/mobile-services/shoppinglist/v2/items"
TOKEN_URL = "https://api.ah.nl/mobile-auth/v1/auth/token"
REFRESH_URL = "https://api.ah.nl/mobile-auth/v1/auth/token/refresh"
ENTITY = "todo.boodschappenlijst"

ITEMS = {
    "items": [
        {"description": "melk", "quantity": 1, "strikedthrough": False},
        {"description": "melk", "quantity": 1, "strikedthrough": True},
        {"productId": 234096, "description": "Vaatwastabletten", "quantity": 1},
    ]
}


def _future() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()


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


async def _result(client, msg_id: int | None = None) -> dict:
    """Read websocket messages until the result (skipping pushed item events)."""
    while True:
        msg = await client.receive_json()
        if msg["type"] == "result" and (msg_id is None or msg["id"] == msg_id):
            return msg


def _calls(aioclient_mock, url: str, method: str = "GET") -> int:
    return sum(1 for c in aioclient_mock.mock_calls if c[0] == method and str(c[1]) == url)


async def test_user_flow_and_no_token_logging(hass: HomeAssistant, aioclient_mock, caplog):
    caplog.set_level(logging.DEBUG, logger="custom_components.appie")
    aioclient_mock.post(
        TOKEN_URL,
        json={"access_token": "SECRET-AT", "refresh_token": "SECRET-RT", "member_id": 42, "expires_in": 3600},
    )
    aioclient_mock.get(LIST_URL, json=ITEMS)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"callback": "appie://login-exit?code=SECRET-CODE"}
    )
    await hass.async_block_till_done()
    assert result["type"] == "create_entry"
    assert result["result"].unique_id == "42"
    assert "SECRET" not in caplog.text


async def test_unique_uids(hass, aioclient_mock, clock):
    aioclient_mock.get(LIST_URL, json=ITEMS)
    await _setup(hass, _entry())
    items = (
        await hass.services.async_call(
            "todo", "get_items", {"entity_id": ENTITY}, blocking=True, return_response=True
        )
    )[ENTITY]["items"]
    uids = [i["uid"] for i in items]
    assert uids == ["text:melk", "text:melk#2", "product:234096"]
    assert len(set(uids)) == 3


async def test_refresh_on_open_and_while_open(hass, aioclient_mock, clock, hass_ws_client):
    aioclient_mock.get(LIST_URL, json=ITEMS)
    await _setup(hass, _entry(**{CONF_SCAN_INTERVAL: 0}))
    assert _calls(aioclient_mock, LIST_URL) == 1  # initial load

    client = await hass_ws_client(hass)

    # Opened within 30 s of the last fetch: no extra AH call.
    await client.send_json_auto_id({"type": "todo/item/subscribe", "entity_id": ENTITY})
    msg = await _result(client)
    assert msg["success"]
    await hass.async_block_till_done()
    assert _calls(aioclient_mock, LIST_URL) == 1
    sub_id = msg["id"]
    await client.send_json_auto_id({"type": "unsubscribe_events", "subscription": sub_id})
    assert (await _result(client))["success"]

    # Opened again after the data went stale: fetch.
    clock["t"] += 100
    await client.send_json_auto_id({"type": "todo/item/subscribe", "entity_id": ENTITY})
    msg = await _result(client)
    assert msg["success"]
    sub_id = msg["id"]
    await hass.async_block_till_done()
    assert _calls(aioclient_mock, LIST_URL) == 2

    # While open: refresh every minute.
    clock["t"] += 61
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=61))
    await hass.async_block_till_done()
    assert _calls(aioclient_mock, LIST_URL) == 3

    # Closed: no more refreshes (background polling is off).
    await client.send_json_auto_id({"type": "unsubscribe_events", "subscription": sub_id})
    assert (await _result(client))["success"]
    await hass.async_block_till_done()
    for minutes in (2, 30, 120):
        clock["t"] += 600
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=minutes))
        await hass.async_block_till_done()
    assert _calls(aioclient_mock, LIST_URL) == 3


async def test_background_interval(hass, aioclient_mock, clock):
    aioclient_mock.get(LIST_URL, json=ITEMS)
    await _setup(hass, _entry(**{CONF_SCAN_INTERVAL: 15}))
    assert _calls(aioclient_mock, LIST_URL) == 1
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=5))
    await hass.async_block_till_done()
    assert _calls(aioclient_mock, LIST_URL) == 1
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=16))
    await hass.async_block_till_done()
    assert _calls(aioclient_mock, LIST_URL) == 2


async def test_401_refresh_and_retry_without_reload(hass, aioclient_mock, clock):
    entry = _entry()
    aioclient_mock.get(LIST_URL, json=ITEMS)
    await _setup(hass, entry)
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]

    calls = {"n": 0}

    async def list_side_effect(method, url, data):
        calls["n"] += 1
        if calls["n"] == 1:
            return AiohttpClientMockResponse(method, url, status=401, json={"message": "expired"})
        return AiohttpClientMockResponse(method, url, json=ITEMS)

    aioclient_mock.clear_requests()
    aioclient_mock.get(LIST_URL, side_effect=list_side_effect)
    aioclient_mock.post(REFRESH_URL, json={"access_token": "AT-new", "refresh_token": "RT-new", "expires_in": 3600})

    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert coordinator.last_update_success
    assert entry.data["access_token"] == "AT-new"
    assert entry.data["refresh_token"] == "RT-new"
    assert entry.state is ConfigEntryState.LOADED
    # Token update must not have caused a reload (same coordinator object).
    assert hass.data[DOMAIN][entry.entry_id]["coordinator"] is coordinator


async def test_rejected_refresh_starts_reauth(hass, aioclient_mock, clock):
    entry = _entry()
    aioclient_mock.get(LIST_URL, status=HTTPStatus.UNAUTHORIZED, json={"message": "expired"})
    aioclient_mock.post(REFRESH_URL, status=HTTPStatus.BAD_REQUEST, json={"error": "invalid_grant"})
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert any(f["context"]["source"] == "reauth" for f in flows)

    # Complete reauth.
    flow = next(f for f in flows if f["context"]["source"] == "reauth")
    aioclient_mock.clear_requests()
    aioclient_mock.post(TOKEN_URL, json={"access_token": "AT-2", "refresh_token": "RT-2", "member_id": 42, "expires_in": 3600})
    aioclient_mock.get(LIST_URL, json=ITEMS)
    result = await hass.config_entries.flow.async_configure(flow["flow_id"], {"callback": "CODE"})
    await hass.async_block_till_done()
    assert result["type"] == "abort" and result["reason"] == "reauth_successful"
    assert entry.data["access_token"] == "AT-2"
    assert entry.state is ConfigEntryState.LOADED


async def test_options_flow_reloads(hass, aioclient_mock, clock):
    entry = _entry()
    aioclient_mock.get(LIST_URL, json=ITEMS)
    await _setup(hass, entry)
    assert hass.data[DOMAIN][entry.entry_id]["coordinator"].update_interval == timedelta(minutes=60)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_SCAN_INTERVAL: 0})
    await hass.async_block_till_done()
    assert entry.options[CONF_SCAN_INTERVAL] == 0
    assert hass.data[DOMAIN][entry.entry_id]["coordinator"].update_interval is None


async def test_check_and_delete_use_fresh_read(hass, aioclient_mock, clock):
    aioclient_mock.get(LIST_URL, json=ITEMS)
    aioclient_mock.patch(LIST_URL, json={})
    await _setup(hass, _entry())
    await hass.services.async_call(
        "todo", "update_item", {"entity_id": ENTITY, "item": "text:melk#2", "status": "needs_action"}, blocking=True
    )
    await hass.services.async_call(
        "todo", "remove_item", {"entity_id": ENTITY, "item": ["product:234096"]}, blocking=True
    )
    patches = [c for c in aioclient_mock.mock_calls if c[0] == "PATCH"]
    assert patches[0][2]["items"][0]["description"] == "melk"
    assert patches[0][2]["items"][0]["strikeThrough"] is False
    assert patches[1][2]["items"][0]["productId"] == 234096
    assert patches[1][2]["items"][0]["quantity"] == 0
