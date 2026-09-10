"""Ядро сервиса wms: транзакция раздела 6.2.

    BEGIN
      INSERT wms_task        ON CONFLICT (wb_order_id) DO NOTHING
      SELECT stock_balance ... FOR UPDATE
      INSERT reservation
      INSERT stock_move      (good → reserved)
      INSERT outbox          (wms.reservation.succeeded.v1)
    COMMIT

Всё, что здесь происходит, происходит одним коммитом Postgres. Расхождение
«заказ есть, резерва нет» — то самое, из-за которого в боевом контуре 2467
заданий разошлись с Wildberries, — становится невозможным по построению
(инвариант 1).

Ни одного HTTP-вызова внутри (инвариант 2): вызов в Wildberries занимает около
500 мс, транзакция — 2–5 мс. Поставка и стикер уезжают ПОСЛЕ коммита, отдельным
шагом; здесь их нет и быть не может.
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from typing import Any

import psycopg

from . import repositories as repo
from .domain import ErrorCode, EventEnvelope, TaskState
from .events import assert_no_secrets
from .metrics import STOCK_SHORTFALL, track_lock
from .postgres import ConnectionPool, is_retryable, transaction

TENANT_ID = os.getenv("MMX_TENANT_ID", "mm-express")

# Конкуренция за строку остатка — рабочая ситуация, а не сбой: десять
# писателей на один owner × sku (раздел 4). Два повтора закрывают её
# незаметно для клиента; дальше честнее вернуть SERIALIZATION_RETRY, чем
# держать соединение и делать вид, что склад отвечает.
RESERVE_ATTEMPTS = 3


@dataclass
class Emitted:
    """Событие, записанное в outbox в той же транзакции.

    Возвращается наружу, чтобы публикатор мог отправить его сразу, не дожидаясь
    своего цикла: событие — уведомление (раздел 6.1), но уведомлять лучше
    быстро. В базе оно уже лежит, поэтому потеря отправки ничего не теряет.
    """

    envelope: EventEnvelope
    sequence: int


@dataclass
class ReservationOutcome:
    status: str
    task_id: str | None = None
    owner_external_id: str | None = None
    error_code: str | None = None
    reservation_id: str | None = None
    ledger_short: bool = False
    events: list[Emitted] = field(default_factory=list)
    # Повтор той же команды. Наружу в контракт не идёт — нужен метрикам и логу,
    # чтобы отличать «сделали» от «уже было сделано» (инвариант 5).
    duplicate: bool = False

    def as_result(self) -> dict[str, Any]:
        """Ответ по схеме ReservationResult. additionalProperties: false."""
        return {
            "status": self.status,
            "task_id": self.task_id,
            "owner_external_id": self.owner_external_id,
            "error_code": self.error_code,
            "reservation_id": self.reservation_id,
            "ledger_short": self.ledger_short,
        }


class WmsService:
    """Операции склада поверх одного пула соединений."""

    def __init__(self, pool: ConnectionPool, *, tenant_id: str = TENANT_ID) -> None:
        self._pool = pool
        self._tenant_id = tenant_id

    # ------------------------------------------------------------- события

    def _emit(self, cursor: psycopg.Cursor, *, event_type: str, payload: dict[str, Any],
              correlation_id: str, aggregate_id: uuid.UUID,
              carry_sequence: bool = True) -> Emitted:
        """Кладёт событие в outbox под номером агрегата.

        Номер берётся и пишется в той же транзакции, что и движение товара
        (приложение E): потребители полагаются на монотонность sequence в
        пределах задания, а «почти монотонно» ломает их одинаково молча.

        `carry_sequence=False` — для payload, где схема запрещает лишние поля
        (`wms.stock.shortfall.v1` заморожен ровно на пяти полях). Номер всё
        равно выделяется и хранится в самой строке outbox: порядок событий
        задания важнее того, видно ли номер внутри payload.
        """
        sequence = repo.next_sequence(cursor, aggregate_id)
        body = dict(payload)
        if carry_sequence:
            body["sequence"] = sequence
        # Токен WB не имеет права попасть в событие никогда (инвариант 15).
        # Проверка стоит на записи, а не на отправке: в outbox событие уже
        # долговечно, и утечь оно успеет раньше публикации.
        assert_no_secrets(body)
        envelope = EventEnvelope(
            type=event_type, tenant_id=self._tenant_id, payload=body,
            correlation_id=correlation_id)
        repo.insert_outbox(
            cursor, event_id=uuid.UUID(envelope.event_id), event_type=event_type,
            tenant_id=self._tenant_id, payload=body, correlation_id=correlation_id,
            aggregate_id=aggregate_id, sequence=sequence, occurred_at=envelope.occurred_at)
        return Emitted(envelope=envelope, sequence=sequence)

    # -------------------------------------------------------------- резерв

    def reserve(self, params: dict[str, Any]) -> ReservationOutcome:
        """Транзакция раздела 6.2 с повтором при конкуренции за остаток."""
        last_error: psycopg.Error | None = None
        for attempt in range(RESERVE_ATTEMPTS):
            try:
                with self._pool.connection() as connection:
                    with transaction(connection) as cursor:
                        with track_lock("reserve"):
                            return self._reserve_once(cursor, params)
            except psycopg.Error as error:
                if not is_retryable(error):
                    raise
                last_error = error
        # Попытки исчерпаны. SERIALIZATION_RETRY — не ошибка данных: клиент
        # обязан повторить с тем же idempotency_key, и второго движения это не
        # создаст, ключи идемпотентности на месте (приложение C, инвариант 5).
        assert last_error is not None
        return ReservationOutcome(
            status="rejected", error_code=ErrorCode.SERIALIZATION_RETRY.value,
            owner_external_id=_text(params.get("seller_external_id")))

    def _reserve_once(self, cursor: psycopg.Cursor, params: dict[str, Any]) -> ReservationOutcome:
        seller = str(params.get("seller_external_id") or "")
        barcode = _text(params.get("barcode"))
        sku_field = _text(params.get("sku"))
        quantity = int(params.get("quantity") or 1)
        correlation_id = str(params.get("correlation_id") or uuid.uuid4())
        wb_order_id = _order_id(params.get("wb_order_id"))
        order_aggregate = repo.aggregate_for_order(wb_order_id)

        if quantity <= 0:
            return self._reject(cursor, ErrorCode.INSUFFICIENT_STOCK, wb_order_id=wb_order_id,
                                barcode=barcode or sku_field, quantity=quantity,
                                correlation_id=correlation_id, aggregate=order_aggregate,
                                seller=seller)

        owner = repo.find_owner(cursor, seller) if seller else None
        account_hint = repo.find_account(
            cursor, external_id=_text(params.get("wb_account_external_id")))
        if not seller and account_hint is not None:
            # Продавца в запросе не назвали, но назвали кабинет. Владельца
            # определяет кабинет: задание пришло именно из него, и гадать тут
            # не о чем. Так зовёт резерв внутренний опросчик WB.
            #
            # Подставлять кабинет вместо НАЗВАННОГО, но неизвестного продавца
            # нельзя ни при каких условиях: это отгрузка чужой вещи по чужому
            # заказу (инвариант 6). Названный и не найденный — всегда отказ.
            owner = repo.owner_by_id(cursor, account_hint["owner_id"])
            seller = owner["seller_external_id"] if owner else seller
        if owner is None or not owner["active"]:
            # Владельца нет — задания тоже не будет: owner_id в схеме NOT NULL,
            # и приписать чужой товар первому попавшемуся клиенту нельзя
            # (инвариант 6).
            return self._reject(cursor, ErrorCode.SELLER_MAPPING_MISSING,
                                wb_order_id=wb_order_id, barcode=barcode or sku_field,
                                quantity=quantity, correlation_id=correlation_id,
                                aggregate=order_aggregate, seller=seller)

        account = account_hint or repo.sole_account_of_owner(cursor, owner["id"])
        if account is None or account["owner_id"] != owner["id"]:
            # Кабинет чужой или неизвестен. Это тоже разрыв связки
            # «продавец ↔ кабинет», поэтому код тот же: другого в приложении C нет.
            return self._reject(cursor, ErrorCode.SELLER_MAPPING_MISSING,
                                wb_order_id=wb_order_id, barcode=barcode or sku_field,
                                quantity=quantity, correlation_id=correlation_id,
                                aggregate=order_aggregate, seller=seller,
                                owner_id=owner["id"])

        # Повтор команды обязан дать тот же ответ, а не второе движение.
        existing = repo.task_by_order(cursor, wb_order_id, for_update=True)
        if existing is not None:
            return self._outcome_for_existing(cursor, existing, seller)

        sku, mapping_error = repo.find_sku(
            cursor, owner["id"], barcode=barcode, sku_field=sku_field)
        if sku is None:
            return self._manual_review(
                cursor, code=mapping_error or ErrorCode.PRODUCT_MAPPING_MISSING,
                owner=owner, account=account, wb_order_id=wb_order_id,
                wb_order_uid=_text(params.get("order_uid")),
                barcode=barcode or sku_field, quantity=quantity,
                deadline=params.get("deadline"), correlation_id=correlation_id,
                seller=seller)

        task_id = uuid.uuid4()
        task = repo.insert_task(
            cursor, task_id=task_id, wb_order_id=wb_order_id,
            wb_order_uid=_text(params.get("order_uid")), account_id=account["id"],
            owner_id=owner["id"], sku_id=sku["id"], barcode=sku["barcode"],
            quantity=quantity, deadline=params.get("deadline"), state=TaskState.NEW.value)
        if task is None:
            # Другая транзакция успела завести это же задание между нашими
            # чтением и вставкой. Отвечаем по её результату, а не своим.
            existing = repo.task_by_order(cursor, wb_order_id, for_update=True)
            if existing is not None:
                return self._outcome_for_existing(cursor, existing, seller)
            raise RuntimeError(f"задание по заказу {wb_order_id} исчезло между вставкой и чтением")

        return self._hold_stock(cursor, task=task, owner=owner, sku=sku, quantity=quantity,
                                correlation_id=correlation_id, seller=seller)

    # ------------------------------------------------------- удержание товара

    def _hold_stock(self, cursor: psycopg.Cursor, *, task: dict[str, Any],
                    owner: dict[str, Any], sku: dict[str, Any], quantity: int,
                    correlation_id: str, seller: str) -> ReservationOutcome:
        """Резерв под задание: `SELECT ... FOR UPDATE` по (owner, sku, cell, box)."""
        placements = repo.lock_good_placements(cursor, owner["id"], sku["id"])

        allocations: list[tuple[Any, Any, int]] = []   # (cell_id, box_id, qty)
        remaining = quantity
        for placement in placements:
            if remaining <= 0:
                break
            take = min(int(placement["qty"]), remaining)
            allocations.append((placement["cell_id"], placement["box_id"], take))
            remaining -= take

        if remaining > 0 and not owner["allow_ledger_short"]:
            # Клапан выключен по этому владельцу — задание остаётся видимым в
            # состоянии `short`, а не исчезает в тихой отмене (раздел 3).
            cursor.execute(
                "UPDATE wms_task SET state = %s, version = version + 1 WHERE id = %s",
                (TaskState.SHORT.value, task["id"]))
            emitted = self._emit(
                cursor, event_type="wms.reservation.failed.v1",
                payload={"task_id": str(task["id"]), "owner_id": str(owner["id"]),
                         "seller_external_id": seller, "wb_order_id": int(task["wb_order_id"]),
                         "barcode": sku["barcode"], "quantity": quantity,
                         "error_code": ErrorCode.INSUFFICIENT_STOCK.value},
                correlation_id=correlation_id, aggregate_id=task["id"])
            return ReservationOutcome(
                status="rejected", task_id=str(task["id"]), owner_external_id=seller,
                error_code=ErrorCode.INSUFFICIENT_STOCK.value, events=[emitted])

        reservation_id = uuid.uuid4()
        short_cell_id = None
        if remaining > 0:
            # Клапан «собрать без остатка» (раздел 6.5). Товар на полке есть,
            # по учёту его нет — смена не встаёт, но след остаётся: движение с
            # reason='ledger_short', расхождение, событие и счётчик.
            short_cell = repo.ledger_short_cell(cursor, owner["id"], sku["id"])
            short_cell_id = short_cell["id"]
            allocations.append((short_cell["id"], short_cell.get("box_id"), remaining))

        for index, (cell_id, box_id, take) in enumerate(allocations):
            ledger_short = short_cell_id is not None and index == len(allocations) - 1
            repo.insert_move(
                cursor, owner_id=owner["id"], sku_id=sku["id"], qty=take,
                cell_from=cell_id, cell_to=cell_id, box_from=box_id, box_to=box_id,
                state_from="good", state_to="reserved",
                reason="ledger_short" if ledger_short else "reservation",
                doc_type="reservation", doc_ref=str(reservation_id),
                idem_key=f"reserve:{reservation_id}:{index}")

        primary_cell, primary_box, _ = max(allocations, key=lambda item: item[2])
        repo.insert_reservation(
            cursor, reservation_id=reservation_id, task_id=task["id"], owner_id=owner["id"],
            sku_id=sku["id"], qty=quantity, cell_id=primary_cell, box_id=primary_box)
        repo.attach_reservation(cursor, task["id"], reservation_id, TaskState.RESERVED.value)

        events: list[Emitted] = []
        if remaining > 0:
            repo.insert_discrepancy(
                cursor, owner_id=owner["id"], sku_id=sku["id"], kind="ledger_short",
                qty=remaining, task_id=task["id"], cell_id=short_cell_id,
                comment="резерв поверх недостающего учётного остатка (раздел 6.5)")
            # Состав payload заморожен потоком 0 ровно на пяти полях —
            # additionalProperties: false, номер события внутрь не кладём.
            events.append(self._emit(
                cursor, event_type="wms.stock.shortfall.v1",
                payload={"owner_id": str(owner["id"]), "sku_id": str(sku["id"]),
                         "cell_id": str(short_cell_id), "qty_short": remaining,
                         "task_id": str(task["id"])},
                correlation_id=correlation_id, aggregate_id=task["id"],
                carry_sequence=False))
            STOCK_SHORTFALL.inc()

        events.append(self._emit(
            cursor, event_type="wms.reservation.succeeded.v1",
            payload={"task_id": str(task["id"]), "reservation_id": str(reservation_id),
                     "owner_id": str(owner["id"]), "seller_external_id": seller,
                     "sku_id": str(sku["id"]), "barcode": sku["barcode"],
                     "cell_id": str(primary_cell) if primary_cell else None,
                     "box_id": str(primary_box) if primary_box else None,
                     "qty": quantity, "wb_order_id": int(task["wb_order_id"]),
                     "deadline": _isoformat(task["deadline"]),
                     "ledger_short": remaining > 0},
            correlation_id=correlation_id, aggregate_id=task["id"]))

        return ReservationOutcome(
            status="reserved", task_id=str(task["id"]), owner_external_id=seller,
            reservation_id=str(reservation_id), ledger_short=remaining > 0, events=events)

    # ---------------------------------------------------------- отказы

    def _manual_review(self, cursor: psycopg.Cursor, *, code: ErrorCode,
                       owner: dict[str, Any], account: dict[str, Any], wb_order_id: int,
                       wb_order_uid: str | None, barcode: str | None, quantity: int,
                       deadline: Any, correlation_id: str, seller: str) -> ReservationOutcome:
        """Немаппленный товар становится заданием в manual_review, а не отказом.

        В боевом контуре PRODUCT_MAPPING_MISSING уходил в отказ и дальше в
        отмену без причины — часть тех 2645 (раздел 3.2). Задание обязано
        попасть человеку на глаза с кодом; резерв при этом не создаётся, и
        товар без маппинга не превращается в остаток (инвариант 6).
        """
        reason = {
            ErrorCode.PRODUCT_MAPPING_MISSING:
                f"товар {barcode!r} не найден у владельца {seller}",
            ErrorCode.AMBIGUOUS_PRODUCT_MAPPING:
                f"по {barcode!r} у владельца {seller} больше одного товара: "
                f"штрихкод определяет вещь, артикул — только модель",
        }.get(code, code.value)

        task = repo.insert_task(
            cursor, task_id=uuid.uuid4(), wb_order_id=wb_order_id, wb_order_uid=wb_order_uid,
            account_id=account["id"], owner_id=owner["id"], sku_id=None, barcode=barcode,
            quantity=quantity, deadline=deadline, state=TaskState.MANUAL_REVIEW.value,
            manual_review_code=code.value, manual_review_reason=reason)
        if task is None:
            existing = repo.task_by_order(cursor, wb_order_id, for_update=True)
            if existing is not None:
                return self._outcome_for_existing(cursor, existing, seller)
            raise RuntimeError(f"задание по заказу {wb_order_id} исчезло между вставкой и чтением")

        emitted = self._emit(
            cursor, event_type="wms.reservation.failed.v1",
            payload={"task_id": str(task["id"]), "owner_id": str(owner["id"]),
                     "seller_external_id": seller, "wb_order_id": int(wb_order_id),
                     "barcode": barcode, "quantity": quantity,
                     "error_code": code.value, "manual_review_code": code.value,
                     "manual_review_reason": reason},
            correlation_id=correlation_id, aggregate_id=task["id"])
        return ReservationOutcome(
            status="rejected", task_id=str(task["id"]), owner_external_id=seller,
            error_code=code.value, events=[emitted])

    def _reject(self, cursor: psycopg.Cursor, code: ErrorCode, *, wb_order_id: int,
                barcode: str | None, quantity: int, correlation_id: str,
                aggregate: uuid.UUID, seller: str,
                owner_id: uuid.UUID | None = None) -> ReservationOutcome:
        """Отказ без задания — заводить его не на кого или не на что.

        Номер события ведётся по заказу WB: у отказа тоже должен быть свой
        порядок, иначе повторный опрос неотличим от нового отказа.
        """
        payload: dict[str, Any] = {
            "task_id": None, "owner_id": str(owner_id) if owner_id else None,
            "wb_order_id": int(wb_order_id), "barcode": barcode,
            "quantity": max(quantity, 1), "error_code": code.value}
        if seller:
            payload["seller_external_id"] = seller
        emitted = self._emit(
            cursor, event_type="wms.reservation.failed.v1", payload=payload,
            correlation_id=correlation_id, aggregate_id=aggregate)
        return ReservationOutcome(
            status="rejected", error_code=code.value,
            owner_external_id=seller or None, events=[emitted])

    def _outcome_for_existing(self, cursor: psycopg.Cursor, task: dict[str, Any],
                              seller: str) -> ReservationOutcome:
        """Ответ на повтор: тот же, что был, и ни одного нового движения."""
        reservation = repo.held_reservation(cursor, task["id"])
        if reservation is not None:
            return ReservationOutcome(
                status="reserved", task_id=str(task["id"]), owner_external_id=seller,
                reservation_id=str(reservation["id"]), duplicate=True,
                ledger_short=_was_ledger_short(cursor, reservation["id"]))
        return ReservationOutcome(
            status="rejected", task_id=str(task["id"]), owner_external_id=seller,
            error_code=task["manual_review_code"], duplicate=True)


def _was_ledger_short(cursor: psycopg.Cursor, reservation_id: uuid.UUID) -> bool:
    cursor.execute(
        "SELECT 1 FROM stock_move WHERE doc_type = 'reservation' AND doc_ref = %s "
        "   AND reason = 'ledger_short' LIMIT 1", (str(reservation_id),))
    return cursor.fetchone() is not None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _order_id(value: Any) -> int:
    """Номер заказа WB. В контракте он и число, и строка — в схеме bigint."""
    if value is None:
        raise ValueError("wb_order_id обязателен: это ключ идемпотентности задания")
    return int(str(value).strip())


def _isoformat(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


# ===========================================================================
# Справочники и приход товара.
#
# Всё, без чего транзакцию 6.2 нечем позвать: владелец, товар, кабинет WB и
# начальный остаток, который при переключении клиента даёт владелец компании
# (решение владельца 11).
# ===========================================================================

class CatalogOperations:
    """Справочники поверх того же пула. Отдельный класс — отдельная ответственность."""

    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    def upsert_owner(self, params: dict[str, Any]) -> dict[str, Any]:
        seller = str(params.get("seller_external_id") or "").strip()
        if not seller:
            raise ValueError("seller_external_id обязателен")
        with self._pool.connection() as connection:
            with transaction(connection) as cursor:
                owner, created = repo.upsert_owner(
                    cursor, seller, name=_text(params.get("name")),
                    inn=_text(params.get("inn")), active=params.get("active"),
                    allow_ledger_short=params.get("allow_ledger_short"))
                return _owner_view(owner) | {"created": created}

    def owners(self) -> list[dict[str, Any]]:
        with self._pool.connection() as connection:
            with transaction(connection) as cursor:
                return [_owner_view(row) for row in repo.list_owners(cursor)]

    def products(self, seller: str | None) -> list[dict[str, Any]]:
        with self._pool.connection() as connection:
            with transaction(connection) as cursor:
                if seller:
                    cursor.execute(
                        "SELECT s.id, s.barcode, s.seller_sku, s.name, s.buffer, "
                        "       o.seller_external_id "
                        "  FROM sku s JOIN owner o ON o.id = s.owner_id "
                        " WHERE o.seller_external_id = %s ORDER BY s.barcode LIMIT 1000",
                        (seller,))
                else:
                    cursor.execute(
                        "SELECT s.id, s.barcode, s.seller_sku, s.name, s.buffer, "
                        "       o.seller_external_id "
                        "  FROM sku s JOIN owner o ON o.id = s.owner_id "
                        " ORDER BY s.barcode LIMIT 1000")
                return [_product_view(row) for row in cursor.fetchall()]

    def ensure_product(self, params: dict[str, Any]) -> dict[str, Any]:
        """Заводит товар у владельца.

        Владелец создаётся заодно: одинаковый штрихкод у двух клиентов — это
        две разные вещи на полке, поэтому товар не существует «вообще», только
        у владельца (инвариант 6).
        """
        seller = str(params.get("seller_external_id") or "").strip()
        barcode = str(params.get("barcode") or "").strip()
        if not seller or not barcode:
            raise ValueError("seller_external_id и barcode обязательны")
        with self._pool.connection() as connection:
            with transaction(connection) as cursor:
                owner, _ = repo.upsert_owner(cursor, seller)
                sku, created = repo.upsert_sku(
                    cursor, owner["id"], barcode,
                    seller_sku=_text(params.get("seller_sku")),
                    name=_text(params.get("name")), buffer=params.get("buffer"))
                return {"product_id": str(sku["id"]), "sku_id": str(sku["id"]),
                        "barcode": barcode, "seller_external_id": seller,
                        "created": created}

    def upsert_wb_account(self, params: dict[str, Any]) -> dict[str, Any]:
        """Кабинет Wildberries. В базу едет ссылка на секрет, не токен.

        Значение токена сюда не приходит никогда (инвариант 15); схема отбивает
        случайную запись живого JWT отдельным CHECK, но проверять форму до
        вставки честнее, чем ловить нарушение ограничением базы.
        """
        external_id = str(params.get("external_id") or "").strip()
        seller = str(params.get("seller_external_id") or "").strip()
        secret_ref = str(params.get("secret_ref") or "").strip()
        if not external_id or not seller or not secret_ref:
            raise ValueError("external_id, seller_external_id и secret_ref обязательны")
        if secret_ref.startswith("eyJ"):
            raise ValueError("secret_ref — ссылка на секрет, а не значение токена")

        with self._pool.connection() as connection:
            with transaction(connection) as cursor:
                owner, _ = repo.upsert_owner(cursor, seller)
                cursor.execute(
                    "INSERT INTO wb_account (id, owner_id, external_id, display_name, "
                    "                        secret_ref, scopes, mode, status, wb_warehouse_id) "
                    "VALUES (%(id)s, %(owner)s, %(external)s, %(name)s, %(secret)s, "
                    "        COALESCE(%(scopes)s, '[]'::jsonb), COALESCE(%(mode)s, 'shadow'), "
                    "        COALESCE(%(status)s, 'DISABLED'), %(warehouse)s) "
                    "ON CONFLICT (external_id) DO UPDATE SET "
                    "    display_name = EXCLUDED.display_name, "
                    "    secret_ref = EXCLUDED.secret_ref, "
                    "    scopes = COALESCE(%(scopes)s, wb_account.scopes), "
                    "    mode = COALESCE(%(mode)s, wb_account.mode), "
                    "    status = COALESCE(%(status)s, wb_account.status), "
                    "    wb_warehouse_id = COALESCE(%(warehouse)s, wb_account.wb_warehouse_id) "
                    "RETURNING id, external_id, owner_id, display_name, secret_ref, scopes, "
                    "          mode, status, token_type, token_expires_at, last_verified_at, "
                    "          last_sync_at, next_sync_at, wb_warehouse_id, (xmax = 0) AS created",
                    {"id": uuid.uuid4(), "owner": owner["id"], "external": external_id,
                     "name": str(params.get("display_name") or external_id),
                     "secret": secret_ref,
                     "scopes": _json_or_none(params.get("scopes")),
                     "mode": _text(params.get("mode")),
                     "status": _text(params.get("status")),
                     "warehouse": params.get("wb_warehouse_id")})
                row = cursor.fetchone()
                assert row is not None
                created = bool(row.pop("created"))
                return _account_view(row, seller) | {"created": created}

    def accounts(self, seller: str | None = None) -> list[dict[str, Any]]:
        with self._pool.connection() as connection:
            with transaction(connection) as cursor:
                cursor.execute(
                    "SELECT a.id, a.external_id, a.owner_id, a.display_name, a.secret_ref, "
                    "       a.scopes, a.mode, a.status, a.token_type, a.token_expires_at, "
                    "       a.last_verified_at, a.last_sync_at, a.next_sync_at, "
                    "       a.wb_warehouse_id, o.seller_external_id "
                    "  FROM wb_account a JOIN owner o ON o.id = a.owner_id "
                    " WHERE (%s::text IS NULL OR o.seller_external_id = %s) "
                    " ORDER BY a.external_id LIMIT 500", (seller, seller))
                return [_account_view(row, row["seller_external_id"])
                        for row in cursor.fetchall()]


class StockOperations:
    """Приход, размещение и публикуемый остаток."""

    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    def apply_document(self, params: dict[str, Any]) -> dict[str, Any]:
        """Складской документ: строки едут прямо в журнал (инвариант 3).

        Отдельной таблицы документа в схеме нет, поэтому идемпотентность держит
        `stock_move.idem_key`: повторный документ с тем же `reference` не
        добавит ни единицы. Начальный остаток при переключении клиента приезжает
        именно так и оставляет след с `doc_type='opening'` (решение владельца 11).
        """
        seller = str(params.get("seller_external_id") or "").strip()
        reference = str(params.get("reference") or "").strip()
        doc_type = str(params.get("doc_type") or "adjustment").strip()
        warehouse_code = str(params.get("warehouse_code") or "RUM").strip() or "RUM"
        lines = params.get("lines") or []
        if not seller or not reference or not lines:
            raise ValueError("seller_external_id, reference и lines обязательны")

        with self._pool.connection() as connection:
            with transaction(connection) as cursor:
                owner, _ = repo.upsert_owner(cursor, seller)
                written = 0
                for index, line in enumerate(lines):
                    barcode = str(line.get("barcode") or "").strip()
                    quantity = int(line.get("quantity") or 0)
                    if not barcode or quantity <= 0:
                        continue
                    sku, _ = repo.upsert_sku(
                        cursor, owner["id"], barcode,
                        seller_sku=_text(line.get("seller_sku")), name=_text(line.get("name")))
                    address = _text(line.get("cell_address")) or f"{warehouse_code}-INBOUND"
                    cell = repo.ensure_cell(cursor, address, warehouse_code=warehouse_code)
                    box = None
                    if _text(line.get("box_barcode")):
                        box = repo.ensure_box(
                            cursor, str(line["box_barcode"]).strip(), owner_id=owner["id"],
                            sku_id=sku["id"], cell_id=cell["id"],
                            comment=_text(line.get("comment"))
                            or f"{doc_type} {reference}: {barcode}")
                    move = repo.insert_move(
                        cursor, owner_id=owner["id"], sku_id=sku["id"], qty=quantity,
                        cell_to=cell["id"], box_to=box["id"] if box else None,
                        state_to=str(line.get("state") or "good"),
                        reason=doc_type, doc_type=doc_type, doc_ref=reference,
                        idem_key=f"{doc_type}:{reference}:{index}")
                    if move is not None:
                        written += 1
                return {"reference": reference, "owner_external_id": seller,
                        "state": "applied", "moves": written,
                        "duplicate": written == 0}

    def placements(self, params: dict[str, Any]) -> dict[str, Any]:
        """Где и в каком состоянии лежит товар — проекция `stock_balance`."""
        seller = str(params.get("seller_external_id") or "").strip()
        barcodes = params.get("barcodes") or None
        states = params.get("states") or None
        with self._pool.connection() as connection:
            with transaction(connection) as cursor:
                cursor.execute(
                    "SELECT s.barcode, c.address AS cell_address, bx.barcode AS box_barcode, "
                    "       b.state, b.qty, c.route_order "
                    "  FROM stock_balance b "
                    "  JOIN owner o ON o.id = b.owner_id "
                    "  JOIN sku s ON s.id = b.sku_id "
                    "  JOIN cell c ON c.id = b.cell_id "
                    "  LEFT JOIN box bx ON bx.id = b.box_id "
                    " WHERE o.seller_external_id = %(seller)s AND b.qty <> 0 "
                    "   AND (%(barcodes)s::text[] IS NULL OR s.barcode = ANY(%(barcodes)s)) "
                    "   AND (%(states)s::text[] IS NULL OR b.state = ANY(%(states)s)) "
                    " ORDER BY c.route_order NULLS LAST, c.address, s.barcode",
                    {"seller": seller, "barcodes": list(barcodes) if barcodes else None,
                     "states": list(states) if states else None})
                rows = [{"barcode": row["barcode"], "cell_address": row["cell_address"],
                         "box_barcode": row["box_barcode"], "state": row["state"],
                         "quantity": max(0, int(row["qty"])),
                         "route_order": row["route_order"]}
                        for row in cursor.fetchall()]
        return {"owner_external_id": seller, "rows": rows}

    def available_rows(self, seller: str) -> tuple[list[dict[str, Any]], int]:
        """Строки для публикации в WB: `available = good − reserved − buffer`.

        Формула взята из инварианта 7 буквально, и это сознательное решение,
        а не недосмотр. Резерв уже переводит товар движением `good → reserved`
        (раздел 6.2), поэтому `good` в проекции его не содержит, и вычитание
        `reserved` — второе по счёту: публикуется меньше, чем физически
        свободно, ровно на величину резерва.

        Оставлено так по двум причинам. Первая: обе стороны расхождения внутри
        мастера (6.2 против 6.4) сходятся в одном — в WB остаток занижают
        всегда, и двойное вычитание ошибается в безопасную сторону. Вторая:
        шаг 15 полного прогона проверяет именно эту формулу, а прогон — это
        контракт, тесты пишутся против него (правило 9.5.3).

        Цена решения реальна: при активных резервах WB видит меньше товара,
        чем есть. Разбор и решение — за потоком 0 и владельцем, см.
        services/wms/FINDINGS.md, находка 1.

        Отрицательный результат публикуется нулём, а не выбрасывается: строка,
        не доехавшая до Wildberries, оставит там прежнее большее число — то
        есть продажу того, чего нет.
        """
        with self._pool.connection() as connection:
            with transaction(connection) as cursor:
                cursor.execute(
                    "SELECT s.barcode, s.buffer, "
                    "       COALESCE(SUM(b.qty) FILTER (WHERE b.state = 'good'), 0) AS good, "
                    "       COALESCE(SUM(b.qty) FILTER (WHERE b.state = 'reserved'), 0) AS reserved "
                    "  FROM sku s "
                    "  JOIN owner o ON o.id = s.owner_id "
                    "  LEFT JOIN stock_balance b ON b.sku_id = s.id AND b.owner_id = s.owner_id "
                    " WHERE o.seller_external_id = %s "
                    " GROUP BY s.id, s.barcode, s.buffer ORDER BY s.barcode", (seller,))
                rows, dropped = [], 0
                for row in cursor.fetchall():
                    barcode = (row["barcode"] or "").strip()
                    if not barcode:
                        dropped += 1
                        continue
                    available = int(row["good"]) - int(row["reserved"]) - int(row["buffer"] or 0)
                    rows.append({"barcode": barcode, "available": max(0, available)})
                return rows, dropped


def _owner_view(row: dict[str, Any]) -> dict[str, Any]:
    return {"owner_id": str(row["id"]), "seller_external_id": row["seller_external_id"],
            "name": row["name"], "inn": row["inn"], "active": row["active"],
            "allow_ledger_short": row["allow_ledger_short"]}


def _product_view(row: dict[str, Any]) -> dict[str, Any]:
    return {"sku_id": str(row["id"]), "barcode": row["barcode"],
            "seller_sku": row["seller_sku"], "name": row["name"],
            "buffer": int(row["buffer"] or 0),
            "seller_external_id": row.get("seller_external_id")}


def _account_view(row: dict[str, Any], seller: str | None) -> dict[str, Any]:
    """Проекция кабинета. Поля со значением токена здесь нет намеренно."""
    return {"id": str(row["id"]), "external_id": row["external_id"],
            "owner_external_id": seller, "display_name": row["display_name"],
            "secret_ref": row["secret_ref"], "scopes": row["scopes"] or [],
            "mode": row["mode"], "status": row["status"],
            "token_type": row.get("token_type"),
            "token_expires_at": _isoformat(row.get("token_expires_at")),
            "last_verified_at": _isoformat(row.get("last_verified_at")),
            "last_sync_at": _isoformat(row.get("last_sync_at")),
            "next_sync_at": _isoformat(row.get("next_sync_at")),
            "wb_warehouse_id": row.get("wb_warehouse_id")}


def _json_or_none(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)
