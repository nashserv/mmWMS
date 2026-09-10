"""Экраны приёмки, размещения и инвентаризации.

Они живут здесь, а не в отдельном фронте (владение потока B), и работают на
пять приёмщиков одновременно.

Главное правило этих экранов: **баланс двигается по факту, а не по ожиданию**.
Приёмщик вводит то, что реально пересчитал; расхождение оформляется типом и
решением — «кто виноват и что делать», — а не подгонкой числа под накладную.

Ответы wms читаются по контракту, но понимаются и в форме заглушки потока 0 —
со счётчиком на каждое такое чтение. Разойтись с контрактом молча значит
обнаружить это в день переключения на настоящий сервис.
"""
from __future__ import annotations

import logging
from typing import Any, Iterable

from . import metrics
from .domain import now, uid
from .wms_client import WmsClient, WmsRejected, WmsUnavailable

logger = logging.getLogger("workstation.receiving")

# Типы расхождений приёмки. `ledger_short` сюда не входит: он рождается не на
# приёмке, а на сборке без остатка (клапан раздела 6.5), и живёт на экране
# начальника склада.
RECEIVING_DISCREPANCY_KINDS = ("shortage", "surplus", "mismatch", "damage")
LIABLE = ("owner", "warehouse", "carrier", "unknown")


class ReceivingRefused(RuntimeError):
    pass


def _rows(result: dict[str, Any], route: str, field: str,
          contract_key: str, *alternatives: str) -> list[dict[str, Any]]:
    """Список по контракту, при отсутствии — по запасному имени, но со счётчиком."""
    value = result.get(contract_key)
    if isinstance(value, list):
        return [row for row in value if isinstance(row, dict)]
    for name in alternatives:
        candidate = result.get(name)
        if isinstance(candidate, list):
            metrics.CONTRACT_FALLBACKS.labels(route=route, field=field).inc()
            return [row for row in candidate if isinstance(row, dict)]
    return []


def _owner_of(row: dict[str, Any], route: str) -> str | None:
    """Владелец в ответе. Контракт называет его owner_external_id.

    Заглушка отдаёт seller_external_id — то же значение под старым именем.
    Понимаем оба, но считаем: перепутанный владелец это отгрузка чужой вещи.
    """
    value = row.get("owner_external_id")
    if value:
        return str(value)
    value = row.get("seller_external_id")
    if value:
        metrics.CONTRACT_FALLBACKS.labels(route=route, field="owner_external_id").inc()
        return str(value)
    return None


class ReceivingService:
    def __init__(self, client: WmsClient) -> None:
        self._client = client

    # ------------------------------------------------------------ приёмка

    async def receipts_screen(self, *, owner_external_id: str | None = None,
                              reference: str | None = None,
                              limit: int = 50) -> dict[str, Any]:
        """Всё, что нужно экрану приёмщика, одним вызовом.

        Одним, а не пятью: на разгрузке каждый лишний круг стоит времени, а
        машина стоит под разгрузкой.
        """
        params: dict[str, Any] = {"limit": int(limit)}
        if owner_external_id:
            params["owner_external_id"] = owner_external_id
        if reference:
            params["reference"] = reference
        try:
            result = await self._client.call("/receipts/screen", params, attempts=2)
        except WmsUnavailable as error:
            raise ReceivingRefused(f"wms недоступен: {error}") from error

        receipts = _rows(result, "/receipts/screen", "receipts", "receipts", "open_receipts")
        normalised = []
        for row in receipts:
            normalised.append({
                "receipt_id": row.get("receipt_id"),
                "reference": row.get("reference"),
                "owner_external_id": _owner_of(row, "/receipts/screen"),
                "owner_name": row.get("owner_name"),
                "state": row.get("state"),
                "created_at": row.get("created_at"),
                "lines": [self._receipt_line(line) for line in (row.get("lines") or [])
                          if isinstance(line, dict)],
                "discrepancies": [self._discrepancy(item)
                                  for item in (row.get("discrepancies") or [])
                                  if isinstance(item, dict)],
            })
        return {
            "receipts": normalised,
            "generated_at": result.get("generated_at") or now(),
            "discrepancy_kinds": list(RECEIVING_DISCREPANCY_KINDS),
            "liable_options": list(LIABLE),
        }

    @staticmethod
    def _receipt_line(line: dict[str, Any]) -> dict[str, Any]:
        return {
            "barcode": line.get("barcode"),
            "seller_sku": line.get("seller_sku"),
            "name": line.get("name"),
            "expected_qty": line.get("expected_qty"),
            "actual_qty": line.get("actual_qty"),
            "box_barcode": line.get("box_barcode"),
            "cell_address": line.get("cell_address"),
            "comment": line.get("comment"),
        }

    @staticmethod
    def _discrepancy(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "discrepancy_id": item.get("discrepancy_id"),
            "kind": item.get("kind"),
            "barcode": item.get("barcode"),
            "qty": item.get("qty"),
            "decision": item.get("decision"),
            "liable": item.get("liable"),
            "comment": item.get("comment"),
            "cell_address": item.get("cell_address"),
            "created_at": item.get("created_at"),
        }

    async def submit_receipt(self, *, seller_external_id: str, reference: str,
                             lines: list[dict[str, Any]], warehouse_code: str = "RUM",
                             seller_name: str | None = None, seller_inn: str | None = None,
                             actor_id: str | None = None) -> dict[str, Any]:
        """Отправить пересчёт приёмки.

        `reference` — ключ идемпотентности (инвариант 5): повторно отправленная
        приёмка не должна принять товар дважды. Поэтому он приходит с экрана и
        не генерируется здесь заново на каждое нажатие.
        """
        if not (reference or "").strip():
            raise ReceivingRefused("приёмка без номера документа не отправляется: "
                                   "номер — ключ идемпотентности")
        clean = [self._clean_line(line) for line in lines]
        clean = [line for line in clean if line]
        if not clean:
            raise ReceivingRefused("в приёмке нет ни одной строки")

        params: dict[str, Any] = {
            "seller_external_id": seller_external_id,
            "warehouse_code": warehouse_code,
            "reference": reference,
            "lines": clean,
        }
        # Владельца, которого склад видит впервые, приёмка заводит сама: товар
        # уже приехал, и держать машину под разгрузкой из-за карточки нельзя.
        if seller_name:
            params["seller_name"] = seller_name
        if seller_inn:
            params["seller_inn"] = seller_inn
        if actor_id:
            params["actor_id"] = actor_id

        try:
            result = await self._client.call("/receipts", params, attempts=2)
        except WmsUnavailable as error:
            raise ReceivingRefused(f"wms недоступен: {error}") from error
        if result.get("error_code"):
            raise ReceivingRefused(f"приёмка отклонена: {result['error_code']}")

        return {
            "receipt_id": result.get("receipt_id"),
            "reference": result.get("reference") or reference,
            "owner_external_id": _owner_of(result, "/receipts"),
            "owner_created": bool(result.get("owner_created")),
            "state": result.get("state"),
            "duplicate": bool(result.get("duplicate")),
            "discrepancies": [self._discrepancy(item)
                              for item in (result.get("discrepancies") or [])
                              if isinstance(item, dict)],
        }

    @staticmethod
    def _clean_line(line: dict[str, Any]) -> dict[str, Any] | None:
        barcode = str(line.get("barcode") or "").strip()
        if not barcode:
            return None
        row: dict[str, Any] = {"barcode": barcode}
        for key in ("seller_sku", "name", "box_barcode", "cell_address", "comment"):
            value = line.get(key)
            if value not in (None, ""):
                row[key] = str(value)
        expected = line.get("expected_qty")
        actual = line.get("actual_qty")
        row["expected_qty"] = int(expected) if expected not in (None, "") else 0
        if actual not in (None, ""):
            # Факт отправляется, даже если он равен нулю: ноль по факту это
            # «ничего не приехало», а не «строку не заполнили».
            row["actual_qty"] = int(actual)
        return row

    # ------------------------------------------------------------ размещение

    async def putaway_screen(self, *, owner_external_id: str | None = None,
                             reference: str | None = None,
                             limit: int = 50) -> dict[str, Any]:
        """Что принято, но ещё не разложено, и куда это класть."""
        params: dict[str, Any] = {"limit": int(limit)}
        if owner_external_id:
            params["owner_external_id"] = owner_external_id
        if reference:
            params["reference"] = reference
        try:
            result = await self._client.call("/putaway/screen", params, attempts=2)
        except WmsUnavailable as error:
            raise ReceivingRefused(f"wms недоступен: {error}") from error

        items = _rows(result, "/putaway/screen", "items", "items", "pending")
        cells = _rows(result, "/putaway/screen", "cells", "cells")
        normalised = []
        for row in items:
            suggestions = [
                {
                    "cell_address": cell.get("cell_address") or cell.get("address"),
                    "route_order": cell.get("route_order"),
                    "free_capacity": cell.get("free_capacity"),
                    "holds_same_sku": bool(cell.get("holds_same_sku")),
                }
                for cell in (row.get("suggested_cells") or []) if isinstance(cell, dict)
            ]
            if not suggestions and cells:
                # Заглушка отдаёт общий список ячеек вместо подсказки на
                # позицию. Подсказка не обязательна к исполнению, но пустой
                # экран размещения бесполезен.
                metrics.CONTRACT_FALLBACKS.labels(route="/putaway/screen",
                                                  field="suggested_cells").inc()
                suggestions = sorted(
                    ({"cell_address": cell.get("address") or cell.get("cell_address"),
                      "route_order": cell.get("route_order"),
                      "free_capacity": cell.get("free_capacity"),
                      "holds_same_sku": False} for cell in cells),
                    key=lambda item: (item["route_order"] is None, item["route_order"] or 0))[:5]
            normalised.append({
                "owner_external_id": _owner_of(row, "/putaway/screen"),
                "barcode": row.get("barcode"),
                "name": row.get("name"),
                "qty_to_place": row.get("qty_to_place"),
                "source_reference": row.get("source_reference") or row.get("reference"),
                "suggested_cells": suggestions,
                "boxes": row.get("boxes") or [],
                "lines": row.get("lines") or [],
            })
        return {"items": normalised, "generated_at": result.get("generated_at") or now(),
                "cells": cells}

    async def place_in_box(self, *, barcode: str, seller_external_id: str, comment: str,
                           product_barcode: str | None = None,
                           cell_address: str | None = None, quantity: int = 0,
                           counted: bool = False, sequence: int | None = None,
                           total_boxes: int | None = None,
                           actor_id: str | None = None) -> dict[str, Any]:
        """Положить принятое в коробку и поставить коробку в ячейку.

        Комментарий обязателен — это требование склада, а не формальность:
        через месяц стоят сотни одинаковых коробок, и без пометки нужную не
        найти (раздел 2.9 мастера). Пустая строка комментарием не считается.
        """
        if not (comment or "").strip():
            raise ReceivingRefused("коробка без комментария не заводится: "
                                   "через месяц её не найти среди сотни одинаковых")
        params: dict[str, Any] = {
            "barcode": barcode,
            "seller_external_id": seller_external_id,
            "comment": comment.strip(),
            "quantity": int(quantity),
            # quantity = 0 значит «не считали», а не «пусто» — различает флаг.
            "counted": bool(counted),
        }
        if product_barcode:
            params["product_barcode"] = product_barcode
        if cell_address:
            params["cell_address"] = cell_address
        if sequence:
            params["sequence"] = int(sequence)
        if total_boxes:
            params["total_boxes"] = int(total_boxes)
        if actor_id:
            params["actor_id"] = actor_id

        try:
            result = await self._client.call("/boxes", params, attempts=2)
        except WmsUnavailable as error:
            raise ReceivingRefused(f"wms недоступен: {error}") from error
        if result.get("error_code"):
            raise ReceivingRefused(f"коробка не заведена: {result['error_code']}")
        box = result.get("box") if isinstance(result.get("box"), dict) else result
        return {"ok": True, "box": box, "created": result.get("created")}

    async def boxes(self, *, seller_external_id: str | None = None,
                    cell_address: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if seller_external_id:
            params["seller_external_id"] = seller_external_id
        if cell_address:
            params["cell_address"] = cell_address
        try:
            result = await self._client.call("/boxes/list", params, attempts=2)
        except WmsUnavailable as error:
            raise ReceivingRefused(f"wms недоступен: {error}") from error
        return _rows(result, "/boxes/list", "boxes", "boxes")

    # ------------------------------------------------------------ инвентаризация

    async def inventory_sheet(self, *, seller_external_id: str, scope: str = "partial",
                              cell_addresses: Iterable[str] | None = None,
                              barcodes: Iterable[str] | None = None) -> dict[str, Any]:
        """Лист для пересчёта.

        Ожидаемое количество отдаётся отдельным полем, чтобы экран мог его
        скрыть: считающий не должен видеть цифру до ввода факта, иначе он
        пересчитывает не полку, а бумажку.
        """
        params: dict[str, Any] = {"seller_external_id": seller_external_id, "scope": scope}
        if cell_addresses:
            params["cell_addresses"] = list(cell_addresses)
        if barcodes:
            params["barcodes"] = list(barcodes)
        try:
            result = await self._client.call("/inventory/sheet", params, attempts=2)
        except WmsUnavailable as error:
            raise ReceivingRefused(f"wms недоступен: {error}") from error
        lines = _rows(result, "/inventory/sheet", "lines", "lines")
        return {
            "owner_external_id": _owner_of(result, "/inventory/sheet") or seller_external_id,
            "lines": [{
                "barcode": line.get("barcode"),
                "name": line.get("name"),
                "cell_address": line.get("cell_address") or line.get("cell"),
                "box_barcode": line.get("box_barcode"),
                "expected_qty": line.get("expected_qty"),
                "route_order": line.get("route_order"),
            } for line in lines],
            "generated_at": result.get("generated_at") or now(),
            "scope": scope,
        }

    async def submit_count(self, *, seller_external_id: str, reference: str, scope: str,
                           lines: list[dict[str, Any]], warehouse_code: str = "RUM",
                           actor_id: str | None = None) -> dict[str, Any]:
        """Применить пересчёт. Излишки приходуются, недостачи списываются.

        Логика применения — на стороне wms (поток A); здесь только интерфейс и
        та же идемпотентность по `reference`.
        """
        if not (reference or "").strip():
            raise ReceivingRefused("инвентаризация без номера документа не отправляется")
        clean = []
        for line in lines:
            barcode = str(line.get("barcode") or "").strip()
            fact = line.get("fact_qty")
            if not barcode or fact in (None, ""):
                continue
            row: dict[str, Any] = {"barcode": barcode, "fact_qty": int(fact)}
            for key in ("cell_address", "box_barcode"):
                if line.get(key):
                    row[key] = str(line[key])
            if line.get("expected_qty") not in (None, ""):
                row["expected_qty"] = int(line["expected_qty"])
            clean.append(row)
        if not clean:
            raise ReceivingRefused("ни в одной строке не введён факт")

        params: dict[str, Any] = {
            "seller_external_id": seller_external_id,
            "warehouse_code": warehouse_code,
            "reference": reference,
            "scope": scope,
            "lines": clean,
        }
        if actor_id:
            params["actor_id"] = actor_id
        try:
            result = await self._client.call("/inventory/count", params, attempts=2)
        except WmsUnavailable as error:
            raise ReceivingRefused(f"wms недоступен: {error}") from error
        if result.get("error_code"):
            raise ReceivingRefused(f"инвентаризация отклонена: {result['error_code']}")
        return {
            "count_id": result.get("count_id"),
            "reference": result.get("reference") or reference,
            "state": result.get("state"),
            "moves": result.get("moves"),
            "duplicate": bool(result.get("duplicate")),
            "discrepancies": [self._discrepancy(item)
                              for item in (result.get("discrepancies") or [])
                              if isinstance(item, dict)],
        }

    # ------------------------------------------------------------ отгрузка

    async def picked_tasks(self, *, seller_external_id: str | None = None,
                           limit: int = 100) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": int(limit)}
        if seller_external_id:
            params["seller_external_id"] = seller_external_id
        try:
            result = await self._client.call("/shipments/picked", params, attempts=2)
        except WmsUnavailable as error:
            raise ReceivingRefused(f"wms недоступен: {error}") from error
        return _rows(result, "/shipments/picked", "tasks", "tasks", "shipments")

    async def shipment(self, *, seller_external_id: str, action: str,
                       wb_supply_id: str | None = None,
                       task_ids: list[str] | None = None,
                       handed_over_by: str | None = None,
                       idempotency_key: str | None = None) -> dict[str, Any]:
        """Действие над поставкой, включая подтверждение передачи человеком.

        `hand_over` требует подписи: `HANDED_TO_WB` ставится только живым
        человеком, статус WB `complete` приёмку не доказывает (раздел 2.12).
        Сегодня подтверждено меньше 1 % отгруженных — цель 95 %.
        """
        if action == "hand_over" and not (handed_over_by or "").strip():
            raise ReceivingRefused(
                "передача поставки подтверждается человеком: нужна подпись")
        params: dict[str, Any] = {
            "seller_external_id": seller_external_id,
            "action": action,
            "idempotency_key": idempotency_key or f"ws-shipment-{action}-{uid()}",
        }
        if wb_supply_id:
            params["wb_supply_id"] = wb_supply_id
        if task_ids:
            params["task_ids"] = list(task_ids)[:100]
        if handed_over_by:
            params["handed_over_by"] = handed_over_by
        try:
            result = await self._client.call("/shipments", params, attempts=2)
        except WmsRejected as error:
            raise ReceivingRefused(f"поставка отклонена: {error.code}") from error
        except WmsUnavailable as error:
            raise ReceivingRefused(f"wms недоступен: {error}") from error
        if result.get("error_code"):
            raise ReceivingRefused(f"поставка отклонена: {result['error_code']}")
        return {
            "shipment_id": result.get("shipment_id"),
            "owner_external_id": _owner_of(result, "/shipments"),
            "wb_supply_id": result.get("wb_supply_id"),
            "state": result.get("state"),
            "handed_by": result.get("handed_by"),
            "handed_at": result.get("handed_at"),
            "orders": result.get("orders"),
            "duplicate": bool(result.get("duplicate")),
        }
