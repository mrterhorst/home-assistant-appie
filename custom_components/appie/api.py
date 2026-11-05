"""Async Albert Heijn mobile-API client.

Python implementation informed by https://github.com/gwillem/appie-go
(Go, AGPL-3.0) and its reverse-engineering notes (doc/albertheijn_api.md).

The single default "boodschappenlijst" is read and written through the
undocumented-but-described `shoppinglist/v2` endpoint pair. AH's separate
multi-list "favorite lists" (v3) feature is not touched.

Labels used below:
  (verified)  endpoint and payload as described in appie-go's notes, and/or
              confirmed against live data by users of this integration.
  (inferred)  no dedicated endpoint is documented; behaviour is derived from
              how AH's other endpoints work (see the method docstrings).
"""

from __future__ import annotations

import asyncio
import json as json_lib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import aiohttp

_LOGGER = logging.getLogger(__name__)

BASE_URL = "https://api.ah.nl"
CLIENT_ID = "appie-ios"
CLIENT_VERSION = "9.28"
USER_AGENT = "Appie/9.28 (iPhone17,3; iPhone; CPU OS 26_1 like Mac OS X)"
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=20)
# Refresh the access token this long before it expires.
TOKEN_REFRESH_MARGIN = timedelta(minutes=2)

# Request and response bodies of these paths are never logged (not even at
# debug level): auth carries codes and tokens, order data carries the
# delivery address.
_REDACTED_PATH_PREFIXES = ("/mobile-auth/", "/mobile-services/order/")

LIST_PATH = "/mobile-services/shoppinglist/v2/items"


class AppieApiError(Exception):
    """Raised for any AH API error (HTTP or transport)."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class AppieNotFoundError(AppieApiError):
    """Raised for HTTP 404 specifically."""


class AppieAuthError(AppieApiError):
    """No valid or refreshable session: a new login is needed."""


@dataclass(frozen=True)
class ListItem:
    """One row of the AH shopping list."""

    id: str
    name: str
    quantity: int
    checked: bool
    product_id: int | None = None

    @property
    def is_product(self) -> bool:
        return self.product_id is not None


@dataclass(frozen=True)
class Product:
    """A product from the AH assortment (search result)."""

    id: int
    title: str
    brand: str = ""
    unit_size: str = ""
    price: float | None = None
    is_bonus: bool = False
    bonus: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "product_id": self.id,
            "title": self.title,
            "brand": self.brand,
            "unit_size": self.unit_size,
            "price": self.price,
            "is_bonus": self.is_bonus,
            "bonus": self.bonus,
        }


def best_search_match(query: str, products: list[Product]) -> Product | None:
    """Pick a confident match for `query`, or None.

    AH's own relevance ranking is unreliable for short queries (e.g.
    "Bolletje" can come back as an unrelated product), so: an exact
    (case-insensitive) title match wins, then a title starting with the
    query, then a title containing every word of the query.
    """
    q = query.strip().lower()
    if not q:
        return None
    words = q.split()
    exact, prefix, contains = [], [], []
    for product in products:
        title = product.title.lower()
        if title == q:
            exact.append(product)
        elif title.startswith(q):
            prefix.append(product)
        elif all(word in title for word in words):
            contains.append(product)
    for bucket in (exact, prefix, contains):
        if bucket:
            return bucket[0]
    return None


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
        # One refresh at a time: AH may rotate refresh tokens, so two
        # concurrent refreshes could invalidate each other and force a
        # needless re-login.
        self._refresh_lock = asyncio.Lock()
        # Writes are read-modify-write against the list; serialise them.
        self._write_lock = asyncio.Lock()
        self._title_cache: dict[int, str] = {}

    # -- tokens ----------------------------------------------------------------

    def tokens_as_dict(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "member_id": self.member_id,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }

    async def _store_tokens(self, data: dict[str, Any]) -> None:
        self.access_token = data.get("access_token")
        self.refresh_token = data.get("refresh_token") or self.refresh_token
        member_id = data.get("member_id")
        if member_id is not None:
            self.member_id = str(member_id)
        expires_in = data.get("expires_in")
        self.expires_at = (
            datetime.now(UTC) + timedelta(seconds=int(expires_in))
            if expires_in
            else None
        )
        if self._on_tokens_updated:
            await self._on_tokens_updated(self.tokens_as_dict())

    async def exchange_code(self, code: str) -> dict[str, Any]:
        """(verified) Exchange an OAuth authorization code for tokens."""
        data = await self._raw_request(
            "POST", "/mobile-auth/v1/auth/token", {"clientId": CLIENT_ID, "code": code}, auth=False
        )
        if not data.get("access_token"):
            raise AppieApiError("login code exchange returned no access token")
        await self._store_tokens(data)
        return self.tokens_as_dict()

    async def logout(self) -> None:
        """(verified) Revoke the refresh token at AH."""
        if not self.refresh_token:
            return
        await self._raw_request(
            "POST",
            "/mobile-auth/v1/auth/token/logout",
            {"clientId": CLIENT_ID, "refreshToken": self.refresh_token},
            auth=False,
        )
        self.access_token = self.refresh_token = None

    def _token_expiring(self) -> bool:
        return bool(
            self.expires_at
            and datetime.now(UTC) > self.expires_at - TOKEN_REFRESH_MARGIN
        )

    async def _refresh_access_token(self, failed_token: str | None = None) -> None:
        """(verified) Refresh the access token.

        `failed_token` is the access token a request was rejected with; if
        another task already replaced it while we waited for the lock, there
        is nothing left to do.
        """
        async with self._refresh_lock:
            if failed_token is not None and self.access_token != failed_token:
                return
            if failed_token is None and self.access_token and not self._token_expiring():
                return  # refreshed by someone else while we waited
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
                    raise AppieAuthError("refresh token rejected", err.status) from err
                raise
            if not data.get("access_token"):
                raise AppieAuthError("token refresh returned no access token")
            await self._store_tokens(data)

    async def _ensure_fresh_token(self) -> None:
        if not self.access_token:
            if not self.refresh_token:
                raise AppieAuthError("not logged in")
            await self._refresh_access_token()
        elif self.refresh_token and self._token_expiring():
            try:
                await self._refresh_access_token()
            except AppieAuthError:
                raise
            except AppieApiError:
                # Transient (network/5xx): try the request with the old token;
                # a 401 there leads to one more refresh attempt.
                _LOGGER.warning("Token refresh failed; continuing with existing token")

    # -- HTTP ------------------------------------------------------------------

    async def _raw_request(
        self, method: str, path: str, json: dict[str, Any] | None = None, auth: bool = True
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
        redact = path.startswith(_REDACTED_PATH_PREFIXES)
        log_path = path.split("?", 1)[0]

        try:
            async with self._session.request(
                method, BASE_URL + path, json=json, headers=headers, timeout=REQUEST_TIMEOUT
            ) as resp:
                text = await resp.text()
                status = resp.status
        except (TimeoutError, aiohttp.ClientError) as err:
            raise AppieApiError(f"{method} {log_path} failed: {type(err).__name__}") from err

        _LOGGER.debug(
            "AH API %s %s -> HTTP %s: %s",
            method,
            log_path,
            status,
            "<redacted>" if redact else text[:4000],
        )
        try:
            body: Any = json_lib.loads(text) if text.strip() else {}
        except ValueError:
            body = {}

        if status >= 400:
            message = None
            if isinstance(body, dict):
                message = body.get("code") or body.get("message") or body.get("error")
            if not message or redact:
                message = f"HTTP {status}"
            if status == 404:
                raise AppieNotFoundError(f"{method} {log_path}: {message}", status)
            if status == 401 and auth:
                raise AppieAuthError(f"{method} {log_path}: {message}", status)
            raise AppieApiError(f"{method} {log_path}: {message}", status)
        return body if isinstance(body, dict) else {"data": body}

    async def _request(
        self, method: str, path: str, json: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        await self._ensure_fresh_token()
        token = self.access_token
        try:
            return await self._raw_request(method, path, json)
        except AppieAuthError:
            # Token rejected (revoked or expired early): refresh once and
            # retry. If AH rejects the refresh token too, AppieAuthError
            # propagates and Home Assistant asks for a new login.
            if not self.refresh_token:
                raise
            await self._refresh_access_token(failed_token=token)
            return await self._raw_request(method, path, json)

    # -- products --------------------------------------------------------------

    async def search_products(self, query: str, limit: int = 10) -> list[Product]:
        """(verified) GET /mobile-services/product/search/v2."""
        params = {"query": query, "page": "0", "size": str(limit), "sortOn": "RELEVANCE"}
        data = await self._request("GET", f"/mobile-services/product/search/v2?{urlencode(params)}")
        products = []
        for raw in (data.get("products") or [])[:limit]:
            if not raw.get("webshopId"):
                continue
            product = Product(
                id=int(raw["webshopId"]),
                title=raw.get("title") or "",
                brand=raw.get("brand") or "",
                unit_size=raw.get("salesUnitSize") or "",
                price=raw.get("currentPrice") or raw.get("priceBeforeBonus"),
                is_bonus=bool(raw.get("isBonus")),
                bonus=raw.get("bonusMechanism") or "",
            )
            if product.title:
                self._title_cache[product.id] = product.title
            products.append(product)
        return products

    async def get_product_title(self, product_id: int) -> str | None:
        """(verified) Product title by webshopId, cached per session."""
        if product_id in self._title_cache:
            return self._title_cache[product_id]
        try:
            data = await self._request("GET", f"/mobile-services/product/detail/v4/fir/{product_id}")
        except AppieAuthError:
            raise
        except AppieApiError:
            return None
        title = (data.get("productCard") or {}).get("title") or None
        if title:
            self._title_cache[product_id] = title
        return title

    # -- shopping list: reading -----------------------------------------------

    @staticmethod
    def _linked_product(raw: dict[str, Any]) -> dict[str, Any]:
        """(verified) Items linked to a product from within the AH app carry
        the product nested under productDetails.product (webshopId/title)."""
        return (raw.get("productDetails") or {}).get("product") or {}

    async def _parse_items(self, raw_items: list[dict[str, Any]]) -> list[ListItem]:
        items: list[ListItem] = []
        seen: dict[str, int] = {}
        for raw in raw_items:
            linked = self._linked_product(raw)
            product_id = raw.get("productId") or linked.get("webshopId") or linked.get("id")
            product_id = int(product_id) if product_id else None
            name = raw.get("description") or linked.get("title") or linked.get("description") or ""
            if product_id and not name:
                name = await self.get_product_title(product_id) or ""
            if not name and not product_id:
                # Unknown shape: log the field names only (not the content).
                _LOGGER.warning(
                    "Shopping list item without name or product id; fields: %s",
                    sorted(raw),
                )
                continue
            # Stable, readable uid: product:<id> or text:<description>.
            # Home Assistant needs unique uids, so duplicates get "#n".
            base = f"product:{product_id}" if product_id else f"text:{name}"
            seen[base] = seen.get(base, 0) + 1
            uid = base if seen[base] == 1 else f"{base}#{seen[base]}"
            items.append(
                ListItem(
                    id=uid,
                    name=name,
                    quantity=max(int(raw.get("quantity") or 1), 1),
                    # (verified) strikedthrough is top-level for every item.
                    checked=bool(raw.get("strikedthrough", False)),
                    product_id=product_id,
                )
            )
        return items

    async def get_shopping_list_items(self) -> list[ListItem]:
        """(verified) GET the default shopping list."""
        try:
            data = await self._request("GET", LIST_PATH)
        except AppieNotFoundError:
            return []  # account without a shopping list yet
        return await self._parse_items(data.get("items") or [])

    async def _find(self, item_id: str) -> ListItem:
        for item in await self.get_shopping_list_items():
            if item.id == item_id:
                return item
        raise AppieNotFoundError(f"item {item_id!r} is no longer on the list")

    # -- shopping list: writing -----------------------------------------------
    #
    # (verified) PATCH shoppinglist/v2/items adds items. (inferred) AH treats
    # it as an upsert keyed on productId (product items) or description
    # (free text), so re-sending an item with another quantity/strikeThrough
    # updates it, and quantity 0 removes it (the same convention AH's order
    # API uses). Confirmed in practice for ticking off.

    @staticmethod
    def _payload(
        *, name: str, quantity: int, checked: bool = False, product_id: int | None = None
    ) -> dict[str, Any]:
        item: dict[str, Any] = {
            "description": name,
            "quantity": max(int(quantity), 0),
            "type": "SHOPPABLE",
            "originCode": "PRD" if product_id else "TXT",
            "strikeThrough": bool(checked),
        }
        if product_id:
            item["productId"] = int(product_id)
            if name:
                item["searchTerm"] = name
        return item

    async def _patch(self, items: list[dict[str, Any]]) -> None:
        if items:
            await self._request("PATCH", LIST_PATH, {"items": items})

    async def add_item(
        self,
        *,
        name: str = "",
        product_id: int | None = None,
        quantity: int = 1,
        link_product: bool = False,
    ) -> dict[str, Any]:
        """Add an item. Returns what was added (product or free text)."""
        if not product_id and link_product and name:
            match = best_search_match(name, await self.search_products(name, limit=10))
            if match is not None:
                product_id, name = match.id, match.title
            else:
                _LOGGER.debug("No confident product match for %r; adding as free text", name)
        if product_id and not name:
            name = await self.get_product_title(product_id) or ""
        if not product_id and not name:
            raise ValueError("name or product_id is required")
        async with self._write_lock:
            await self._patch(
                [self._payload(name=name, quantity=quantity, product_id=product_id)]
            )
        if product_id:
            return {"added": "product", "product_id": product_id, "name": name, "quantity": quantity}
        return {"added": "free_text", "name": name, "quantity": quantity}

    async def update_item(
        self,
        item_id: str,
        *,
        name: str | None = None,
        quantity: int | None = None,
        product_id: int | None = None,
        checked: bool | None = None,
    ) -> None:
        """Change status, quantity and/or text of an item.

        Same identity (product id, or text for free-text items): one upsert.
        Different identity (renamed/relinked): remove the old item and add
        the new one in a single PATCH.
        """
        async with self._write_lock:
            current = await self._find(item_id)
            new_product_id = product_id if (product_id or name is not None) else current.product_id
            new_name = current.name if name is None else name
            if product_id and name is None:
                new_name = await self.get_product_title(product_id) or ""
            new = self._payload(
                name=new_name,
                quantity=current.quantity if quantity is None else quantity,
                checked=current.checked if checked is None else checked,
                product_id=new_product_id,
            )
            same_identity = (
                new_product_id == current.product_id
                if current.product_id or new_product_id
                else new_name == current.name
            )
            if same_identity:
                await self._patch([new])
                return
            old = self._payload(
                name=current.name, quantity=0, checked=current.checked, product_id=current.product_id
            )
            await self._patch([old, new])

    async def delete_items(self, item_ids: list[str]) -> None:
        """(inferred) Remove items by re-sending them with quantity 0."""
        if not item_ids:
            return
        async with self._write_lock:
            by_id = {item.id: item for item in await self.get_shopping_list_items()}
            await self._patch(
                [
                    self._payload(
                        name=item.name, quantity=0, checked=item.checked, product_id=item.product_id
                    )
                    for item_id in item_ids
                    if (item := by_id.get(item_id)) is not None
                ]
            )

    async def clear_list(self) -> int:
        items = await self.get_shopping_list_items()
        await self.delete_items([item.id for item in items])
        return len(items)

    # -- order -----------------------------------------------------------------

    async def shopping_list_to_order(self) -> dict[str, Any]:
        """Add unchecked, product-linked list items to the active AH order.

        (verified) PUT order/v1/items *sets* the quantity of each product in
        the order, so the current order quantities are read first and the
        list quantities are added to them; otherwise products already in the
        basket would be overwritten.
        """
        items = await self.get_shopping_list_items()
        wanted: dict[int, int] = {}
        skipped = 0
        for item in items:
            if item.checked:
                continue
            if not item.product_id:
                skipped += 1
                continue
            wanted[item.product_id] = wanted.get(item.product_id, 0) + item.quantity
        if not wanted:
            return {"added_products": 0, "skipped_free_text": skipped}

        try:
            order = await self._request("GET", "/mobile-services/order/v1/summaries/active?sortBy=DEFAULT")
        except AppieNotFoundError as err:
            raise AppieApiError("no active order (open a new order in the AH app first)", 404) from err
        in_order: dict[int, int] = {}
        for row in order.get("orderedProducts") or []:
            pid = (row.get("product") or {}).get("webshopId")
            if pid:
                in_order[int(pid)] = int(row.get("quantity") or row.get("amount") or 0)

        payload = [
            {
                "productId": pid,
                "quantity": in_order.get(pid, 0) + qty,
                "originCode": "PRD",
                "description": "",
                "strikethrough": False,
            }
            for pid, qty in wanted.items()
        ]
        await self._request("PUT", "/mobile-services/order/v1/items?sortBy=DEFAULT", {"items": payload})
        return {"added_products": len(wanted), "skipped_free_text": skipped}
