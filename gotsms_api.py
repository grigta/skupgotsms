from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger("gotsms")


class GotSmsError(Exception):
    def __init__(self, status: int, payload: Any):
        self.status = status
        self.payload = payload
        super().__init__(f"gotsms api error {status}: {payload}")


class NoNumbersAvailable(GotSmsError):
    pass


class InsufficientFunds(GotSmsError):
    pass


@dataclass
class Service:
    id: str
    name: str


@dataclass
class Plan:
    id: str
    service_id: str
    service_name: str
    country_name: str | None
    duration: str
    duration_type: str
    billing_type: str
    price: float
    raw: dict


@dataclass
class Rent:
    id: str
    service_name: str
    phone: str
    price: float
    status: str
    active_from: str | None
    active_till: str | None


@dataclass
class Message:
    id: str
    rent_id: str
    service_name: str
    phone: str
    sender: str
    body: str
    code: str | None
    received_at: str


def _extract_price(item: dict) -> float:
    """Plan response shapes vary across API versions — try common keys."""
    for key in ("price", "total_price", "amount", "cost"):
        if key in item and item[key] is not None:
            try:
                return float(item[key])
            except (TypeError, ValueError):
                continue
    prices = item.get("prices")
    if isinstance(prices, dict):
        for key in ("total", "amount", "value"):
            if key in prices:
                try:
                    return float(prices[key])
                except (TypeError, ValueError):
                    continue
    return 0.0


class GotSmsClient:
    def __init__(self, token: str, base_url: str = "https://app.gotsms.org", timeout: float = 60.0, cache_ttl: float = 600.0):
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout=timeout,
            http2=True,
            limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
        )
        self._cache_ttl = cache_ttl
        self._cache: dict[str, tuple[float, Any]] = {}
        self._cache_locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, key: str) -> asyncio.Lock:
        lock = self._cache_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._cache_locks[key] = lock
        return lock

    def invalidate_cache(self, prefix: str | None = None) -> None:
        if prefix is None:
            self._cache.clear()
            return
        for k in [k for k in self._cache if k.startswith(prefix)]:
            self._cache.pop(k, None)

    def _cache_get(self, key: str) -> Any | None:
        item = self._cache.get(key)
        if not item:
            return None
        ts, val = item
        if time.monotonic() - ts > self._cache_ttl:
            self._cache.pop(key, None)
            return None
        return val

    def _cache_set(self, key: str, val: Any) -> None:
        self._cache[key] = (time.monotonic(), val)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.aclose()

    async def _request(self, method: str, path: str, **kwargs) -> Any:
        resp = await self._client.request(method, path, **kwargs)
        try:
            payload = resp.json()
        except ValueError:
            payload = {"raw": resp.text}

        if resp.status_code >= 400:
            log.warning(
                "%s %s -> %d body=%s payload=%s",
                method, path, resp.status_code, kwargs.get("json"), payload,
            )
            text = str(payload).lower()
            if "no numbers" in text or "not available" in text or "out of stock" in text:
                raise NoNumbersAvailable(resp.status_code, payload)
            if "balance" in text or "funds" in text or "insufficient" in text:
                raise InsufficientFunds(resp.status_code, payload)
            raise GotSmsError(resp.status_code, payload)

        return payload

    async def balance(self) -> float:
        data = await self._request("GET", "/api/account")
        return float(data.get("data", {}).get("balance", 0))

    async def services(self, search: str | None = None, page: int = 1, per_page: int = 50) -> tuple[list[Service], dict]:
        cache_key = f"services:{search or ''}:{page}:{per_page}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached
        async with self._lock_for(cache_key):
            cached = self._cache_get(cache_key)
            if cached is not None:
                return cached
            params: dict[str, Any] = {"page": page, "per_page": per_page}
            if search:
                params["search"] = search
            data = await self._request("GET", "/api/services", params=params)
            items = [Service(id=str(x["id"]), name=x["name"]) for x in data.get("data", [])]
            result = (items, data.get("meta", {}))
            self._cache_set(cache_key, result)
            return result

    async def services_all(self, per_page: int = 200) -> list[Service]:
        items, _ = await self.services(page=1, per_page=per_page)
        return items

    async def services_full(self, per_page: int = 200) -> list[Service]:
        """All services across pages, sorted by name. Cached as a single bundle."""
        cache_key = f"services_full:{per_page}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached
        async with self._lock_for(cache_key):
            cached = self._cache_get(cache_key)
            if cached is not None:
                return cached
            first_items, meta = await self.services(page=1, per_page=per_page)
            last_page = int(meta.get("last_page", 1) or 1)
            all_items: list[Service] = list(first_items)
            if last_page > 1:
                sem = asyncio.Semaphore(4)

                async def fetch(p: int) -> list[Service]:
                    async with sem:
                        its, _ = await self.services(page=p, per_page=per_page)
                        return its

                results = await asyncio.gather(
                    *[fetch(p) for p in range(2, last_page + 1)],
                    return_exceptions=True,
                )
                for r in results:
                    if isinstance(r, list):
                        all_items.extend(r)
            all_items.sort(key=lambda s: s.name.lower())
            self._cache_set(cache_key, all_items)
            return all_items

    async def plans(
        self,
        service_id: str | None = None,
        country_id: str | None = None,
        duration_type: str | None = None,
        billing_type: str | None = None,
        page: int = 1,
        per_page: int = 50,
        use_cache: bool = True,
    ) -> tuple[list[Plan], dict]:
        cache_key = f"plans:{service_id or ''}:{country_id or ''}:{duration_type or ''}:{billing_type or ''}:{page}:{per_page}"
        if use_cache:
            cached = self._cache_get(cache_key)
            if cached is not None:
                return cached
        async with self._lock_for(cache_key):
            if use_cache:
                cached = self._cache_get(cache_key)
                if cached is not None:
                    return cached
            params: dict[str, Any] = {"page": page, "per_page": per_page}
            if service_id:
                params["service_id"] = service_id
            if country_id:
                params["country_id"] = country_id
            if duration_type:
                params["duration_type"] = duration_type
            if billing_type:
                params["billing_type"] = billing_type
            data = await self._request("GET", "/api/rents/plans", params=params)
            items = []
            for x in data.get("data", []):
                service = x.get("service") or {}
                country = x.get("country") or {}
                items.append(
                    Plan(
                        id=str(x["id"]),
                        service_id=str(service.get("id", "")),
                        service_name=service.get("name", "—"),
                        country_name=country.get("name"),
                        duration=str(x.get("duration", "")),
                        duration_type=str(x.get("duration_type", "")),
                        billing_type=str(x.get("billing_type", "")),
                        price=_extract_price(x),
                        raw=x,
                    )
                )
            result = (items, data.get("meta", {}))
            self._cache_set(cache_key, result)
            return result

    async def plans_all(self, service_id: str, per_page: int = 200) -> list[Plan]:
        items, _ = await self.plans(service_id=service_id, page=1, per_page=per_page)
        return items

    async def create_rent(self, plan_id: str, area_code: str | None = None) -> Rent:
        body: dict[str, Any] = {"plan_id": plan_id}
        if area_code:
            body["area_code"] = area_code
        data = await self._request("POST", "/api/rents", json=body)
        item = data.get("data", data)
        return self._rent_from(item)

    async def list_rents(self, status: str = "active", per_page: int = 50) -> list[Rent]:
        data = await self._request("GET", "/api/rents", params={"status": status, "per_page": per_page})
        return [self._rent_from(x) for x in data.get("data", [])]

    async def refund_rent(self, rent_id: str) -> dict:
        return await self._request("POST", f"/api/rents/{rent_id}/refund")

    async def unread_messages(self, mark_as_read: bool = True, per_page: int = 50) -> list[Message]:
        params = {"mark_as_read": "true" if mark_as_read else "false", "per_page": per_page}
        data = await self._request("GET", "/api/messages/unread", params=params)
        return [self._msg_from(x) for x in data.get("data", [])]

    async def rent_messages(self, rent_id: str, limit: int = 20) -> list[Message]:
        data = await self._request("GET", f"/api/numbers/{rent_id}/messages", params={"limit": limit})
        return [self._msg_from(x) for x in data.get("data", [])]

    @staticmethod
    def _rent_from(x: dict) -> Rent:
        service = x.get("service") or {}
        return Rent(
            id=str(x["id"]),
            service_name=service.get("name", "—"),
            phone=str(x.get("phone", "")),
            price=float(x.get("price", 0) or 0),
            status=str(x.get("status", "")),
            active_from=x.get("active_from"),
            active_till=x.get("active_till"),
        )

    @staticmethod
    def _msg_from(x: dict) -> Message:
        service = x.get("service") or {}
        return Message(
            id=str(x["id"]),
            rent_id=str(x.get("rent_id", "")),
            service_name=service.get("name", "—"),
            phone=str(x.get("phone", "")),
            sender=str(x.get("from", "")),
            body=str(x.get("body", "")),
            code=x.get("code"),
            received_at=str(x.get("received_at", "")),
        )
