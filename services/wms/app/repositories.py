"""Запросы к базе wms. Ни одного HTTP-вызова, ни одного решения — только SQL.

Функции принимают открытый курсор: транзакцией управляет вызывающий
(service.py), потому что раздел 6.2 требует, чтобы задание, резерв, движение
и событие ложились ОДНИМ коммитом. Репозиторий, открывающий транзакцию сам,
это правило бы тихо нарушил.

Баланс здесь нигде не пишется напрямую: `stock_balance` — проекция журнала
(инвариант 3), и триггер миграции 004 такую запись просто не пропустит.
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Iterable, Sequence

from psycopg import Cursor

from .domain import ErrorCode

# Пространство имён для детерминированных uuid. Отказ резерва происходит до
# появления задания, а outbox требует aggregate_id типа uuid: номер заказа WB
# разворачивается в постоянный uuid, чтобы у повторного отказа по тому же
# заказу продолжалась та же нумерация, а не начиналась новая.
AGGREGATE_NAMESPACE = uuid.UUID("6b3a1d0e-6c37-4f1a-9a2e-0f2b5c8d4a71")


def aggregate_for_order(wb_order_id: Any) -> uuid.UUID:
    return uuid.uuid5(AGGREGATE_NAMESPACE, f"wb-order:{wb_order_id}")


# --------------------------------------------------------------- владельцы

def find_owner(cursor: Cursor, seller_external_id: str) -> dict[str, Any] | None:
    cursor.execute(
        "SELECT id, seller_external_id, name, inn, active, allow_ledger_short "
        "  FROM owner WHERE seller_external_id = %s",
        (seller_external_id,))
    return cursor.fetchone()


def upsert_owner(cursor: Cursor, seller_external_id: str, *, name: str | None = None,
                 inn: str | None = None, active: bool | None = None,
                 allow_ledger_short: bool | None = None) -> tuple[dict[str, Any], bool]:
    """Заводит владельца или обновляет заполненные поля. Возвращает (строка, создан).

    COALESCE на каждом поле: приёмка зовёт этот же путь, когда видит продавца
    впервые (приложение C), и передаёт только имя с ИНН. Затирать ими флаг
    клапана или активность нельзя.
    """
    cursor.execute(
        "INSERT INTO owner (id, seller_external_id, name, inn, active, allow_ledger_short) "
        "VALUES (%(id)s, %(external)s, %(name)s, %(inn)s, "
        "        COALESCE(%(active)s, true), COALESCE(%(short)s, true)) "
        "ON CONFLICT (seller_external_id) DO UPDATE SET "
        "    name = COALESCE(%(name)s, owner.name), "
        "    inn = COALESCE(%(inn)s, owner.inn), "
        "    active = COALESCE(%(active)s, owner.active), "
        "    allow_ledger_short = COALESCE(%(short)s, owner.allow_ledger_short) "
        "RETURNING id, seller_external_id, name, inn, active, allow_ledger_short, "
        "          (xmax = 0) AS created",
        {"id": uuid.uuid4(), "external": seller_external_id,
         "name": name or seller_external_id, "inn": inn,
         "active": active, "short": allow_ledger_short})
    row = cursor.fetchone()
    assert row is not None
    return row, bool(row.pop("created"))


def list_owners(cursor: Cursor, limit: int = 500) -> list[dict[str, Any]]:
    cursor.execute(
        "SELECT id, seller_external_id, name, inn, active, allow_ledger_short "
        "  FROM owner ORDER BY seller_external_id LIMIT %s", (limit,))
    return cursor.fetchall()


# ------------------------------------------------------------------- товар

def upsert_sku(cursor: Cursor, owner_id: uuid.UUID, barcode: str, *,
               seller_sku: str | None = None, name: str | None = None,
               buffer: int | None = None) -> tuple[dict[str, Any], bool]:
    cursor.execute(
        "INSERT INTO sku (id, owner_id, seller_sku, barcode, name, buffer) "
        "VALUES (%(id)s, %(owner)s, %(seller_sku)s, %(barcode)s, %(name)s, COALESCE(%(buffer)s, 0)) "
        "ON CONFLICT (owner_id, barcode) DO UPDATE SET "
        "    seller_sku = COALESCE(%(seller_sku)s, sku.seller_sku), "
        "    name = COALESCE(%(name)s, sku.name), "
        "    buffer = COALESCE(%(buffer)s, sku.buffer) "
        "RETURNING id, owner_id, seller_sku, barcode, name, buffer, (xmax = 0) AS created",
        {"id": uuid.uuid4(), "owner": owner_id, "seller_sku": seller_sku,
         "barcode": barcode, "name": name, "buffer": buffer})
    row = cursor.fetchone()
    assert row is not None
    return row, bool(row.pop("created"))


# Ограничение на неоднозначность (раздел 3.2): два совпадения — это
# AMBIGUOUS_PRODUCT_MAPPING, а не «берём первое». У одной карточки WB
# несколько размеров; штрихкод определяет вещь на полке, артикул — только модель.
_LOOKUP_LIMIT = 2

_SKU_BY_BARCODE = ("SELECT id, owner_id, seller_sku, barcode, name, buffer FROM sku "
                   " WHERE owner_id = %s AND barcode = %s LIMIT %s")
_SKU_BY_SELLER_SKU = ("SELECT id, owner_id, seller_sku, barcode, name, buffer FROM sku "
                      " WHERE owner_id = %s AND seller_sku = %s LIMIT %s")


def find_sku(cursor: Cursor, owner_id: uuid.UUID, *, barcode: str | None,
             sku_field: str | None) -> tuple[dict[str, Any] | None, ErrorCode | None]:
    """Поиск товара в три шага: barcode → external_sku → barcode == sku.

    Третий шаг существует потому, что у Wildberries поле называется `sku`, но
    приходит в нём штрихкод (раздел 3.2). Порядок шагов сохранён с боевого
    моста: сначала то, что клиент назвал штрихкодом, потом артикул продавца,
    и только потом содержимое `sku` как штрихкод.
    """
    steps = (
        (_SKU_BY_BARCODE, barcode),
        (_SKU_BY_SELLER_SKU, sku_field),
        (_SKU_BY_BARCODE, sku_field),
    )
    seen: set[tuple[str, str]] = set()
    for statement, value in steps:
        if not value or (statement, value) in seen:
            continue
        seen.add((statement, value))
        cursor.execute(statement, (owner_id, value, _LOOKUP_LIMIT))
        rows = cursor.fetchall()
        if len(rows) > 1:
            return None, ErrorCode.AMBIGUOUS_PRODUCT_MAPPING
        if rows:
            return rows[0], None
    return None, ErrorCode.PRODUCT_MAPPING_MISSING


# ---------------------------------------------------------- кабинеты WB

def find_account(cursor: Cursor, *, external_id: str | None = None,
                 account_id: uuid.UUID | None = None) -> dict[str, Any] | None:
    if account_id is not None:
        cursor.execute(
            "SELECT id, owner_id, external_id, display_name, secret_ref, scopes, mode, status, "
            "       wb_warehouse_id FROM wb_account WHERE id = %s", (account_id,))
        return cursor.fetchone()
    if not external_id:
        return None
    cursor.execute(
        "SELECT id, owner_id, external_id, display_name, secret_ref, scopes, mode, status, "
        "       wb_warehouse_id FROM wb_account WHERE external_id = %s", (external_id,))
    return cursor.fetchone()


def sole_account_of_owner(cursor: Cursor, owner_id: uuid.UUID) -> dict[str, Any] | None:
    """Единственный кабинет владельца.

    Кабинет в запросе резерва необязателен по контракту, а `wms_task` без него
    не существует. Угадывать можно только когда кабинет один: у владельца с
    двумя кабинетами (в сиде такой есть) выбор за нас сделать некому.
    """
    cursor.execute(
        "SELECT id, owner_id, external_id, display_name, secret_ref, scopes, mode, status, "
        "       wb_warehouse_id FROM wb_account WHERE owner_id = %s LIMIT 2", (owner_id,))
    rows = cursor.fetchall()
    return rows[0] if len(rows) == 1 else None


# --------------------------------------------------------------- задание

TASK_COLUMNS = (
    "id, wb_order_id, wb_order_uid, wb_account_id, owner_id, sku_id, barcode, quantity, "
    "deadline, state, wb_status, reservation_id, label_id, package_ref, supply_id, "
    "assignee, claimed_at, claim_expires_at, cancel_reason, manual_review_code, "
    "manual_review_reason, last_reconciled_at, created_at, updated_at, version")


def insert_task(cursor: Cursor, *, task_id: uuid.UUID, wb_order_id: int,
                wb_order_uid: str | None, account_id: uuid.UUID, owner_id: uuid.UUID,
                sku_id: uuid.UUID | None, barcode: str | None, quantity: int,
                deadline: Any, state: str, wb_status: str | None = None,
                manual_review_code: str | None = None,
                manual_review_reason: str | None = None) -> dict[str, Any] | None:
    """Заводит задание. None — задание с этим заказом WB уже есть.

    ON CONFLICT DO NOTHING по `wb_order_id` — это идемпотентность опроса
    (инвариант 5): повторный опрос того же заказа обязан дать тот же ответ,
    а не второе задание и не второе движение товара.
    """
    cursor.execute(
        f"INSERT INTO wms_task (id, wb_order_id, wb_order_uid, wb_account_id, owner_id, "
        f"                      sku_id, barcode, quantity, deadline, state, wb_status, "
        f"                      manual_review_code, manual_review_reason) "
        f"VALUES (%(id)s, %(order)s, %(uid)s, %(account)s, %(owner)s, %(sku)s, %(barcode)s, "
        f"        %(quantity)s, %(deadline)s, %(state)s, %(wb_status)s, %(code)s, %(reason)s) "
        f"ON CONFLICT (wb_order_id) DO NOTHING "
        f"RETURNING {TASK_COLUMNS}",
        {"id": task_id, "order": wb_order_id, "uid": wb_order_uid, "account": account_id,
         "owner": owner_id, "sku": sku_id, "barcode": barcode, "quantity": quantity,
         "deadline": deadline, "state": state, "wb_status": wb_status,
         "code": manual_review_code, "reason": manual_review_reason})
    return cursor.fetchone()


def task_by_order(cursor: Cursor, wb_order_id: int, *,
                  for_update: bool = False) -> dict[str, Any] | None:
    cursor.execute(
        f"SELECT {TASK_COLUMNS} FROM wms_task WHERE wb_order_id = %s"
        + (" FOR UPDATE" if for_update else ""),
        (wb_order_id,))
    return cursor.fetchone()


def task_by_id(cursor: Cursor, task_id: uuid.UUID, *,
               for_update: bool = False) -> dict[str, Any] | None:
    cursor.execute(
        f"SELECT {TASK_COLUMNS} FROM wms_task WHERE id = %s"
        + (" FOR UPDATE" if for_update else ""),
        (task_id,))
    return cursor.fetchone()


def attach_reservation(cursor: Cursor, task_id: uuid.UUID, reservation_id: uuid.UUID,
                       state: str = "reserved") -> None:
    cursor.execute(
        "UPDATE wms_task SET reservation_id = %s, state = %s, version = version + 1 "
        " WHERE id = %s", (reservation_id, state, task_id))


# ------------------------------------------------------------- склад и адреса

def find_cell(cursor: Cursor, address: str) -> dict[str, Any] | None:
    cursor.execute(
        "SELECT c.id, c.address, c.route_order, c.zone_id, z.warehouse_id "
        "  FROM cell c JOIN zone z ON z.id = c.zone_id WHERE c.address = %s", (address,))
    return cursor.fetchone()


def ensure_warehouse(cursor: Cursor, code: str) -> dict[str, Any]:
    cursor.execute(
        "INSERT INTO warehouse (id, code) VALUES (%s, %s) "
        "ON CONFLICT (code) DO UPDATE SET code = EXCLUDED.code RETURNING id, code",
        (uuid.uuid4(), code))
    row = cursor.fetchone()
    assert row is not None
    return row


def ensure_zone(cursor: Cursor, warehouse_id: uuid.UUID, code: str, kind: str) -> dict[str, Any]:
    cursor.execute(
        "INSERT INTO zone (id, warehouse_id, code, kind) VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (warehouse_id, code) DO UPDATE SET kind = EXCLUDED.kind "
        "RETURNING id, warehouse_id, code, kind",
        (uuid.uuid4(), warehouse_id, code, kind))
    row = cursor.fetchone()
    assert row is not None
    return row


def ensure_cell(cursor: Cursor, address: str, *, warehouse_code: str = "RUM",
                zone_code: str = "STORAGE", zone_kind: str = "storage",
                route_order: int | None = None) -> dict[str, Any]:
    """Ячейка по адресу, заводится при первом упоминании.

    Отдельного маршрута создания ячеек в контракте нет (приложение B), а
    документ начального остатка и приёмка адресуют строки именно адресом.
    Пока схема адресации не утверждена владельцем (раздел 13, вопрос 1),
    первое упоминание адреса и есть заведение ячейки — иначе приёмку нечем
    принять, а сборщику некуда идти.
    """
    existing = find_cell(cursor, address)
    if existing is not None:
        return existing
    warehouse = ensure_warehouse(cursor, warehouse_code)
    zone = ensure_zone(cursor, warehouse["id"], zone_code, zone_kind)
    cursor.execute(
        "INSERT INTO cell (id, zone_id, address, route_order) VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (address) DO UPDATE SET address = EXCLUDED.address "
        "RETURNING id, address, route_order, zone_id",
        (uuid.uuid4(), zone["id"], address, route_order))
    row = cursor.fetchone()
    assert row is not None
    row["warehouse_id"] = warehouse["id"]
    return row


# Адрес, по которому лежит товар, которого по учёту нет. Клапан 6.5 обязан
# назвать ячейку: событие wms.stock.shortfall.v1 требует cell_id, и «не знаем
# где» — это тоже адрес, который начальник склада может пойти проверить.
LEDGER_SHORT_CELL_SUFFIX = "-LEDGER-SHORT"


def ledger_short_cell(cursor: Cursor, owner_id: uuid.UUID, sku_id: uuid.UUID, *,
                      warehouse_code: str = "RUM") -> dict[str, Any]:
    """Куда записать сборку без остатка.

    Сначала — туда, где товар по учёту лежал последним: расхождение адресное,
    и разбирать его пойдут именно туда. Если следов нет вовсе — в отдельную
    ячейку «неизвестно где», а не в первую попавшуюся: приписать недостачу
    чужому адресу хуже, чем честно сказать, что адреса нет.
    """
    cursor.execute(
        "SELECT b.cell_id AS id, b.box_id, c.address "
        "  FROM stock_balance b JOIN cell c ON c.id = b.cell_id "
        " WHERE b.owner_id = %s AND b.sku_id = %s "
        " ORDER BY (b.state = 'good') DESC, b.qty DESC, c.route_order NULLS LAST "
        " LIMIT 1", (owner_id, sku_id))
    row = cursor.fetchone()
    if row is not None:
        return row
    cursor.execute(
        "SELECT m.cell_to AS id, m.box_to AS box_id, c.address "
        "  FROM stock_move m JOIN cell c ON c.id = m.cell_to "
        " WHERE m.owner_id = %s AND m.sku_id = %s AND m.cell_to IS NOT NULL "
        " ORDER BY m.id DESC LIMIT 1", (owner_id, sku_id))
    row = cursor.fetchone()
    if row is not None:
        return row
    cell = ensure_cell(cursor, f"{warehouse_code}{LEDGER_SHORT_CELL_SUFFIX}",
                       warehouse_code=warehouse_code,
                       zone_code="QUARANTINE", zone_kind="quarantine")
    cell["box_id"] = None
    return cell


def find_box(cursor: Cursor, barcode: str) -> dict[str, Any] | None:
    cursor.execute(
        "SELECT id, barcode, owner_id, sku_id, cell_id, quantity, counted, comment, state "
        "  FROM box WHERE barcode = %s", (barcode,))
    return cursor.fetchone()


def ensure_box(cursor: Cursor, barcode: str, *, owner_id: uuid.UUID,
               sku_id: uuid.UUID | None, cell_id: uuid.UUID | None,
               comment: str, created_by: uuid.UUID | None = None) -> dict[str, Any]:
    """Коробка по штрихкоду. Комментарий обязателен на уровне схемы.

    «Через месяц стоят сотни одинаковых коробок» (раздел 2.9) — поэтому пустой
    комментарий отбивается базой, а не полагается на дисциплину вызывающего.
    """
    cursor.execute(
        "INSERT INTO box (id, barcode, owner_id, sku_id, cell_id, comment, created_by) "
        "VALUES (%(id)s, %(barcode)s, %(owner)s, %(sku)s, %(cell)s, %(comment)s, %(actor)s) "
        "ON CONFLICT (barcode) DO UPDATE SET "
        "    sku_id = COALESCE(box.sku_id, EXCLUDED.sku_id), "
        "    cell_id = COALESCE(EXCLUDED.cell_id, box.cell_id) "
        "RETURNING id, barcode, owner_id, sku_id, cell_id, quantity, counted, comment, state",
        {"id": uuid.uuid4(), "barcode": barcode, "owner": owner_id, "sku": sku_id,
         "cell": cell_id, "comment": comment, "actor": created_by})
    row = cursor.fetchone()
    assert row is not None
    return row


# ------------------------------------------------------------ журнал остатков

def lock_good_placements(cursor: Cursor, owner_id: uuid.UUID,
                         sku_id: uuid.UUID) -> list[dict[str, Any]]:
    """Берёт под блокировку все места, где лежит годный товар (инвариант 4).

    Порядок фиксирован и одинаков для всех: десять параллельных писателей,
    берущих строки в разном порядке, дают взаимную блокировку. Сортировка по
    маршруту обхода заодно означает, что резерв встаёт на ближний к воротам
    товар, а не на случайный.
    """
    cursor.execute(
        "SELECT b.cell_id, b.box_id, b.qty, c.route_order "
        "  FROM stock_balance b JOIN cell c ON c.id = b.cell_id "
        " WHERE b.owner_id = %s AND b.sku_id = %s AND b.state = 'good' AND b.qty > 0 "
        " ORDER BY c.route_order NULLS LAST, b.cell_id, b.box_id NULLS FIRST "
        "   FOR UPDATE OF b", (owner_id, sku_id))
    return cursor.fetchall()


def insert_move(cursor: Cursor, *, owner_id: uuid.UUID, sku_id: uuid.UUID, qty: int,
                reason: str, doc_type: str, idem_key: str,
                cell_from: uuid.UUID | None = None, cell_to: uuid.UUID | None = None,
                box_from: uuid.UUID | None = None, box_to: uuid.UUID | None = None,
                state_from: str | None = None, state_to: str | None = None,
                doc_ref: str | None = None,
                actor_id: uuid.UUID | None = None) -> dict[str, Any] | None:
    """Дописывает движение. None — движение с этим idem_key уже записано.

    Баланс пересчитает триггер: писать в `stock_balance` руками нельзя нигде
    (инвариант 3), и миграция 004 это не даёт сделать даже случайно.
    """
    cursor.execute(
        "INSERT INTO stock_move (owner_id, sku_id, cell_from, cell_to, box_from, box_to, "
        "                        state_from, state_to, qty, reason, doc_type, doc_ref, "
        "                        actor_id, idem_key) "
        "VALUES (%(owner)s, %(sku)s, %(cell_from)s, %(cell_to)s, %(box_from)s, %(box_to)s, "
        "        %(state_from)s, %(state_to)s, %(qty)s, %(reason)s, %(doc_type)s, "
        "        %(doc_ref)s, %(actor)s, %(idem)s) "
        "ON CONFLICT (idem_key) DO NOTHING "
        "RETURNING id, qty, reason, doc_type, idem_key",
        {"owner": owner_id, "sku": sku_id, "cell_from": cell_from, "cell_to": cell_to,
         "box_from": box_from, "box_to": box_to, "state_from": state_from,
         "state_to": state_to, "qty": qty, "reason": reason, "doc_type": doc_type,
         "doc_ref": doc_ref, "actor": actor_id, "idem": idem_key})
    return cursor.fetchone()


def balance_of(cursor: Cursor, owner_id: uuid.UUID, sku_id: uuid.UUID,
               state: str = "good") -> int:
    cursor.execute(
        "SELECT COALESCE(SUM(qty), 0) AS qty FROM stock_balance "
        " WHERE owner_id = %s AND sku_id = %s AND state = %s", (owner_id, sku_id, state))
    row = cursor.fetchone()
    return int(row["qty"]) if row else 0


# ------------------------------------------------------------------- резерв

def insert_reservation(cursor: Cursor, *, reservation_id: uuid.UUID, task_id: uuid.UUID,
                       owner_id: uuid.UUID, sku_id: uuid.UUID, qty: int,
                       cell_id: uuid.UUID | None, box_id: uuid.UUID | None,
                       expires_at: Any = None) -> dict[str, Any]:
    """Резерв под задание. На задание живёт ровно один действующий резерв.

    `cell_id`/`box_id` — основное место, откуда взят товар. Если резерв собран
    из нескольких мест, точная раскладка лежит в движениях с тем же doc_ref:
    журнал — источник истины, резерв — только притязание на него.
    """
    cursor.execute(
        "INSERT INTO reservation (id, task_id, owner_id, sku_id, cell_id, box_id, qty, "
        "                         state, expires_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, 'held', %s) "
        "RETURNING id, task_id, owner_id, sku_id, cell_id, box_id, qty, state, created_at",
        (reservation_id, task_id, owner_id, sku_id, cell_id, box_id, qty, expires_at))
    row = cursor.fetchone()
    assert row is not None
    return row


def held_reservation(cursor: Cursor, task_id: uuid.UUID, *,
                     for_update: bool = False) -> dict[str, Any] | None:
    cursor.execute(
        "SELECT id, task_id, owner_id, sku_id, cell_id, box_id, qty, state "
        "  FROM reservation WHERE task_id = %s AND state = 'held'"
        + (" FOR UPDATE" if for_update else ""),
        (task_id,))
    return cursor.fetchone()


def reservation_moves(cursor: Cursor, reservation_id: uuid.UUID) -> list[dict[str, Any]]:
    """Из каких мест был собран резерв — чтобы вернуть товар туда же."""
    cursor.execute(
        "SELECT cell_from, cell_to, box_from, box_to, qty, reason FROM stock_move "
        " WHERE doc_type = 'reservation' AND doc_ref = %s ORDER BY id",
        (str(reservation_id),))
    return cursor.fetchall()


def release_reservation(cursor: Cursor, reservation_id: uuid.UUID, reason: str) -> None:
    cursor.execute(
        "UPDATE reservation SET state = 'released', released_at = now(), release_reason = %s "
        " WHERE id = %s AND state = 'held'", (reason, reservation_id))


# -------------------------------------------------------------- расхождения

def insert_discrepancy(cursor: Cursor, *, owner_id: uuid.UUID, sku_id: uuid.UUID,
                       kind: str, qty: int, receipt_id: uuid.UUID | None = None,
                       task_id: uuid.UUID | None = None, cell_id: uuid.UUID | None = None,
                       comment: str | None = None, liable: str | None = None,
                       actor_id: uuid.UUID | None = None) -> dict[str, Any]:
    cursor.execute(
        "INSERT INTO discrepancy (id, receipt_id, task_id, owner_id, sku_id, cell_id, "
        "                         kind, qty, comment, liable, actor_id) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
        "RETURNING id, kind, qty, decision, cell_id, created_at",
        (uuid.uuid4(), receipt_id, task_id, owner_id, sku_id, cell_id, kind, qty,
         comment, liable, actor_id))
    row = cursor.fetchone()
    assert row is not None
    return row


# ------------------------------------------------------------------- outbox

def next_sequence(cursor: Cursor, aggregate_id: uuid.UUID) -> int:
    """Следующий номер события в пределах агрегата.

    Приложение E: потребители полагаются на строгую монотонность sequence по
    заданию. Блокировка по агрегату берётся до чтения максимума — иначе две
    транзакции вычислят один и тот же номер и вторая упрётся в уникальный
    индекс уже после того, как сделала работу.
    """
    cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (str(aggregate_id),))
    cursor.execute(
        "SELECT COALESCE(MAX(sequence), 0) + 1 AS next FROM outbox WHERE aggregate_id = %s",
        (aggregate_id,))
    row = cursor.fetchone()
    return int(row["next"]) if row else 1


def insert_outbox(cursor: Cursor, *, event_id: uuid.UUID, event_type: str, tenant_id: str,
                  payload: dict[str, Any], correlation_id: str, aggregate_id: uuid.UUID,
                  sequence: int, occurred_at: Any = None) -> dict[str, Any]:
    cursor.execute(
        "INSERT INTO outbox (event_id, type, tenant_id, occurred_at, payload, "
        "                    correlation_id, aggregate_id, sequence) "
        "VALUES (%s, %s, %s, COALESCE(%s, now()), %s, %s, %s, %s) "
        "RETURNING id, event_id, type, occurred_at, sequence",
        (event_id, event_type, tenant_id, occurred_at, json.dumps(payload, ensure_ascii=False),
         correlation_id, aggregate_id, sequence))
    row = cursor.fetchone()
    assert row is not None
    return row


def pending_outbox(cursor: Cursor, limit: int = 200) -> list[dict[str, Any]]:
    """Очередь публикатора.

    SKIP LOCKED: публикаторов может быть несколько, и одно событие не должно
    уехать в шину дважды из-за того, что второй воркер ждал первого.
    """
    cursor.execute(
        "SELECT id, event_id, type, tenant_id, occurred_at, payload, correlation_id, "
        "       aggregate_id, sequence, attempts "
        "  FROM outbox WHERE published_at IS NULL "
        " ORDER BY occurred_at, id LIMIT %s FOR UPDATE SKIP LOCKED", (limit,))
    return cursor.fetchall()


def mark_published(cursor: Cursor, rows: Sequence[tuple[int, Any]]) -> None:
    if not rows:
        return
    cursor.executemany(
        "UPDATE outbox SET published_at = now() WHERE id = %s AND occurred_at = %s", rows)


def mark_failed(cursor: Cursor, rows: Iterable[tuple[int, Any, str]]) -> None:
    payload = list(rows)
    if not payload:
        return
    cursor.executemany(
        "UPDATE outbox SET attempts = attempts + 1, last_error = %s "
        " WHERE id = %s AND occurred_at = %s",
        [(error[:500], row_id, occurred_at) for row_id, occurred_at, error in payload])


# --------------------------------------------------- лизинг опроса кабинетов

def lease_accounts(cursor: Cursor, *, limit: int = 8, lease_seconds: int = 120,
                   modes: Sequence[str] = ("shadow", "live"),
                   only: Sequence[str] | None = None) -> list[dict[str, Any]]:
    """Занимает кабинеты под опрос.

    Паттерн лизинга перенесён из существующего шлюза как есть (файл 02: он
    написан хорошо, переносить как есть): `sync_claimed_at`, `sync_attempts`,
    `next_sync_at`. Смысл — два опросчика не должны бить в один кабинет: лимит
    у Wildberries общий на кабинет, и вдвоём они выберут его вдвое быстрее.

    SKIP LOCKED, а не ожидание: занятый кабинет опросит тот, кто его занял.
    """
    cursor.execute(
        "WITH due AS ("
        "    SELECT id FROM wb_account "
        "     WHERE status IN ('ACTIVE', 'RATE_LIMITED') "
        "       AND mode = ANY(%(modes)s) "
        # Переключение на новую WMS идёт по одному кабинету, не пачкой
        # (раздел 11, шаг 6), и откат обязан быть возможен за минуту.
        # Пустой список означает «все», а не «ни одного».
        "       AND (%(only)s::text[] IS NULL OR external_id = ANY(%(only)s)) "
        "       AND (next_sync_at IS NULL OR next_sync_at <= now()) "
        "       AND (sync_claimed_at IS NULL "
        "            OR sync_claimed_at < now() - make_interval(secs => %(lease)s)) "
        "     ORDER BY next_sync_at NULLS FIRST "
        "     LIMIT %(limit)s FOR UPDATE SKIP LOCKED) "
        "UPDATE wb_account a SET sync_claimed_at = now() "
        "  FROM due, owner o WHERE a.id = due.id AND o.id = a.owner_id "
        "RETURNING a.id, a.owner_id, a.external_id, a.display_name, a.secret_ref, a.mode, "
        "          a.status, a.wb_warehouse_id, a.sync_attempts, o.seller_external_id, "
        "          o.allow_ledger_short",
        {"limit": limit, "lease": lease_seconds, "modes": list(modes),
         "only": list(only) if only else None})
    return cursor.fetchall()


def sync_cursor(cursor: Cursor, account_id: uuid.UUID) -> int:
    cursor.execute("SELECT cursor FROM wb_sync_cursor WHERE account_id = %s", (account_id,))
    row = cursor.fetchone()
    if row is None or row["cursor"] is None:
        return 0
    try:
        return int(row["cursor"])
    except (TypeError, ValueError):
        return 0


def save_sync_cursor(cursor: Cursor, account_id: uuid.UUID, value: int) -> None:
    cursor.execute(
        "INSERT INTO wb_sync_cursor (account_id, cursor, last_seen_at) "
        "VALUES (%s, %s, now()) "
        "ON CONFLICT (account_id) DO UPDATE SET cursor = EXCLUDED.cursor, "
        "                                       last_seen_at = now()",
        (account_id, str(value)))


def finish_sync(cursor: Cursor, account_id: uuid.UUID, *, next_in_seconds: float,
                error_code: str | None = None, status: str | None = None) -> None:
    """Закрывает цикл опроса кабинета.

    Счётчик попыток сбрасывается только успехом: по нему видно кабинет, который
    «работает», но каждый раз падает, — молчащий воркер считается сломанным
    (инвариант 14).
    """
    cursor.execute(
        "UPDATE wb_account SET "
        "    sync_claimed_at = NULL, "
        "    last_sync_at = CASE WHEN %(error)s::text IS NULL THEN now() ELSE last_sync_at END, "
        "    next_sync_at = now() + make_interval(secs => %(next)s), "
        "    sync_attempts = CASE WHEN %(error)s::text IS NULL "
        "                         THEN 0 ELSE sync_attempts + 1 END, "
        "    sync_error_code = %(error)s::text, "
        "    status = COALESCE(%(status)s::text, status) "
        "  WHERE id = %(account)s",
        {"account": account_id, "next": float(next_in_seconds),
         "error": error_code, "status": status})


def owner_by_id(cursor: Cursor, owner_id: uuid.UUID) -> dict[str, Any] | None:
    cursor.execute(
        "SELECT id, seller_external_id, name, inn, active, allow_ledger_short "
        "  FROM owner WHERE id = %s", (owner_id,))
    return cursor.fetchone()


def known_orders(cursor: Cursor, wb_order_ids: Sequence[int]) -> set[int]:
    """Какие из этих заказов WB уже стали заданиями.

    Опрос идёт с перекрытием (раздел 6.7), поэтому большая часть страницы —
    это заказы, которые мы уже завели. Спрашивать про них по одному значит
    открывать транзакцию и брать блокировку строки ради заведомо известного
    ответа: 22 кабинета по два опроса в секунду дали бы сотни таких
    транзакций в секунду на пустом месте.
    """
    if not wb_order_ids:
        return set()
    cursor.execute("SELECT wb_order_id FROM wms_task WHERE wb_order_id = ANY(%s)",
                   (list(wb_order_ids),))
    return {int(row["wb_order_id"]) for row in cursor.fetchall()}


def take_slot_and_cursor(cursor: Cursor, account_id: uuid.UUID, *, cost: int = 1,
                         limit: int = 300) -> tuple[bool, float, int]:
    """Занять место в минутном окне и прочитать курсор — одним запросом.

    Опросчик делает это перед каждым вызовом в Wildberries, то есть десятки
    раз в секунду на все кабинеты. Два круга до сервера вместо одного здесь
    дороже самой работы.
    """
    cursor.execute(
        "WITH permit AS ("
        "    INSERT INTO wb_rate_limit (account_id, window_start, used) "
        "    VALUES (%(account)s, date_trunc('minute', now()), %(cost)s) "
        "    ON CONFLICT (account_id, window_start) DO UPDATE SET "
        "        used = wb_rate_limit.used + %(cost)s "
        "      WHERE wb_rate_limit.used + %(cost)s <= %(limit)s "
        "        AND (wb_rate_limit.blocked_until IS NULL "
        "             OR wb_rate_limit.blocked_until <= now()) "
        "    RETURNING used) "
        "SELECT (SELECT used FROM permit) AS used, "
        "       (SELECT cursor FROM wb_sync_cursor WHERE account_id = %(account)s) AS cursor, "
        "       EXTRACT(EPOCH FROM (date_trunc('minute', now()) "
        "                           + interval '1 minute' - now())) AS wait",
        {"account": account_id, "cost": cost, "limit": limit})
    row = cursor.fetchone() or {}
    allowed = row.get("used") is not None
    try:
        position = int(row.get("cursor") or 0)
    except (TypeError, ValueError):
        position = 0
    return allowed, float(row.get("wait") or 0), position


def finish_sync_with_cursor(cursor: Cursor, account_id: uuid.UUID, *, cursor_value: int,
                            next_in_seconds: float, status: str | None = None) -> None:
    """Сохранить курсор и закрыть цикл опроса — одним запросом."""
    cursor.execute(
        "WITH saved AS ("
        "    INSERT INTO wb_sync_cursor (account_id, cursor, last_seen_at) "
        "    VALUES (%(account)s, %(cursor)s, now()) "
        "    ON CONFLICT (account_id) DO UPDATE SET cursor = EXCLUDED.cursor, "
        "                                           last_seen_at = now()) "
        "UPDATE wb_account SET sync_claimed_at = NULL, last_sync_at = now(), "
        "                      next_sync_at = now() + make_interval(secs => %(next)s), "
        "                      sync_attempts = 0, sync_error_code = NULL, "
        "                      status = COALESCE(%(status)s::text, status) "
        "  WHERE id = %(account)s",
        {"account": account_id, "cursor": str(cursor_value),
         "next": float(next_in_seconds), "status": status})


# ------------------------------------------------- стикеры и поставки

def tasks_awaiting_labels(cursor: Cursor, *, limit: int = 100,
                          only_accounts: Sequence[str] | None = None,
                          modes: Sequence[str] = ("live",)) -> list[dict[str, Any]]:
    """Задания, у которых ещё нет действующего стикера.

    Порядок по сроку WB, а не по времени создания: если стикеров успевает
    выехать не всё, первыми обязаны получить их те задания, которые раньше
    везти.

    Режимы кабинета фильтруются вызывающим: запрос стикера — это запись в WB
    (он кладёт заказ в поставку), а в shadow писать нельзя. Что считать
    разрешённым, решает `wb.writes_allowed`, а не этот запрос.
    """
    cursor.execute(
        "SELECT t.id, t.wb_order_id, t.wb_account_id, t.owner_id, t.supply_id, "
        "       a.external_id AS account_external_id, a.secret_ref "
        "  FROM wms_task t "
        "  JOIN wb_account a ON a.id = t.wb_account_id "
        "  LEFT JOIN wb_label l ON l.task_id = t.id AND l.invalidated_at IS NULL "
        " WHERE t.state IN ('reserved', 'picking', 'picked') "
        "   AND l.id IS NULL "
        "   AND a.mode = ANY(%(modes)s) AND a.status = 'ACTIVE' "
        "   AND (%(only)s::text[] IS NULL OR a.external_id = ANY(%(only)s)) "
        " ORDER BY t.deadline NULLS LAST, t.created_at "
        " LIMIT %(limit)s",
        {"limit": limit, "only": list(only_accounts) if only_accounts else None,
         "modes": list(modes)})
    return cursor.fetchall()


def open_supply(cursor: Cursor, account_id: uuid.UUID) -> dict[str, Any]:
    """Накопительная поставка кабинета. Одна открытая на кабинет.

    Открывается лениво и живёт локально до первого обращения к WB: пока в неё
    нечего класть, создавать поставку в Wildberries незачем.
    """
    cursor.execute(
        "INSERT INTO wb_supply (id, wb_account_id, state) VALUES (%s, %s, 'open') "
        "ON CONFLICT (wb_account_id) WHERE state = 'open' DO NOTHING "
        "RETURNING id, wb_account_id, wb_supply_id, state",
        (uuid.uuid4(), account_id))
    row = cursor.fetchone()
    if row is not None:
        return row
    cursor.execute(
        "SELECT id, wb_account_id, wb_supply_id, state FROM wb_supply "
        " WHERE wb_account_id = %s AND state = 'open'", (account_id,))
    row = cursor.fetchone()
    assert row is not None
    return row


def bind_supply_to_wb(cursor: Cursor, supply_id: uuid.UUID, wb_supply_id: str) -> None:
    cursor.execute("UPDATE wb_supply SET wb_supply_id = %s WHERE id = %s AND wb_supply_id IS NULL",
                   (wb_supply_id, supply_id))


def attach_tasks_to_supply(cursor: Cursor, task_ids: Sequence[uuid.UUID],
                           supply_id: uuid.UUID) -> None:
    cursor.execute("UPDATE wms_task SET supply_id = %s WHERE id = ANY(%s) AND supply_id IS NULL",
                   (supply_id, list(task_ids)))


def save_label(cursor: Cursor, *, task_id: uuid.UUID, payload: bytes, checksum: str,
               label_format: str) -> dict[str, Any] | None:
    """Кладёт стикер рядом с заданием и связывает их.

    Версия растёт при перевыпуске: у отменённого и заново собранного задания
    стикер другой, и печатать старый нельзя.
    """
    cursor.execute(
        "INSERT INTO wb_label (id, task_id, format, payload, checksum) "
        "VALUES (%(id)s, %(task)s, %(format)s, %(payload)s, %(checksum)s) "
        "ON CONFLICT (task_id) DO UPDATE SET "
        "    format = EXCLUDED.format, payload = EXCLUDED.payload, "
        "    checksum = EXCLUDED.checksum, fetched_at = now(), "
        "    invalidated_at = NULL, version = wb_label.version + 1 "
        "RETURNING id, task_id, format, checksum, version, fetched_at",
        {"id": uuid.uuid4(), "task": task_id, "format": label_format,
         "payload": payload, "checksum": checksum})
    label = cursor.fetchone()
    if label is not None:
        cursor.execute("UPDATE wms_task SET label_id = %s WHERE id = %s",
                       (label["id"], task_id))
    return label


def invalidate_label(cursor: Cursor, task_id: uuid.UUID) -> None:
    """Стикер отменённого задания больше не действителен (раздел 6.6)."""
    cursor.execute(
        "UPDATE wb_label SET invalidated_at = now() "
        " WHERE task_id = %s AND invalidated_at IS NULL", (task_id,))


def label_of(cursor: Cursor, task_id: uuid.UUID) -> dict[str, Any] | None:
    cursor.execute(
        "SELECT id, task_id, format, payload, checksum, version, fetched_at, invalidated_at "
        "  FROM wb_label WHERE task_id = %s", (task_id,))
    return cursor.fetchone()


def available_for_push(cursor: Cursor, owner_id: uuid.UUID,
                       sku_ids: Any = None) -> list[dict[str, Any]]:
    """Строки для публикации в WB: `available = good − reserved − buffer`.

    Отрицательное значение публикуется нулём, а не выбрасывается: строка, не
    доехавшая до Wildberries, оставит там прежнее большее число, то есть
    продажу того, чего нет. Занижать всегда (инвариант 7).
    """
    cursor.execute(
        "SELECT s.barcode, "
        "       GREATEST(0, "
        "           COALESCE(SUM(b.qty) FILTER (WHERE b.state = 'good'), 0) "
        "         - COALESCE(SUM(b.qty) FILTER (WHERE b.state = 'reserved'), 0) "
        "         - s.buffer)::int AS available "
        "  FROM sku s "
        "  LEFT JOIN stock_balance b ON b.sku_id = s.id AND b.owner_id = s.owner_id "
        " WHERE s.owner_id = %(owner)s "
        "   AND (%(skus)s::uuid[] IS NULL OR s.id = ANY(%(skus)s)) "
        "   AND s.barcode IS NOT NULL AND length(btrim(s.barcode)) > 0 "
        " GROUP BY s.id, s.barcode, s.buffer ORDER BY s.barcode",
        {"owner": owner_id, "skus": list(sku_ids) if sku_ids else None})
    return cursor.fetchall()


def record_stock_push(cursor: Cursor, *, account_id: uuid.UUID, rows: int,
                      result: dict[str, Any]) -> None:
    """Журнал публикаций: когда мы последний раз сказали WB про этот кабинет."""
    cursor.execute(
        "INSERT INTO wb_stock_push (id, account_id, rows, result) VALUES (%s, %s, %s, %s)",
        (uuid.uuid4(), account_id, rows, json.dumps(result, ensure_ascii=False)))


def accounts_of_owner(cursor: Cursor, owner_id: uuid.UUID) -> list[dict[str, Any]]:
    """Кабинеты владельца, годные для публикации остатка."""
    cursor.execute(
        "SELECT id, owner_id, external_id, secret_ref, mode, status, wb_warehouse_id "
        "  FROM wb_account WHERE owner_id = %s AND status IN ('ACTIVE', 'RATE_LIMITED') "
        " ORDER BY external_id", (owner_id,))
    return cursor.fetchall()
