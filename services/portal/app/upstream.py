"""Соседи портала: склад, биллинг, identity.

Портал ничего не считает и почти ничего не хранит. Его работа — показать
клиенту то, что уже есть в `wms` и `billing`, и показать честно: остаток
берётся у склада, а не из своего зеркала (файл 04), деньги — у биллинга
вместе с наценкой партнёра, а не итогом.
"""
from __future__ import annotations

import json
from typing import Any

import httpx

from .config import billing_url, identity_url, wms_url


class Upstream(RuntimeError):
    """Сервис за порталом не ответил. Показывать нечего — так и скажем."""


class WmsClient:
    """Контракт /api/mmx/wms/v1/* (приложение B мастера)."""

    def __init__(self, base_url: str | None = None, *, timeout: float = 10.0) -> None:
        self.base_url = (base_url or wms_url()).rstrip("/")
        self.path = "/api/mmx/wms/v1"
        self._timeout = timeout

    def call(self, route: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            response = httpx.post(f"{self.base_url}{self.path}{route}", timeout=self._timeout,
                                  json={"jsonrpc": "2.0", "method": "call", "id": 1,
                                        "params": params})
        except httpx.HTTPError as failure:
            raise Upstream(f"склад недоступен ({route}): {failure}") from failure
        if response.status_code >= 400:
            raise Upstream(f"склад ответил {response.status_code} на {route}")
        body = response.json()
        return (body.get("result") or {}) if isinstance(body, dict) else {}

    def stock(self, seller: str) -> list[dict[str, Any]]:
        return self.call("/warehouse/stock",
                         {"seller_external_id": seller, "warehouse_code": "RUM"}).get("stock", [])

    def placements(self, seller: str, barcode: str | None = None) -> list[dict[str, Any]]:
        """Где именно лежит товар: коробка, ячейка, комментарий.

        Коробка — фактическая единица адресации склада (раздел 2.9), и клиент
        имеет право видеть её так же, как видит сборщик.
        """
        params: dict[str, Any] = {"seller_external_id": seller}
        if barcode:
            params["barcode"] = barcode
        return self.call("/storage/lookup", params).get("placements", [])


class BillingClient:
    def __init__(self, base_url: str | None = None, *, timeout: float = 10.0) -> None:
        self.base_url = (base_url or billing_url()).rstrip("/")
        self._timeout = timeout

    def get(self, path: str, authorization: str | None,
            params: dict[str, Any] | None = None) -> tuple[int, Any]:
        headers = {"Authorization": authorization} if authorization else {}
        try:
            response = httpx.get(f"{self.base_url}{path}", headers=headers, params=params,
                                 timeout=self._timeout)
        except httpx.HTTPError as failure:
            raise Upstream(f"биллинг недоступен: {failure}") from failure
        try:
            return response.status_code, (response.json() if response.content else {})
        except json.JSONDecodeError:
            return response.status_code, {"error": response.text[:500]}


class IdentityClient:
    def __init__(self, base_url: str | None = None, *, timeout: float = 5.0) -> None:
        self.base_url = (base_url or identity_url()).rstrip("/")
        self._timeout = timeout

    def whoami(self, authorization: str | None) -> dict[str, Any]:
        token = ""
        if authorization and authorization.lower().startswith("bearer "):
            token = authorization[7:].strip()
        if not token:
            return {"active": False, "reason": "нужен заголовок Authorization: Bearer <токен>"}
        try:
            response = httpx.post(f"{self.base_url}/api/identity/v1/introspect",
                                  json={"token": token}, timeout=self._timeout)
        except httpx.HTTPError as failure:
            raise Upstream(f"identity недоступен: {failure}") from failure
        return response.json() if response.content else {"active": False}
