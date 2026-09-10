"""Задания: выдача сборщикам, скан, упаковка, отмена.

Выдача — та самая, из раздела 7:

    SELECT ... FROM wms_task
     WHERE state = 'reserved' AND assignee IS NULL
     ORDER BY deadline                     -- срок WB, не время создания
     FOR UPDATE SKIP LOCKED
     LIMIT $n

Пять сборщиков работают одновременно (раздел 4). `SKIP LOCKED` означает, что
второй сборщик берёт следующее задание, а не ждёт первого; лизинг
(`claim_expires_at`) означает, что задание вернётся в очередь, если сборщик
ушёл со смены посреди обхода. Ни выдать одно задание двоим, ни потерять его.

Сортировка по сроку Wildberries, а не по времени создания: просроченное задание
дороже свежего.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Callable

from . import repositories as repo
from .domain import TaskState, now
from .postgres import ConnectionPool, single, transaction
from .service import Emitted, WmsService
from .metrics import track_lock

log = logging.getLogger("wms.tasks")

DEFAULT_LEASE_SECONDS = 900

# Кому выдано задание. В схеме это `uuid` (пользователь из identity), а
# контракт описывает поле строкой до 128 символов, и прогон присылает
# «picker-1». Принимаем оба: настоящий идентификатор пользователя проходит как
# есть, а имя рабочего места разворачивается в постоянный uuid — один и тот же
# от смены к смене, чтобы «за кем задание» оставалось воспроизводимым.
# Расхождение схемы и контракта — находка 7 в FINDINGS.md.
ASSIGNEE_NAMESPACE = uuid.UUID("2f1c9a44-7b8e-4d5c-9a3f-1e6b0d8c5a72")


def assignee_id(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError):
        return uuid.uuid5(ASSIGNEE_NAMESPACE, value)


class TaskOperations:
    def __init__(self, pool: ConnectionPool, service: WmsService,
                 on_stock_changed: Callable[[uuid.UUID, set[uuid.UUID]], None] | None = None,
                 on_supply_release: Callable[[str, int], None] | None = None) -> None:
        self._pool = pool
        self._service = service
        self._on_stock_changed = on_stock_changed
        # Освобождение заказа из поставки — вызов в Wildberries, поэтому он
        # уходит после коммита, а не внутри (инвариант 2).
        self._on_supply_release = on_supply_release

    # ------------------------------------------------------------- выдача

    def pull(self, params: dict[str, Any]) -> dict[str, Any]:
        assignee = str(params.get("assignee") or "").strip()
        if not assignee:
            raise ValueError("assignee обязателен: задание выдаётся человеку, а не в воздух")
        limit = max(1, min(int(params.get("limit") or 10), 500))
        claim = params.get("claim", True)
        lease = max(30, min(int(params.get("lease_seconds") or DEFAULT_LEASE_SECONDS), 3600))
        states = params.get("states") or [TaskState.RESERVED.value]
        owners = params.get("owner_external_ids") or None
        extended = params.get("include_extended", True)

        with self._pool.connection() as connection:
            with transaction(connection) as cursor:
                with track_lock("tasks_pull"):
                    # Сначала вернуть в очередь то, что зависло за пропавшими
                    # сборщиками, — иначе задание не потеряно только на бумаге.
                    released = repo.release_expired_claims(cursor)
                    rows = repo.claim_tasks(
                        cursor, assignee=assignee_id(assignee), limit=limit, states=states,
                        owner_external_ids=owners, lease_seconds=lease, claim=bool(claim))
                    available = repo.available_for_pull(cursor, states=states,
                                                        owner_external_ids=owners)
                    tasks = [self._pull_task(cursor, row, extended) for row in rows]
        if released:
            log.info("возвращено в очередь заданий с истёкшим лизингом: %d", released)
        return {"tasks": tasks, "served_at": now(), "available_total": max(0, available)}

    def _pull_task(self, cursor: Any, row: dict[str, Any], extended: bool) -> dict[str, Any]:
        item: dict[str, Any] = {
            "task": _projection(row),
            "leased_until": _isoformat(row.get("claim_expires_at")),
        }
        if extended:
            # Сборщик должен видеть, куда идти, а не искать глазами: адрес,
            # коробка и порядок обхода приезжают вместе с заданием.
            item["placements"] = repo.placements_for_task(cursor, row["id"])
        return item

    def read(self, task_id: str) -> dict[str, Any]:
        with self._pool.connection() as connection:
            with single(connection) as cursor:
                row = repo.task_view(cursor, _uuid(task_id))
                if row is None:
                    raise ValueError(f"задание {task_id} не найдено")
                return _projection(row)

    # ---------------------------------------------------- скан и упаковка

    def scan(self, task_id: str, params: dict[str, Any]) -> dict[str, Any]:
        """Контрольный скан у стойки.

        Главный рубеж качества — именно он (раздел 4). Отклонённый скан
        обязан сохраниться: по нему видно, что именно человек взял не то.
        """
        barcode = str(params.get("barcode") or "").strip()
        if not barcode:
            raise ValueError("barcode обязателен")
        with self._pool.connection() as connection:
            with transaction(connection) as cursor:
                row = repo.task_view(cursor, _uuid(task_id), for_update=True)
                if row is None:
                    raise ValueError(f"задание {task_id} не найдено")

                if row["state"] in (TaskState.PICKED.value, TaskState.PACKED.value,
                                    TaskState.LABELED.value, TaskState.IN_SUPPLY.value):
                    # Ответ мог потеряться; повтор обязан вернуть то же самое.
                    return _scan_result(row, barcode, "ok", "picked", duplicate=True)

                if (row["barcode"] or "") != barcode:
                    # Чужой штрихкод — не ошибка системы, а пойманная ошибка
                    # сборщика. Она записывается, а не просто отвергается.
                    repo.record_scan(cursor, task_id=row["id"], owner_id=row["owner_id"],
                                     sku_id=row["sku_id"], result="wrong_barcode")
                    return _scan_result(row, barcode, "wrong_barcode", "rejected")

                repo.set_task_state(cursor, row["id"], TaskState.PICKED.value)
                repo.record_scan(cursor, task_id=row["id"], owner_id=row["owner_id"],
                                 sku_id=row["sku_id"], result="ok")
                self._service.emit_for_task(
                    cursor, task_id=row["id"], event_type="wms.item.scanned.v1",
                    payload={"task_id": str(row["id"]), "owner_id": str(row["owner_id"]),
                             "sku_id": str(row["sku_id"]), "barcode": barcode,
                             "qty": int(row["quantity"])},
                    correlation_id=f"scan-{row['id']}")
                row["state"] = TaskState.PICKED.value
                return _scan_result(row, barcode, "ok", "picked")

    def pack(self, task_id: str, params: dict[str, Any]) -> dict[str, Any]:
        """Упаковка с контрольным сканом.

        Несовпадение — отказ: это последнее место, где ошибка сборщика ловится
        до отгрузки.
        """
        control = _text(params.get("control_scan_barcode"))
        with self._pool.connection() as connection:
            with transaction(connection) as cursor:
                row = repo.task_view(cursor, _uuid(task_id), for_update=True)
                if row is None:
                    raise ValueError(f"задание {task_id} не найдено")
                if control is not None and control != (row["barcode"] or ""):
                    raise ValueError(
                        "контрольный скан не совпал со штрихкодом задания: "
                        "упаковано было бы не то")
                if row["state"] in (TaskState.PACKED.value, TaskState.LABELED.value,
                                    TaskState.IN_SUPPLY.value, TaskState.SHIPPED.value):
                    return _command_result(row, duplicate=True)

                repo.set_task_state(cursor, row["id"], TaskState.PACKED.value,
                                    package_ref=_text(params.get("box_barcode")))
                self._service.emit_for_task(
                    cursor, task_id=row["id"], event_type="wms.packing.completed.v1",
                    payload={"task_id": str(row["id"]), "owner_id": str(row["owner_id"]),
                             "qty": int(row["quantity"]),
                             "package_ref": _text(params.get("box_barcode"))},
                    correlation_id=str(params.get("idempotency_key") or f"pack-{row['id']}"))
                row["state"] = TaskState.PACKED.value
                return _command_result(row)

    # --------------------------------------------------- возврат и отмена

    def return_to_shelf(self, task_id: str, params: dict[str, Any]) -> dict[str, Any]:
        """Товар вернулся на полку. Причина обязательна.

        Возврат на полку без причины — ровно та тихая запись, из-за которой у
        всех 2645 отмен боевого контура причина пуста (раздел 3).
        """
        reason = _text(params.get("reason"))
        if not reason:
            raise ValueError("reason обязателен: возврат на полку без причины неразбираем")
        return self._unwind(task_id, reason=reason, state=TaskState.RESERVED.value,
                            release_reason=f"returned_to_shelf: {reason}",
                            event="wms.returned.to.shelf.v1",
                            idempotency=str(params.get("idempotency_key") or ""))

    def cancel(self, task_id: str, params: dict[str, Any]) -> dict[str, Any]:
        """Отмена задания, в том числе после выдачи стикера (раздел 6.6).

        Резерв снимается, товар возвращается в good, локальный стикер
        помечается недействительным, заказ освобождается из поставки. Причина
        отмены обязательна на уровне схемы (инвариант 11).
        """
        event_id = _text(params.get("cancellation_event_id"))
        if not event_id:
            raise ValueError("cancellation_event_id обязателен: это ключ идемпотентности")
        handed_over = bool(params.get("handed_over"))
        reason = ("отменено после передачи в доставку — разбор возврата"
                  if handed_over else f"отменено по событию {event_id}")
        return self._unwind(task_id, reason=reason, state=TaskState.CANCELLED.value,
                            release_reason=f"cancelled: {event_id}",
                            event="wms.order.cancelled.v1", idempotency=event_id,
                            invalidate_label=True, release_supply=True)

    def _unwind(self, task_id: str, *, reason: str, state: str, release_reason: str,
                event: str, idempotency: str, invalidate_label: bool = False,
                release_supply: bool = False) -> dict[str, Any]:
        """Снимает резерв и возвращает товар туда, откуда он был взят."""
        supply_release: tuple[str, int] | None = None
        owner_id: uuid.UUID | None = None
        sku_ids: set[uuid.UUID] = set()

        with self._pool.connection() as connection:
            with transaction(connection) as cursor:
                row = repo.task_view(cursor, _uuid(task_id), for_update=True)
                if row is None:
                    raise ValueError(f"задание {task_id} не найдено")
                if row["state"] == state and state == TaskState.CANCELLED.value:
                    return _cancel_result(row, duplicate=True)

                owner_id = row["owner_id"]
                reservation = repo.held_reservation(cursor, row["id"], for_update=True)
                if reservation is not None:
                    # Товар возвращается ровно туда, откуда был взят: движения
                    # резерва хранят точную раскладку по ячейкам и коробкам.
                    for index, move in enumerate(repo.reservation_moves(
                            cursor, reservation["id"])):
                        repo.insert_move(
                            cursor, owner_id=row["owner_id"], sku_id=reservation["sku_id"],
                            qty=int(move["qty"]),
                            cell_from=move["cell_to"], cell_to=move["cell_to"],
                            box_from=move["box_to"], box_to=move["box_to"],
                            state_from="reserved", state_to="good",
                            reason=release_reason.split(":")[0],
                            doc_type="reservation", doc_ref=str(reservation["id"]),
                            idem_key=f"release:{reservation['id']}:{idempotency}:{index}")
                    repo.release_reservation(cursor, reservation["id"], release_reason)
                    sku_ids.add(reservation["sku_id"])

                if invalidate_label:
                    repo.invalidate_label(cursor, row["id"])
                if release_supply and row["supply_id"]:
                    supply_release = repo.supply_reference(cursor, row["supply_id"],
                                                           int(row["wb_order_id"]))
                repo.set_task_state(
                    cursor, row["id"], state,
                    cancel_reason=reason if state == TaskState.CANCELLED.value else None,
                    clear_supply=release_supply, clear_assignee=True,
                    clear_reservation=state == TaskState.CANCELLED.value)
                self._service.emit_for_task(
                    cursor, task_id=row["id"], event_type=event,
                    payload={"task_id": str(row["id"]), "owner_id": str(row["owner_id"]),
                             "wb_order_id": int(row["wb_order_id"]), "reason": reason},
                    correlation_id=idempotency or f"unwind-{row['id']}")
                row["state"], row["cancel_reason"] = state, reason
                result = (_cancel_result(row) if state == TaskState.CANCELLED.value
                          else _command_result(row))

        # Оба вызова наружу — после коммита (инвариант 2).
        if supply_release and self._on_supply_release is not None:
            try:
                self._on_supply_release(*supply_release)
            except Exception:
                log.warning("заказ не освобождён из поставки в WB", exc_info=False)
        if owner_id is not None and sku_ids and self._on_stock_changed is not None:
            try:
                self._on_stock_changed(owner_id, sku_ids)
            except Exception:
                log.warning("остаток вернулся на полку, но не опубликован")
        return result


# ------------------------------------------------------------- проекции

def _projection(row: dict[str, Any]) -> dict[str, Any]:
    """Проекция задания по схеме TaskProjection. additionalProperties: false."""
    label = None
    if row.get("label_format") is not None:
        label = {"ready": row.get("label_invalidated_at") is None,
                 "format": row["label_format"],
                 "version": int(row.get("label_version") or 1),
                 "checksum": row.get("label_checksum"),
                 "fetched_at": _isoformat(row.get("label_fetched_at"))}
    return {
        "task_id": str(row["id"]),
        "wb_order_id": int(row["wb_order_id"]),
        "wb_order_uid": row.get("wb_order_uid"),
        "wb_account_external_id": row.get("account_external_id"),
        "owner_external_id": row.get("seller_external_id"),
        "barcode": row.get("barcode"),
        "seller_sku": row.get("seller_sku"),
        "name": row.get("sku_name"),
        "quantity": int(row["quantity"]),
        "deadline": _isoformat(row.get("deadline")),
        "state": row["state"],
        "wb_status": row.get("wb_status"),
        "reservation_id": _str_or_none(row.get("reservation_id")),
        "label": label,
        "package_ref": row.get("package_ref"),
        "supply_id": _str_or_none(row.get("supply_id")),
        "assignee": _str_or_none(row.get("assignee")),
        "claim_expires_at": _isoformat(row.get("claim_expires_at")),
        "cancel_reason": row.get("cancel_reason"),
        "manual_review_code": row.get("manual_review_code"),
        "manual_review_reason": row.get("manual_review_reason"),
        "last_reconciled_at": _isoformat(row.get("last_reconciled_at")),
        "created_at": _isoformat(row.get("created_at")),
        "updated_at": _isoformat(row.get("updated_at")),
        "version": int(row.get("version") or 1),
    }


def _scan_result(row: dict[str, Any], barcode: str, scan_result: str, status: str,
                 *, duplicate: bool = False) -> dict[str, Any]:
    return {"status": status, "owner_external_id": row.get("seller_external_id"),
            "task_id": str(row["id"]), "barcode": barcode, "state": row["state"],
            "cell_address": row.get("cell_address"), "box_barcode": row.get("box_barcode"),
            "scanned_at": now(), "scan_result": scan_result, "duplicate": duplicate}


def _command_result(row: dict[str, Any], *, duplicate: bool = False) -> dict[str, Any]:
    return {"task_id": str(row["id"]), "owner_external_id": row.get("seller_external_id"),
            "state": row["state"], "version": int(row.get("version") or 1),
            "duplicate": duplicate}


def _cancel_result(row: dict[str, Any], *, duplicate: bool = False) -> dict[str, Any]:
    return {"task_id": str(row["id"]), "owner_external_id": row.get("seller_external_id"),
            "state": row["state"], "version": int(row.get("version") or 1),
            "duplicate": duplicate, "cancel_reason": row.get("cancel_reason") or ""}


def _uuid(value: Any) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError):
        raise ValueError(f"{value!r} не похоже на идентификатор задания") from None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _str_or_none(value: Any) -> str | None:
    return str(value) if value is not None else None


def _isoformat(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)
