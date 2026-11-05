"""Async Albert Heijn mobile-API client.

This is a Python port of the relevant parts of
https://github.com/gwillem/appie-go (Go, AGPL-3.0). The shopping-list
calls below are based on that project's own reverse-engineering
notes (doc/albertheijn_api.md, bundled with the library) rather than
appie-go's Go code directly — that doc documents a simpler, undocumented
`shoppinglist/v2` endpoint pair that appie-go's own Go wrapper doesn't
use, but that turned out to be the reliable way to read/write the single
default "boodschappenlijst" (as opposed to the separate, multi-list
"favorite lists" v3 feature, which some accounts use for other things
like saved recipes and which this integration does not touch).

Endpoints marked "(verified)" were copied one-to-one from that
documentation. Endpoints marked "(inferred)" are AH's own PATCH-based
add-item call, reused with different field values to approximate
"check" and "delete", because no dedicated endpoints for those are
documented — see the docstrings on check_item/delete_items for the
reasoning. None of this could be tested against a live AH account.
"""

from __future__ import annotations

import json as json_lib
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

import aiohttp

_LOGGER = logging.getLogger(__name__)

BASE_URL = "https://api.ah.nl"
LOGIN_BASE_URL = "https://login.ah.nl"
CLIENT_ID = "appie-ios"
CLIENT_VERSION = "9.28"
USER_AGENT = "Appie/9.28 (iPhone17,3; iPhone; CPU OS 26_1 like Mac OS X)"


class AppieApiError(Exception):
    """Raised for any AH API error (HTTP or GraphQL)."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class AppieNotFoundError(AppieApiError):
    """Raised for HTTP 404 specifically."""


class AppieAuthError(AppieApiError):
    """Raised when we have no valid/refreshable session (re-login needed)."""


# Paths whose request/response bodies contain secrets (auth codes, access
# and refresh tokens). These are never written to the log.
_SENSITIVE_PATH_PREFIX = "/mobile-auth/"


class AppieClient:
    """Talks to api.ah.nl directly. One instance per config entry."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        tokens: dict[str, Any] | None = None,
        on_tokens_updated=None,
    ) -> None:
        self._session = session
        self._on_tokens_updated = on_tokens_updated

        tokens = tokens or {}
        self.access_token: str | None = tokens.get("access_token")
        self.refresh_token: str | None = tokens.get("refresh_token")
        self.member_id: str | None = tokens.get("member_id")
        expires_at = tokens.get("expires_at")
        self.expires_at: datetime | None = (
            datetime.fromisoformat(expires_at) if expires_at else None
        )


    # -- token/session plumbing -----------------------------------------

    def is_authenticated(self) -> bool:
        return bool(self.access_token)

    def _tokens_as_dict(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "member_id": self.member_id,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }

    async def _store_tokens(self, data: dict[str, Any]) -> None:
        self.access_token = data.get("access_token")
        self.refresh_token = data.get("refresh_token") or self.refresh_token
        self.member_id = data.get("member_id") or self.member_id
        expires_in = data.get("expires_in")
        if expires_in:
            self.expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
        if self._on_tokens_updated:
            await self._on_tokens_updated(self._tokens_as_dict())

    async def get_anonymous_token(self) -> None:
        """(verified) Get a token without logging in (product browsing only)."""
        data = await self._raw_request(
            "POST", "/mobile-auth/v1/auth/token/anonymous", {"clientId": CLIENT_ID}, auth=False
        )
        await self._store_tokens(data)

    async def exchange_code(self, code: str) -> dict[str, Any]:
        """(verified) Exchange an OAuth authorization code for tokens."""
        data = await self._raw_request(
            "POST", "/mobile-auth/v1/auth/token", {"clientId": CLIENT_ID, "code": code}, auth=False
        )
        await self._store_tokens(data)
        return self._tokens_as_dict()

    async def _refresh_access_token(self) -> None:
        if not self.refresh_token:
            raise AppieAuthError("no refresh token available")
        try:
            data = await self._raw_request(
                "POST",
                "/mobile-auth/v1/auth/token/refresh",
                {"clientId": CLIENT_ID, "refreshToken": self.refresh_token},
                auth=False,
            )
        except AppieApiError as err:
            if err.status in (400, 401, 403):
                # AH rejected the refresh token itself: only a new login helps.
                raise AppieAuthError(f"refresh token rejected: {err}", err.status) from err
            raise
        if not data.get("access_token"):
            raise AppieAuthError("token refresh returned no access token")
        await self._store_tokens(data)

    async def _ensure_fresh_token(self) -> None:
        if not self.access_token:
            if not self.refresh_token:
                raise AppieAuthError("not logged in")
            await self._refresh_access_token()
            return
        # Refresh a minute early so a request never races the expiry.
        if (
            self.expires_at
            and self.refresh_token
            and datetime.now(timezone.utc) > self.expires_at - timedelta(seconds=60)
        ):
            try:
                await self._refresh_access_token()
            except AppieAuthError:
                raise
            except AppieApiError:
                # Transient (network/5xx): try the request with the old token;
                # a 401 there triggers one more refresh attempt.
                _LOGGER.warning("Token refresh failed; continuing with existing token")

    # -- low-level HTTP ----------------------------------------------------

    async def _raw_request(
        self, method: str, path: str, json: dict[str, Any] | None, auth: bool = True
    ) -> dict[str, Any]:
        headers = {
            "User-Agent": USER_AGENT,
            "x-client-name": CLIENT_ID,
            "x-client-version": CLIENT_VERSION,
            "x-application": "AHWEBSHOP",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if auth and self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"

        try:
            async with self._session.request(
                method, BASE_URL + path, json=json, headers=headers, timeout=aiohttp.ClientTimeout(total=20)
            ) as resp:
                text = await resp.text()
                try:
                    body: Any = json_lib.loads(text) if text.strip() else {}
                except ValueError:
                    body = {}
                sensitive = path.startswith(_SENSITIVE_PATH_PREFIX)
                _LOGGER.debug(
                    "AH API %s %s -> HTTP %s: %s",
                    method,
                    path,
                    resp.status,
                    "<redacted>" if sensitive else text[:4000],
                )
                if resp.status >= 400:
                    message = None
                    if isinstance(body, dict):
                        message = body.get("message") or body.get("code") or body.get("error")
                    if not message:
                        # Include the raw body so unexpected error shapes are
                        # still debuggable from the Home Assistant log.
                        snippet = "" if sensitive else text.strip()[:300]
                        message = f"HTTP {resp.status}" + (f" — body: {snippet}" if snippet else "")
                    if resp.status == 404:
                        exc_cls = AppieNotFoundError
                    elif resp.status == 401 and auth:
                        exc_cls = AppieAuthError
                    else:
                        exc_cls = AppieApiError
                    raise exc_cls(f"{method} {path} failed: {message}", resp.status)
                return body or {}
        except aiohttp.ClientError as err:
            raise AppieApiError(f"{method} {path} failed: {err}") from err

    async def _request(self, method: str, path: str, json: dict[str, Any] | None = None) -> dict[str, Any]:
        await self._ensure_fresh_token()
        try:
            return await self._raw_request(method, path, json)
        except AppieAuthError:
            # Access token rejected (expired early / revoked): refresh once and
            # retry. If the refresh itself is rejected, AppieAuthError
            # propagates and Home Assistant asks for a new login.
            if not self.refresh_token:
                raise
            await self._refresh_access_token()
            return await self._raw_request(method, path, json)

    async def _graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        result = await self._request("POST", "/graphql", {"query": query, "variables": variables})
        errors = result.get("errors")
        if errors:
            raise AppieApiError(f"graphql error: {errors[0].get('message')}")
        return result.get("data") or {}

    # -- shopping list -------------------------------------------------------
    #
    # There is a single, unnamed default shopping list per AH account (the
    # "boodschappenlijst" shown at the top of the AH app's list tab). It is
    # read and written entirely through the `shoppinglist/v2` endpoints
    # below and needs no list ID.
    #
    # This is a *different* feature from AH's "favorite lists" (v3) —
    # multiple named lists some accounts use for e.g. saved recipes or
    # wish lists. An earlier version of this integration used the v3
    # favoriteListV2/lists endpoints instead, on the assumption they were
    # the same thing; for at least some accounts that endpoint 404s even
    # though v3 "favorite lists" do exist, which is what prompted this
    # switch to the documented v2 pair. This integration does not touch
    # v3 lists at all.

    @staticmethod
    def _linked_product_details(raw_item: dict[str, Any]) -> dict[str, Any]:
        """Locate the nested product object for an item linked to a real
        product *from within the AH app itself* (as opposed to one added
        product-linked through this integration, which already carries a
        flat `productId`).

        (verified) Confirmed shape, from a live account's debug log:

            {
              ...,
              "productDetails": {
                "missingBonusOffersQuantity": 0,
                "product": {"webshopId": 407171, "title": "...", ...}
              }
            }
        """
        return (raw_item.get("productDetails") or {}).get("product") or {}

    @classmethod
    def _extract_product_id(cls, raw_item: dict[str, Any]) -> int:
        if raw_item.get("productId"):
            return raw_item["productId"]
        details = cls._linked_product_details(raw_item)
        return details.get("webshopId") or details.get("id") or 0

    @classmethod
    def _extract_name(cls, raw_item: dict[str, Any]) -> str:
        if raw_item.get("description"):
            return raw_item["description"]
        details = cls._linked_product_details(raw_item)
        return details.get("title") or details.get("description") or ""

    def _item_id(self, raw_item: dict[str, Any]) -> str:
        """Build a stable id for a v2 item.

        The v2 API has no per-item UUID (only a `listItemId` that looks
        like an internal/possibly-always-0 placeholder in samples seen),
        so we synthesize one: `product:<id>` for product-linked items,
        or `text:<description>` for free-text ones (made unique in
        get_shopping_list_items). Automations may match on these uids.
        """
        product_id = self._extract_product_id(raw_item)
        if product_id:
            return f"product:{product_id}"
        return f"text:{self._extract_name(raw_item)}"

    async def get_product_title(self, product_id: int) -> str | None:
        """(verified) GET /mobile-services/product/detail/v4/fir/{id}.

        Fallback used when a shopping-list item has a resolvable
        productId but no usable name from get_shopping_list_items —
        resolves the title via a direct product lookup instead.
        """
        try:
            data = await self._request("GET", f"/mobile-services/product/detail/v4/fir/{product_id}")
        except AppieApiError:
            return None
        card = data.get("productCard") or {}
        return card.get("title") or None

    async def get_shopping_list_items(self) -> list[dict[str, Any]]:
        """(verified) GET /mobile-services/shoppinglist/v2/items."""
        data = await self._request("GET", "/mobile-services/shoppinglist/v2/items")
        raw_items = data.get("items") or []
        parsed = []
        seen_ids: dict[str, int] = {}
        for item in raw_items:
            product_id = self._extract_product_id(item)
            name = self._extract_name(item)
            # (verified) strikedthrough sits at the top level of the item
            # regardless of whether it's linked to a product — confirmed
            # from a live account's debug log.
            checked = bool(item.get("strikedthrough", False))
            if product_id and not name:
                # We know which product it is but not its title (the item
                # itself didn't carry a usable one) — resolve it directly
                # instead of showing a bare "Product #<id>".
                name = await self.get_product_title(product_id) or ""
            item_id = self._item_id(item)
            # Home Assistant needs unique uids per list. Two free-text items
            # with the same text (or two rows for one product) would collide,
            # so later duplicates get a "#n" suffix. check_item/delete_items
            # resolve uids against a fresh read, never by parsing them.
            seen_ids[item_id] = seen_ids.get(item_id, 0) + 1
            if seen_ids[item_id] > 1:
                item_id = f"{item_id}#{seen_ids[item_id]}"
            entry = {
                "id": item_id,
                "product_id": product_id,
                "quantity": max(item.get("quantity") or 1, 1),
                "checked": checked,
                "name": name,
            }
            if not entry["name"] and not entry["product_id"]:
                # Neither field we rely on came back, even after the
                # fallbacks above — this is a shape this integration
                # doesn't understand yet. Log it at warning level (visible
                # without turning on debug logging) so the raw item can be
                # reported for a fix.
                _LOGGER.warning(
                    "Shopping list item with no name/productId — raw AH data: %s", item
                )
            parsed.append(entry)
        return parsed

    async def _add_to_shopping_list(self, items: list[dict[str, Any]]) -> None:
        """(verified) PATCH /mobile-services/shoppinglist/v2/items.

        Each item dict may set "checked" (maps to the API's
        `strikeThrough` field) and "quantity" — re-submitting an existing
        item (matched by productId, or by description for free-text
        items) is also how check_item/delete_items below update or
        remove an item; that upsert-by-identity behaviour is inferred,
        not documented.
        """
        v2_items = []
        for item in items:
            v2_items.append(
                {
                    "description": item.get("name", ""),
                    **({"productId": item["product_id"]} if item.get("product_id") else {}),
                    "quantity": max(item.get("quantity", 1), 0),
                    "type": "SHOPPABLE",
                    "originCode": "PRD" if item.get("product_id") else "TXT",
                    **({"searchTerm": item["name"]} if item.get("product_id") and item.get("name") else {}),
                    "strikeThrough": bool(item.get("checked", False)),
                }
            )
        await self._request("PATCH", "/mobile-services/shoppinglist/v2/items", {"items": v2_items})

    @staticmethod
    def _best_search_match(query: str, products: list[dict[str, Any]]) -> dict[str, Any] | None:
        """Pick the best product match for `query` from search results.

        Requesting only a single result from AH's search endpoint
        (`size=1`) turned out to be unreliable — the "best" match AH
        returns for a small page size doesn't always actually contain the
        search term (e.g. searching "Bolletje" can come back with an
        unrelated product as the sole result). To compensate, callers
        should request several candidates and let this scorer pick among
        them: an exact (case-insensitive) title match wins, then a title
        starting with the query, then a title containing every word of
        the query. If nothing reasonable matches, returns None so the
        caller can fall back to a free-text entry instead of silently
        adding the wrong product.
        """
        q = query.strip().lower()
        if not q:
            return None
        q_words = q.split()
        exact, startswith, contains_all = [], [], []
        for p in products:
            title = (p.get("title") or "").lower()
            if title == q:
                exact.append(p)
            elif title.startswith(q):
                startswith.append(p)
            elif all(w in title for w in q_words):
                contains_all.append(p)
        for bucket in (exact, startswith, contains_all):
            if bucket:
                return bucket[0]
        return None

    async def add_item(
        self, name: str | None = None, product_id: int | None = None, quantity: int = 1, free_text: bool = False
    ) -> dict[str, Any]:
        """Search for `name` and add the best match; falls back to free text."""
        if product_id:
            await self._add_to_shopping_list([{"product_id": product_id, "quantity": quantity}])
            return {"added": "product", "product_id": product_id}

        if not name:
            raise AppieApiError("name or product_id is required")

        if not free_text:
            matches = await self.search_products(name, limit=10)
            best = self._best_search_match(name, matches)
            if best:
                await self._add_to_shopping_list(
                    [{"product_id": best["id"], "quantity": quantity, "name": best["title"]}]
                )
                return {"added": "product", "product_id": best["id"], "title": best["title"]}
            _LOGGER.info(
                "No confident product match for %r among %d search results — adding as free text",
                name,
                len(matches),
            )

        await self._add_to_shopping_list([{"name": name, "quantity": quantity}])
        return {"added": "free_text", "name": name}

    async def check_item(self, item_id: str, checked: bool) -> None:
        """(inferred — see module docstring) No dedicated "check" endpoint
        is documented for the v2 shopping list, so this re-submits the
        item through the same add-item PATCH with `strikeThrough` set,
        preserving its current quantity/name from a fresh read.
        """
        items = await self.get_shopping_list_items()
        current = next((i for i in items if i["id"] == item_id), None)
        if current is None:
            raise AppieApiError(f"item {item_id!r} not found on the shopping list")
        await self._add_to_shopping_list(
            [
                {
                    "product_id": current["product_id"] or None,
                    "name": current["name"],
                    "quantity": current["quantity"],
                    "checked": checked,
                }
            ]
        )

    async def delete_items(self, item_ids: list[str]) -> None:
        """(inferred — see module docstring) No dedicated "delete" endpoint
        is documented for the v2 shopping list either. AH's own Order API
        uses `quantity: 0` to mean "remove" (see appie-go's
        RemoveFromOrder), so this applies the same pattern here via the
        add-item PATCH — unverified for the shopping list specifically.
        """
        if not item_ids:
            return
        items = await self.get_shopping_list_items()
        by_id = {i["id"]: i for i in items}
        to_remove = [by_id[i] for i in item_ids if i in by_id]
        if not to_remove:
            return
        await self._add_to_shopping_list(
            [
                {
                    "product_id": i["product_id"] or None,
                    "name": i["name"],
                    "quantity": 0,
                    "checked": i["checked"],
                }
                for i in to_remove
            ]
        )

    async def clear_list(self) -> None:
        items = await self.get_shopping_list_items()
        await self.delete_items([item["id"] for item in items])

    async def search_products(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        """(verified) GET /mobile-services/product/search/v2."""
        params = {"query": query, "page": "0", "size": str(limit), "sortOn": "RELEVANCE"}
        qs = urlencode(params)
        data = await self._request("GET", f"/mobile-services/product/search/v2?{qs}")
        out = []
        for p in (data.get("products") or [])[:limit]:
            out.append(
                {
                    "id": p["webshopId"],
                    "title": p.get("title", ""),
                    "brand": p.get("brand", ""),
                    "price": p.get("currentPrice") or p.get("priceBeforeBonus") or 0.0,
                }
            )
        return out

    async def shopping_list_to_order(self) -> None:
        """(verified) Move unchecked, product-linked items into the AH cart."""
        items = await self.get_shopping_list_items()
        order_items = [
            {"product_id": item["product_id"], "quantity": item["quantity"]}
            for item in items
            if not item["checked"] and item["product_id"]
        ]
        if not order_items:
            return
        merged: dict[int, int] = {}
        for it in order_items:
            merged[it["product_id"]] = merged.get(it["product_id"], 0) + it["quantity"]
        req_items = [
            {
                "productId": pid,
                "quantity": qty,
                "originCode": "PRD",
                "description": "",
                "strikethrough": False,
            }
            for pid, qty in merged.items()
        ]
        await self._request("PUT", "/mobile-services/order/v1/items?sortBy=DEFAULT", {"items": req_items})
