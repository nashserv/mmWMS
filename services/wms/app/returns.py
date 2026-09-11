"""Возвраты — контрактные маршруты минимально: принять и решить.

Раздел 6.7 просит ровно это и ничего сверх: возвраты подняты, но почти не
используются (раздел 2.3), а вопрос 10 раздела 13 («сейчас или следующая
фаза») ещё открыт. Поэтому здесь нет своего процесса поверх контракта — есть
приём, решение и движение товара туда, куда решили.

`wb-returns` после вывода Odoo зовёт `wms` по этому же контракту (раздел 6.8).
"""
from __future__ import annotations

import uuid
from datetime import datetime, UTC
from typing import Any
from collections.abc import Callable

from . import repositories as repo
from .postgres import ConnectionPool, single, transaction
from .service import WmsService

# Куда попадает вещь по решению. Годная возвращается в оборот, брак — в
# состояние defect: он остаётся остатком владельца, но продать его нельзя.
DECISION_STATE = {"resellable": "good", "defective": "defect"}


class ReturnOperations:
    def __init__(self, pool: ConnectionPool, service: WmsService,
                 on_stock_changed: Callable[[uuid.UUID, set[uuid.UUID]], None] | None = None
                 ) -> None:
        self._pool = pool
        self._service = service
        self._on_stock_changed = on_stock_changed

    def expect(self, task_id: str, params: dict[str, Any]) -> dict[str, Any]:
        """`/tasks/{id}/return` — возврат ожидается по заданию."""
        event_id = str(params.get("return_event_id") or "").strip()
        if not event_id:
            raise ValueError("return_event_id обязателен: это ключ идемпотентности")
        with self._pool.connection() as connection:
            with transaction(connection) as cursor:
                task = repo.task_view(cursor, _uuid(task_id))
                if task is None:
                    raise ValueError(f"задание {task_id} не найдено")
                row, created = repo.upsert_return(
                    cursor, return_event_id=event_id, owner_id=task["owner_id"],
                    task_id=task["id"], reason=_text(params.get("reason")))
                if created:
                    self._service.emit_for_aggregate(
                        cursor, aggregate_id=row["id"],
                        event_type="wms.return.expected.v1",
                        payload={"return_id": str(row["id"]), "task_id": str(task["id"]),
                                 "owner_id": str(task["owner_id"]),
                                 "reason": _text(params.get("reason"))},
                        correlation_id=event_id)
        return {"return_id": str(row["id"]),
                "owner_external_id": task.get("seller_external_id"),
                "duplicate": not created}

    def receive(self, return_id: str, params: dict[str, Any]) -> dict[str, Any]:
        """`/returns/{id}/receive` — вещь физически приехала."""
        with self._pool.connection() as connection:
            with transaction(connection) as cursor:
                row = repo.return_by_id_or_event(cursor, return_id)
                if row is None:
                    row, _ = repo.upsert_return(
                        cursor, return_event_id=return_id,
                        owner_id=self._owner(cursor, params), task_id=None,
                        reason=_text(params.get("reason")))
                if row["state"] == "expected":
                    repo.set_return_state(cursor, row["id"], "received")
                    self._service.emit_for_aggregate(
                        cursor, aggregate_id=row["id"],
                        event_type="wms.return.received.v1",
                        payload={"return_id": str(row["id"]),
                                 "owner_id": str(row["owner_id"]),
                                 "barcode": _text(params.get("barcode"))},
                        correlation_id=str(return_id))
                    row["state"] = "received"
        return {"return_id": str(row["id"]), "state": row["state"],
                # Владелец обязателен: возврат чужого товара не бывает
                # безымянным (инвариант 6).
                "owner_external_id": _owner_external_id(row),
                "task_id": str(row["task_id"]) if row.get("task_id") else None,
                "duplicate": row["state"] != "received"}

    def decide(self, return_id: str, params: dict[str, Any]) -> dict[str, Any]:
        """`/returns/{id}/decision` — годен или брак, и товар едет по решению."""
        decision = str(params.get("decision") or "").strip()
        if decision not in DECISION_STATE:
            raise ValueError("decision: resellable или defective")

        touched: set[uuid.UUID] = set()
        owner_id: uuid.UUID | None = None
        with self._pool.connection() as connection:
            with transaction(connection) as cursor:
                row = repo.return_by_id_or_event(cursor, return_id)
                if row is None:
                    raise ValueError(f"возврат {return_id} не найден")
                if row["state"] == "decided":
                    return {"return_id": str(row["id"]), "state": row["state"],
                            "decision": row["decision"], "duplicate": True}
                owner_id = row["owner_id"]

                barcode = _text(params.get("barcode"))
                if barcode:
                    sku, error = repo.find_sku(cursor, row["owner_id"], barcode=barcode,
                                               sku_field=None)
                    if sku is None:
                        raise ValueError(f"товар {barcode!r}: {error.value if error else ''}")
                    cell = repo.ensure_cell(
                        cursor, _text(params.get("cell_address")) or "RUM-RETURNS",
                        zone_code="RETURNS", zone_kind="quarantine")
                    repo.insert_move(
                        cursor, owner_id=row["owner_id"], sku_id=sku["id"],
                        qty=max(1, int(params.get("quantity") or 1)),
                        cell_to=cell["id"], state_to=DECISION_STATE[decision],
                        reason=f"return_{decision}", doc_type="return",
                        doc_ref=str(row["id"]),
                        idem_key=f"return:{row['id']}:{decision}")
                    touched.add(sku["id"])

                repo.decide_return(cursor, row["id"], decision)
                self._service.emit_for_aggregate(
                    cursor, aggregate_id=row["id"],
                    event_type=f"wms.return.{decision}.v1",
                    payload={"return_id": str(row["id"]),
                             "owner_id": str(row["owner_id"]),
                             "decision": decision},
                    correlation_id=str(return_id))
                row["state"], row["decision"] = "decided", decision

        if owner_id is not None and touched and self._on_stock_changed is not None:
            self._on_stock_changed(owner_id, touched)
        return {"return_id": str(row["id"]), "state": "decided", "decision": decision,
                "owner_external_id": _owner_external_id(row),
                "task_id": str(row["task_id"]) if row.get("task_id") else None,
                "duplicate": False}

    def receipt(self, params: dict[str, Any]) -> dict[str, Any]:
        """`/returns/receipt` — список возвратов для экрана."""
        limit = min(int(params.get("limit") or 100), 500)
        with self._pool.connection() as connection:
            with single(connection) as cursor:
                rows = repo.returns_list(
                    cursor, owner_external_id=_text(params.get("seller_external_id")),
                    states=params.get("states"), limit=limit)
        # Форма — ReturnsReceiptResult: документ приёмки возвратов.
        return {
            "reference": _text(params.get("reference")) or f"returns:{_now_iso()}",
            "returns": [{
                "return_id": row["return_id"],
                "owner_external_id": row.get("owner_external_id")
                                     or row.get("seller_external_id") or "",
                "task_id": row.get("task_id"),
                "state": row["state"], "decision": row.get("decision"),
                "received_at": _isoformat(row.get("received_at"))} for row in rows],
            # Сколько строк не удалось привязать к заданию. Ноль не гарантирован:
            # возврат может приехать раньше, чем WB отдаст связь.
            "unmatched": sum(1 for row in rows if not row.get("task_id")),
            "duplicate": False}

    def _owner(self, cursor: Any, params: dict[str, Any]) -> uuid.UUID:
        seller = _text(params.get("seller_external_id"))
        owner = repo.find_owner(cursor, seller) if seller else None
        if owner is None:
            raise ValueError("seller_external_id обязателен для неизвестного возврата")
        return owner["id"]


def _uuid(value: Any) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError):
        raise ValueError(f"{value!r} не похоже на идентификатор") from None


def _owner_external_id(row: dict[str, Any]) -> str:
    """Внешний идентификатор владельца возврата.

    Обязателен по контракту: возврат чужого товара не бывает безымянным
    (инвариант 6).
    """
    return str(row.get("seller_external_id") or row.get("owner_external_id") or "")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _isoformat(value: Any) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
