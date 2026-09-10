"""Клиент сервиса wms по контракту /api/mmx/wms/v1/* (приложение B мастера).

Контракт сохранён специально ради таких клиентов (раздел 6.7), и поток C
работает против него, а не против кода потока A: до готовности A на том же
адресе стоит mock потока 0 — правило 9.5.4, никто никого не ждёт.

Все вызовы — снаружи транзакций базы биллинга (инвариант 2).
"""
from __future__ import annotations

import os
from typing import Any

import httpx


class WmsUnavailable(RuntimeError):
    """`wms` не ответил. Онбординг останавливается, а не идёт дальше вслепую."""


class WmsClient:
    def __init__(self, base_url: str | None = None, *, timeout: float = 15.0) -> None:
        self.base_url = (base_url or os.getenv("WMS_BASE_URL", "http://wms:8080")).rstrip("/")
        self.path = os.getenv("WMS_API_PATH", "/api/mmx/wms/v1")
        self._timeout = timeout

    def call(self, route: str, params: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url}{self.path}{route}"
        body = {"jsonrpc": "2.0", "method": "call", "id": 1, "params": params}
        try:
            response = httpx.post(url, json=body, timeout=self._timeout)
        except httpx.HTTPError as failure:
            raise WmsUnavailable(f"{route}: {failure}") from failure
        if response.status_code >= 400:
            raise WmsUnavailable(f"{route}: HTTP {response.status_code}")
        payload = response.json()
        if isinstance(payload, dict) and "result" in payload:
            return payload["result"] or {}
        return payload if isinstance(payload, dict) else {}

    # --- шаги онбординга ---------------------------------------------------

    def ensure_owner(self, seller_external_id: str, name: str, inn: str | None) -> dict[str, Any]:
        return self.call("/sellers", {
            "seller_external_id": seller_external_id, "name": name, "inn": inn,
            "active": True, "allow_ledger_short": True})

    def connect_wb_account(self, external_id: str, seller_external_id: str,
                           display_name: str, secret_ref: str) -> dict[str, Any]:
        """Подключает кабинет WB по ссылке на секрет.

        Передаётся `secret_ref`, а не токен: значение секрета не покидает
        секрет-провайдер и не попадает ни в базу, ни в лог, ни в событие
        (инвариант 15). Проверка формы — в admin.check_secret_ref.
        """
        return self.call("/wb/accounts", {
            "op": "upsert", "external_id": external_id,
            "seller_external_id": seller_external_id, "display_name": display_name,
            "secret_ref": secret_ref, "mode": "shadow", "status": "ACTIVE"})

    def load_opening_stock(self, seller_external_id: str, reference: str,
                           lines: list[dict[str, Any]], warehouse_code: str = "RUM",
                           comment: str = "начальный остаток от владельца компании") -> dict[str, Any]:
        """Начальный остаток даёт владелец компании (решение владельца 11).

        Документом, а не правкой баланса: движение с doc_type='opening'
        обязано остаться в журнале (шаг 1 полного прогона).
        """
        return self.call("/warehouse/documents", {
            "seller_external_id": seller_external_id, "warehouse_code": warehouse_code,
            "reference": reference, "doc_type": "opening", "comment": comment,
            "lines": lines})

    def ensure_product(self, seller_external_id: str, barcode: str,
                       seller_sku: str | None = None, name: str | None = None) -> dict[str, Any]:
        return self.call("/catalog/products/ensure", {
            "seller_external_id": seller_external_id, "barcode": barcode,
            "seller_sku": seller_sku, "name": name})
