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


def remember_rejection(cursor: Cursor, *, wb_order_id: int, error_code: str,
                       seller_hint: str | None) -> bool:
    """Помнит отказ по заказу, для которого нельзя завести задание.

    `True` — отказ новый и о нём стоит сказать наружу. `False` — этот заказ
    уже отвергали: событие было, и второе такое же никому не нужно.

    Отказ без задания эмитил `wms.reservation.failed.v1` при КАЖДОМ опросе:
    заказ неизвестного продавца приезжал каждые две секунды и каждые две
    секунды рождал событие. За сутки — сорок тысяч событий об одном заказе,
    и в этом шуме тонули настоящие отказы.
    """
    cursor.execute(
        "INSERT INTO wb_order_rejected (wb_order_id, error_code, seller_hint) "
        "VALUES (%s, %s, %s) "
        "ON CONFLICT (wb_order_id) DO UPDATE SET "
        "    last_seen_at = now(), seen = wb_order_rejected.seen + 1, "
        "    error_code = EXCLUDED.error_code "
        "RETURNING (xmax = 0) AS created",
        (wb_order_id, error_code, seller_hint))
    row = cursor.fetchone()
    return bool(row and row["created"])


def park_unprocessable_order(cursor: Cursor, *, wb_account_id: uuid.UUID,
                            owner_id: uuid.UUID, wb_order_id: int,
                            code: str, reason: str) -> dict[str, Any] | None:
    """Заводит задание в `manual_review` по неразобранному заказу WB.

    Количество ставится 1: схема требует положительное, а настоящее как раз и
    не разобрано. Штрихкод и срок не ставятся вовсе — ровно поэтому заказ и
    попал сюда. Всё непонятое остаётся в `manual_review_reason` для человека.

    Молча пропустить такой заказ нельзя: он существует у Wildberries, и срок
    по нему идёт. Невидимое задание — это тот же просроченный заказ, только
    без следа.
    """
    cursor.execute(
        f"INSERT INTO wms_task (id, wb_order_id, wb_account_id, owner_id, quantity, "
        f"                      state, manual_review_code, manual_review_reason) "
        f"VALUES (%(id)s, %(order)s, %(account)s, %(owner)s, 1, 'manual_review', "
        f"        %(code)s, %(reason)s) "
        f"ON CONFLICT (wb_order_id) DO NOTHING "
        f"RETURNING {TASK_COLUMNS}",
        {"id": uuid.uuid4(), "order": wb_order_id, "account": wb_account_id,
         "owner": owner_id, "code": code, "reason": reason[:2000]})
    return cursor.fetchone()


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
        "SELECT b.id, b.barcode, b.owner_id, b.sku_id, b.cell_id, b.quantity, "
        "       b.counted, b.comment, b.state, o.seller_external_id "
        "  FROM box b JOIN owner o ON o.id = b.owner_id "
        " WHERE b.barcode = %s", (barcode,))
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
        "  WHERE box.owner_id = EXCLUDED.owner_id "
        "RETURNING id, barcode, owner_id, sku_id, cell_id, quantity, counted, comment, state",
        {"id": uuid.uuid4(), "barcode": barcode, "owner": owner_id, "sku": sku_id,
         "cell": cell_id, "comment": comment, "actor": created_by})
    row = cursor.fetchone()
    if row is None:
        # Ключ `box.barcode` глобален, а коробка принадлежит клиенту. Коробка
        # с таким штрихкодом уже есть у ДРУГОГО клиента: `ON CONFLICT DO
        # UPDATE` без проверки владельца перекладывал в неё чужой товар —
        # изоляция владельца (инвариант 6) кончалась на штрихкоде коробки.
        raise ValueError(
            f"коробка {barcode!r} принадлежит другому клиенту: "
            f"штрихкод коробки уникален на складе, а не у владельца")
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


def consume_reservation(cursor: Cursor, reservation_id: uuid.UUID) -> None:
    """Резерв израсходован: товар уехал со склада.

    Отличается от `released` по смыслу и потому отдельным состоянием: снятый
    резерв означает «товар вернулся на полку», израсходованный — «товара
    больше нет». Состояние `consumed` стояло в схеме с первого дня и не
    использовалось нигде: отгрузка закрывала задание, а резерв оставался
    `held` навсегда, и `reserved` в остатке не убывал.
    """
    cursor.execute(
        "UPDATE reservation SET state = 'consumed' WHERE id = %s AND state = 'held'",
        (reservation_id,))


def reserved_places(cursor: Cursor, *, owner_id: uuid.UUID, sku_id: uuid.UUID,
                    for_update: bool = False) -> list[dict[str, Any]]:
    """Где сейчас лежит зарезервированный товар владельца.

    Откат резерва брал раскладку из ИСТОРИЧЕСКИХ движений резерва. За время
    жизни резерва товар могли переложить: инвентаризация, перемещение между
    ячейками, разбор коробки. Возврат по старым движениям писал товар в
    ячейку, где его давно нет, и заводил там отрицательный остаток, а в
    настоящей ячейке — излишек.

    Текущий баланс знает, где вещь лежит на самом деле.
    """
    cursor.execute(
        "SELECT cell_id, box_id, qty FROM stock_balance "
        " WHERE owner_id = %s AND sku_id = %s AND state = 'reserved' AND qty > 0 "
        " ORDER BY qty DESC, cell_id"
        + (" FOR UPDATE" if for_update else ""),
        (owner_id, sku_id))
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


def pending_outbox(cursor: Cursor, limit: int = 200,
                   lease_seconds: int = 30) -> list[dict[str, Any]]:
    """Очередь публикатора с явным лизингом.

    `FOR UPDATE SKIP LOCKED` держит строку ровно до конца запроса: пачка
    прочитана, блокировки отпущены, а публикация только началась. Второй
    публикатор в этот момент видит те же строки непубликованными и отправляет
    их второй раз — потребитель получает дубль.

    `claimed_until` переживает конец запроса. SKIP LOCKED остаётся: он
    разводит двух публикаторов, стартовавших одновременно, по разным пачкам.
    Просроченный лизинг снова свободен — публикатор мог упасть посреди пачки.
    """
    cursor.execute(
        "WITH due AS ("
        "    SELECT id, occurred_at FROM outbox "
        "     WHERE published_at IS NULL "
        "       AND (claimed_until IS NULL OR claimed_until <= now()) "
        "     ORDER BY occurred_at, id LIMIT %(limit)s FOR UPDATE SKIP LOCKED) "
        "UPDATE outbox o SET claimed_until = now() + make_interval(secs => %(lease)s) "
        "  FROM due WHERE o.id = due.id AND o.occurred_at = due.occurred_at "
        "RETURNING o.id, o.event_id, o.type, o.tenant_id, o.occurred_at, o.payload, "
        "          o.correlation_id, o.aggregate_id, o.sequence, o.attempts",
        {"limit": limit, "lease": lease_seconds})
    rows = cursor.fetchall()
    rows.sort(key=lambda row: (row["occurred_at"], row["id"]))
    return rows


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
                          exclude_accounts: Sequence[Any] | None = None,
                          modes: Sequence[str] = ("live",)) -> list[dict[str, Any]]:
    """Задания, у которых ещё нет действующего стикера.

    Порядок по сроку WB, а не по времени создания: если стикеров успевает
    выехать не всё, первыми обязаны получить их те задания, которые раньше
    везти.

    Режимы кабинета фильтруются вызывающим: запрос стикера — это запись в WB
    (он кладёт заказ в поставку), а в shadow писать нельзя. Что считать
    разрешённым, решает `wb.writes_allowed`, а не этот запрос.

    `exclude_accounts` — кабинеты, которым сейчас не звонят: они только что
    ответили отказом и стоят на паузе. Без этого кабинет, чьи заказы WB не
    знает вовсе, набивает собой всю пачку и стикеры не достаются никому —
    очередь встаёт головой.
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
        "   AND (%(skip)s::uuid[] IS NULL OR NOT (t.wb_account_id = ANY(%(skip)s))) "
        " ORDER BY t.deadline NULLS LAST, t.created_at "
        " LIMIT %(limit)s",
        {"limit": limit, "only": list(only_accounts) if only_accounts else None,
         "skip": list(exclude_accounts) if exclude_accounts else None,
         "modes": list(modes)})
    return cursor.fetchall()


def tasks_needing_supply(cursor: Cursor, *, limit: int = 100,
                         only_accounts: Sequence[str] | None = None,
                         exclude_accounts: Sequence[Any] | None = None,
                         modes: Sequence[str] = ("live",)) -> list[dict[str, Any]]:
    """Задания со стикером, но без поставки.

    Такое бывает после `deliver`: несобранное освобождается из поставки, чтобы
    машина уехала с тем, что лежит в коробе. Стикер у задания при этом
    действующий, и `tasks_awaiting_labels` его больше не видит — там условие
    «стикера нет». Задание остаётся вне поставки навсегда: `deliver` его не
    возьмёт (его нет в поставке), а стикеровщик не тронет (стикер есть).

    Этим заданиям нужен не стикер, а только `add_orders` + `attach`.
    """
    cursor.execute(
        "SELECT t.id, t.wb_order_id, t.wb_account_id, t.owner_id, t.supply_id, "
        "       a.external_id AS account_external_id, a.secret_ref "
        "  FROM wms_task t "
        "  JOIN wb_account a ON a.id = t.wb_account_id "
        "  JOIN wb_label l ON l.task_id = t.id AND l.invalidated_at IS NULL "
        " WHERE t.state = ANY(%(alive)s) AND t.supply_id IS NULL "
        "   AND a.mode = ANY(%(modes)s) AND a.status = 'ACTIVE' "
        "   AND (%(only)s::text[] IS NULL OR a.external_id = ANY(%(only)s)) "
        "   AND (%(skip)s::uuid[] IS NULL OR NOT (t.wb_account_id = ANY(%(skip)s))) "
        " ORDER BY t.deadline NULLS LAST, t.created_at "
        " LIMIT %(limit)s",
        {"limit": limit, "only": list(only_accounts) if only_accounts else None,
         "skip": list(exclude_accounts) if exclude_accounts else None,
         "modes": list(modes), "alive": list(ATTACHABLE_STATES)})
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


# Состояния, в которых задание ещё живо и стикер ему нужен.
#
# Между «спросили стикер у WB» и «сохранили ответ» проходит вызов в сеть — до
# секунды. Задание за это время могут отменить: клиент отменил заказ, сверка
# увидела `cancel`, человек нажал отмену. Ответ WB, записанный вслепую,
# ВОСКРЕШАЛ такое задание: `invalidated_at` сбрасывался в NULL, и отменённое
# задание снова выглядело готовым к отгрузке — со стикером и в поставке.
ALIVE_FOR_LABEL = ("reserved", "picking", "picked")

# В поставку кладут и собранное: `add_orders` собирает машину из упакованного.
# Не кладут отменённое, уехавшее и остановленное сверкой — им в машине нечего
# делать, а WB будет ждать их там.
ATTACHABLE_STATES = ALIVE_FOR_LABEL + ("packed", "labeled")


def attach_tasks_to_supply(cursor: Cursor, task_ids: Sequence[uuid.UUID],
                           supply_id: uuid.UUID) -> list[uuid.UUID]:
    """Кладёт задания в поставку. Возвращает те, что действительно легли."""
    cursor.execute(
        "UPDATE wms_task SET supply_id = %s "
        " WHERE id = ANY(%s) AND supply_id IS NULL AND state = ANY(%s) "
        "RETURNING id",
        (supply_id, list(task_ids), list(ATTACHABLE_STATES)))
    return [row["id"] for row in cursor.fetchall()]


def save_label(cursor: Cursor, *, task_id: uuid.UUID, payload: bytes, checksum: str,
               label_format: str) -> dict[str, Any] | None:
    """Кладёт стикер рядом с заданием и связывает их.

    Версия растёт при перевыпуске: у отменённого и заново собранного задания
    стикер другой, и печатать старый нельзя.
    """
    cursor.execute(
        # Сохраняем только живому заданию. `SELECT` в источнике вставки, а не
        # проверка в коде: между проверкой и вставкой отмена успеет пройти
        # снова, а здесь условие держит та же строка, что и пишет.
        "INSERT INTO wb_label (id, task_id, format, payload, checksum) "
        "SELECT %(id)s, %(task)s, %(format)s, %(payload)s, %(checksum)s "
        "  FROM wms_task t WHERE t.id = %(task)s AND t.state = ANY(%(alive)s) "
        "ON CONFLICT (task_id) DO UPDATE SET "
        "    format = EXCLUDED.format, payload = EXCLUDED.payload, "
        "    checksum = EXCLUDED.checksum, fetched_at = now(), "
        "    invalidated_at = NULL, version = wb_label.version + 1 "
        "RETURNING id, task_id, format, checksum, version, fetched_at",
        {"id": uuid.uuid4(), "task": task_id, "format": label_format,
         "payload": payload, "checksum": checksum, "alive": list(ALIVE_FOR_LABEL)})
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
    """Строки для публикации в WB: `available = good − buffer`.

    Резерв здесь НЕ вычитается: транзакция раздела 6.2 переводит товар
    движением `good → reserved`, то есть в проекции `stock_balance` состояние
    `good` зарезервированного уже не содержит. Вычесть `reserved` второй раз —
    занизить остаток вдвое по активным резервам: при 10 единицах и резерве на 3
    в Wildberries уезжало 4 вместо 7. Занижение само по себе намеренно
    (инвариант 7), но это уже не страховка, а потерянные продажи клиента.

    Отрицательное значение публикуется нулём, а не выбрасывается: строка, не
    доехавшая до Wildberries, оставит там прежнее большее число, то есть
    продажу того, чего нет.
    """
    cursor.execute(
        "SELECT s.barcode, "
        "       GREATEST(0, "
        "           COALESCE(SUM(b.qty) FILTER (WHERE b.state = 'good'), 0) "
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


# ---------------------------------------------------------------- приёмка

def receipt_by_reference(cursor: Cursor, reference: str,
                         owner_id: uuid.UUID | None = None) -> dict[str, Any] | None:
    """Приёмка по номеру. Без владельца — по всей таблице, и это находка.

    Номер документа уникален у владельца, а не глобально: «ТН-1» есть у
    каждого второго клиента. Поиск по всей таблице отдавал клиенту A приёмку
    клиента B вместе с чужими строками — изоляция владельца (инвариант 6)
    кончалась на номере накладной.
    """
    cursor.execute(
        "SELECT id, owner_id, reference, warehouse_id, state, created_at "
        "  FROM receipt WHERE reference = %s "
        "   AND (%s::uuid IS NULL OR owner_id = %s)", (reference, owner_id, owner_id))
    return cursor.fetchone()


def insert_receipt(cursor: Cursor, *, owner_id: uuid.UUID, reference: str,
                   warehouse_id: uuid.UUID, actor_id: uuid.UUID | None) -> dict[str, Any]:
    cursor.execute(
        "INSERT INTO receipt (id, owner_id, reference, warehouse_id, state, actor_id) "
        "VALUES (%s, %s, %s, %s, 'counting', %s) "
        "RETURNING id, owner_id, reference, warehouse_id, state, created_at",
        (uuid.uuid4(), owner_id, reference, warehouse_id, actor_id))
    row = cursor.fetchone()
    assert row is not None
    return row


def insert_receipt_line(cursor: Cursor, *, receipt_id: uuid.UUID, sku_id: uuid.UUID,
                        expected_qty: int | None, actual_qty: int | None,
                        box_id: uuid.UUID | None, cell_id: uuid.UUID | None) -> None:
    """Строка приёмки. Одна на товар и ячейку — приёмку досчитывают повтором.

    Без ключа каждый повтор добавлял вторую строку про тот же товар, и
    «сколько чего принято» переставало читаться из таблицы вовсе. Пересчёт
    перезаписывает объявленное и фактическое, но не стирает уже
    пересчитанное пустым значением.
    """
    cursor.execute(
        "INSERT INTO receipt_line (id, receipt_id, sku_id, expected_qty, actual_qty, "
        "                          box_id, cell_id) VALUES (%s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (receipt_id, sku_id, cell_id) WHERE cell_id IS NOT NULL "
        "DO UPDATE SET expected_qty = COALESCE(EXCLUDED.expected_qty, "
        "                                      receipt_line.expected_qty), "
        "              actual_qty = COALESCE(EXCLUDED.actual_qty, receipt_line.actual_qty), "
        "              box_id = COALESCE(EXCLUDED.box_id, receipt_line.box_id)",
        (uuid.uuid4(), receipt_id, sku_id, expected_qty, actual_qty, box_id, cell_id))


def receipt_is_fully_counted(cursor: Cursor, receipt_id: uuid.UUID) -> bool:
    """Все ли строки приёмки пересчитаны — по таблице, а не по этому вызову.

    Досчёт приходит только с недостающими строками: судить о готовности
    приёмки по ним одним значит закрывать её, пока половина не пересчитана.
    """
    cursor.execute(
        "SELECT count(*) AS n FROM receipt_line "
        " WHERE receipt_id = %s AND actual_qty IS NULL", (receipt_id,))
    row = cursor.fetchone()
    return not (row and int(row["n"]))


def set_receipt_state(cursor: Cursor, receipt_id: uuid.UUID, state: str) -> None:
    cursor.execute("UPDATE receipt SET state = %s WHERE id = %s", (state, receipt_id))


def receipt_line_count(cursor: Cursor, receipt_id: uuid.UUID) -> int:
    cursor.execute("SELECT count(*) AS n FROM receipt_line WHERE receipt_id = %s",
                   (receipt_id,))
    row = cursor.fetchone()
    return int(row["n"]) if row else 0


def discrepancies_of_receipt(cursor: Cursor, receipt_id: uuid.UUID) -> list[dict[str, Any]]:
    cursor.execute(
        "SELECT d.id, d.kind, d.qty, d.decision, d.created_at, s.barcode, c.address AS cell_address "
        "  FROM discrepancy d JOIN sku s ON s.id = d.sku_id "
        "  LEFT JOIN cell c ON c.id = d.cell_id "
        " WHERE d.receipt_id = %s ORDER BY d.created_at", (receipt_id,))
    return cursor.fetchall()


def receipts_for_screen(cursor: Cursor, *, states: Sequence[str],
                        owner_external_id: str | None = None, reference: str | None = None,
                        limit: int = 50) -> list[dict[str, Any]]:
    cursor.execute(
        "SELECT r.id, r.reference, r.state, r.created_at, o.seller_external_id, "
        "       w.code AS warehouse_code, "
        "       (SELECT count(*) FROM receipt_line l WHERE l.receipt_id = r.id) AS lines, "
        "       (SELECT count(*) FROM discrepancy d WHERE d.receipt_id = r.id "
        "          AND d.decision = 'pending') AS open_discrepancies "
        "  FROM receipt r JOIN owner o ON o.id = r.owner_id "
        "  JOIN warehouse w ON w.id = r.warehouse_id "
        " WHERE r.state = ANY(%(states)s) "
        "   AND (%(owner)s::text IS NULL OR o.seller_external_id = %(owner)s) "
        "   AND (%(reference)s::text IS NULL OR r.reference = %(reference)s) "
        " ORDER BY r.created_at DESC LIMIT %(limit)s",
        {"states": list(states), "owner": owner_external_id,
         "reference": reference, "limit": limit})
    return [{"receipt_id": str(row["id"]), "reference": row["reference"],
             "state": row["state"], "owner_external_id": row["seller_external_id"],
             "warehouse_code": row["warehouse_code"], "lines": int(row["lines"]),
             "open_discrepancies": int(row["open_discrepancies"]),
             "created_at": row["created_at"].isoformat()} for row in cursor.fetchall()]



def discrepancies_for_screen(cursor: Cursor, *, kinds: Sequence[str] | None = None,
                             decisions: Sequence[str] | None = None,
                             owner_external_id: str | None = None,
                             task_id: uuid.UUID | None = None,
                             receipt_id: uuid.UUID | None = None,
                             since: Any = None, limit: int = 100) -> list[dict[str, Any]]:
    """Расхождения экрана начальника склада — все, а не только приёмочные.

    `ledger_short` рождается на резерве, а не на приёмке, и `receipt_id` у него
    пуст: через `/receipts/screen` его не увидеть никогда. Схема заводит под
    него отдельный индекс ровно ради этого экрана (миграция 005).
    """
    cursor.execute(
        "SELECT d.id, d.kind, d.qty, d.decision, d.liable, d.comment, d.created_at, "
        "       s.barcode, c.address AS cell_address "
        "  FROM discrepancy d "
        "  JOIN owner o ON o.id = d.owner_id "
        "  LEFT JOIN sku s ON s.id = d.sku_id "
        "  LEFT JOIN cell c ON c.id = d.cell_id "
        " WHERE (%(kinds)s::text[] IS NULL OR d.kind = ANY(%(kinds)s)) "
        "   AND (%(decisions)s::text[] IS NULL OR d.decision = ANY(%(decisions)s)) "
        "   AND (%(owner)s::text IS NULL OR o.seller_external_id = %(owner)s) "
        "   AND (%(task)s::uuid IS NULL OR d.task_id = %(task)s) "
        "   AND (%(receipt)s::uuid IS NULL OR d.receipt_id = %(receipt)s) "
        "   AND (%(since)s::timestamptz IS NULL OR d.created_at >= %(since)s) "
        " ORDER BY d.created_at DESC LIMIT %(limit)s",
        {"kinds": list(kinds) if kinds else None,
         "decisions": list(decisions) if decisions else None,
         "owner": owner_external_id, "task": task_id, "receipt": receipt_id,
         "since": since, "limit": limit})
    return [{"discrepancy_id": str(row["id"]), "kind": row["kind"],
             "barcode": row["barcode"], "qty": int(row["qty"]),
             "decision": row["decision"], "liable": row["liable"],
             "comment": row["comment"], "cell_address": row["cell_address"],
             "created_at": row["created_at"].isoformat()} for row in cursor.fetchall()]


def movements_of(cursor: Cursor, *, owner_id: uuid.UUID, barcode: str | None = None,
                 since: Any = None, until: Any = None,
                 cursor_id: int | None = None,
                 limit: int = 100) -> list[dict[str, Any]]:
    """История движений по SKU — прямо из журнала.

    Журнал append-only и есть единственный источник истины (инвариант 3),
    поэтому история читается из него, а не из отдельной проекции: второй
    источник разошёлся бы с первым молча.

    Листание курсором, а не смещением: движения дописываются во время
    листания, и страница со смещением показала бы одну строку дважды.
    """
    cursor.execute(
        "SELECT m.id, m.ts, m.qty, m.state_from, m.state_to, m.reason, "
        "       m.doc_type, m.doc_ref, s.barcode, "
        "       cf.address AS cell_from, ct.address AS cell_to, "
        "       bf.barcode AS box_from, bt.barcode AS box_to "
        "  FROM stock_move m "
        "  JOIN sku s ON s.id = m.sku_id "
        "  LEFT JOIN cell cf ON cf.id = m.cell_from "
        "  LEFT JOIN cell ct ON ct.id = m.cell_to "
        "  LEFT JOIN box bf ON bf.id = m.box_from "
        "  LEFT JOIN box bt ON bt.id = m.box_to "
        " WHERE m.owner_id = %(owner)s "
        "   AND (%(barcode)s::text IS NULL OR s.barcode = %(barcode)s) "
        "   AND (%(since)s::timestamptz IS NULL OR m.ts >= %(since)s) "
        "   AND (%(until)s::timestamptz IS NULL OR m.ts <= %(until)s) "
        "   AND (%(cursor)s::bigint IS NULL OR m.id < %(cursor)s) "
        " ORDER BY m.id DESC LIMIT %(limit)s",
        {"owner": owner_id, "barcode": barcode, "since": since, "until": until,
         "cursor": cursor_id, "limit": limit})
    # id монотонный (bigint identity) и он же порядок записи, поэтому листаем
    # по нему: сортировка по ts неоднозначна — движения одной транзакции
    # получают одинаковый now().
    return [{"movement_id": str(row["id"]), "occurred_at": row["ts"].isoformat(),
             "barcode": row["barcode"], "qty": int(row["qty"]),
             "state_from": row["state_from"], "state_to": row["state_to"],
             "cell_from": row["cell_from"], "cell_to": row["cell_to"],
             "box_from": row["box_from"], "box_to": row["box_to"],
             "reason": row["reason"], "doc_type": row["doc_type"],
             "doc_ref": row["doc_ref"]} for row in cursor.fetchall()]


def known_barcodes(cursor: Cursor, owner_id: uuid.UUID) -> set[str]:
    """Штрихкоды, заведённые в каталоге склада. Нужны, чтобы отличить
    маппленную карточку от немаппленной: немаппленный товар — это
    `manual_review` с кодом, а не остаток (инвариант 6)."""
    cursor.execute(
        "SELECT barcode FROM sku WHERE owner_id = %s AND barcode IS NOT NULL", (owner_id,))
    return {row["barcode"] for row in cursor.fetchall()}


# ------------------------------------------------------------- размещение

def putaway_queue(cursor: Cursor, *, owner_external_id: str | None = None,
                  limit: int = 100) -> list[dict[str, Any]]:
    """Что лежит в зоне приёмки и ждёт разноса по местам хранения."""
    cursor.execute(
        "SELECT s.barcode, c.address AS cell_address, bx.barcode AS box_barcode, "
        "       b.state, b.qty AS quantity, c.route_order, o.seller_external_id "
        "  FROM stock_balance b "
        "  JOIN owner o ON o.id = b.owner_id "
        "  JOIN sku s ON s.id = b.sku_id "
        "  JOIN cell c ON c.id = b.cell_id "
        "  JOIN zone z ON z.id = c.zone_id "
        "  LEFT JOIN box bx ON bx.id = b.box_id "
        " WHERE b.qty > 0 AND z.kind = 'receiving' "
        "   AND (%(owner)s::text IS NULL OR o.seller_external_id = %(owner)s) "
        " ORDER BY c.route_order NULLS LAST, c.address LIMIT %(limit)s",
        {"owner": owner_external_id, "limit": limit})
    return cursor.fetchall()


def place_box(cursor: Cursor, box_id: uuid.UUID, cell_id: uuid.UUID) -> None:
    cursor.execute("UPDATE box SET cell_id = %s WHERE id = %s", (cell_id, box_id))


def move_box_contents(cursor: Cursor, *, box: dict[str, Any], target_cell: uuid.UUID,
                      reference: str, actor_id: uuid.UUID | None) -> int:
    """Переносит остаток коробки в другую ячейку движениями, а не правкой места.

    Коробка — контейнер, ячейка — адрес (раздел 2.9). Переставить коробку и не
    записать движение значит оставить остаток числиться там, где его нет.
    """
    cursor.execute(
        "SELECT sku_id, box_id, cell_id, state, qty FROM stock_balance "
        " WHERE owner_id = %s AND box_id = %s AND qty > 0 FOR UPDATE",
        (box["owner_id"], box["id"]))
    moved = 0
    for index, row in enumerate(cursor.fetchall()):
        if row["cell_id"] == target_cell:
            continue
        insert_move(
            cursor, owner_id=box["owner_id"], sku_id=row["sku_id"], qty=int(row["qty"]),
            cell_from=row["cell_id"], cell_to=target_cell,
            box_from=box["id"], box_to=box["id"],
            state_from=row["state"], state_to=row["state"],
            reason="putaway", doc_type="putaway", doc_ref=reference, actor_id=actor_id,
            idem_key=f"putaway:{reference}:{box['id']}:{index}")
        moved += 1
    return moved


# ---------------------------------------------------------- инвентаризация

def balance_at(cursor: Cursor, *, owner_id: uuid.UUID, sku_id: uuid.UUID,
               cell_id: uuid.UUID, box_id: uuid.UUID | None, state: str = "good") -> int:
    cursor.execute(
        "SELECT COALESCE(SUM(qty), 0) AS qty FROM stock_balance "
        " WHERE owner_id = %s AND sku_id = %s AND cell_id = %s AND state = %s "
        "   AND box_id IS NOT DISTINCT FROM %s",
        (owner_id, sku_id, cell_id, state, box_id))
    row = cursor.fetchone()
    return int(row["qty"]) if row else 0


def inventory_by_reference(cursor: Cursor, reference: str,
                           owner_id: uuid.UUID | None = None) -> dict[str, Any] | None:
    """Инвентаризация по номеру — в пределах владельца (инвариант 6)."""
    cursor.execute("SELECT id, owner_id, state, scope FROM inventory_count "
                   " WHERE reference = %s AND (%s::uuid IS NULL OR owner_id = %s)",
                   (reference, owner_id, owner_id))
    return cursor.fetchone()


def insert_inventory_count(cursor: Cursor, *, owner_id: uuid.UUID, reference: str,
                           scope: str, actor_id: uuid.UUID | None) -> dict[str, Any]:
    cursor.execute(
        "INSERT INTO inventory_count (id, owner_id, reference, scope, state, actor_id) "
        "VALUES (%s, %s, %s, %s, 'counting', %s) RETURNING id, reference, scope, state",
        (uuid.uuid4(), owner_id, reference, scope, actor_id))
    row = cursor.fetchone()
    assert row is not None
    return row


def insert_inventory_line(cursor: Cursor, *, count_id: uuid.UUID, sku_id: uuid.UUID,
                          cell_id: uuid.UUID | None, box_id: uuid.UUID | None,
                          expected_qty: int | None, fact_qty: int | None) -> None:
    cursor.execute(
        "INSERT INTO inventory_count_line (id, count_id, sku_id, cell_id, box_id, "
        "                                  expected_qty, fact_qty) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (uuid.uuid4(), count_id, sku_id, cell_id, box_id, expected_qty, fact_qty))


def apply_inventory_count(cursor: Cursor, count_id: uuid.UUID) -> None:
    cursor.execute(
        "UPDATE inventory_count SET state = 'applied', applied_at = now() WHERE id = %s",
        (count_id,))


def inventory_sheet(cursor: Cursor, *, seller_external_id: str,
                    cell_address: str | None = None,
                    barcodes: Any = None) -> list[dict[str, Any]]:
    cursor.execute(
        "SELECT s.barcode, c.address AS cell_address, bx.barcode AS box_barcode, "
        "       b.state, b.qty AS expected_qty, c.route_order "
        "  FROM stock_balance b "
        "  JOIN owner o ON o.id = b.owner_id "
        "  JOIN sku s ON s.id = b.sku_id "
        "  JOIN cell c ON c.id = b.cell_id "
        "  LEFT JOIN box bx ON bx.id = b.box_id "
        " WHERE o.seller_external_id = %(seller)s AND b.qty <> 0 "
        "   AND (%(cell)s::text IS NULL OR c.address = %(cell)s) "
        "   AND (%(barcodes)s::text[] IS NULL OR s.barcode = ANY(%(barcodes)s)) "
        " ORDER BY c.route_order NULLS LAST, c.address, s.barcode",
        {"seller": seller_external_id, "cell": cell_address,
         "barcodes": list(barcodes) if barcodes else None})
    return cursor.fetchall()


# ------------------------------------------------------------------ коробки

def boxes_of(cursor: Cursor, *, owner_external_id: str | None = None,
             cell_address: str | None = None, barcode: str | None = None,
             limit: int = 200) -> list[dict[str, Any]]:
    cursor.execute(
        "SELECT bx.barcode, bx.comment, bx.quantity, bx.counted, bx.state, bx.sequence, "
        "       bx.total_boxes, bx.created_at, c.address AS cell_address, "
        "       o.seller_external_id, s.barcode AS sku_barcode "
        "  FROM box bx JOIN owner o ON o.id = bx.owner_id "
        "  LEFT JOIN cell c ON c.id = bx.cell_id "
        "  LEFT JOIN sku s ON s.id = bx.sku_id "
        " WHERE (%(owner)s::text IS NULL OR o.seller_external_id = %(owner)s) "
        "   AND (%(cell)s::text IS NULL OR c.address = %(cell)s) "
        "   AND (%(barcode)s::text IS NULL OR bx.barcode = %(barcode)s) "
        " ORDER BY bx.created_at DESC LIMIT %(limit)s",
        {"owner": owner_external_id, "cell": cell_address, "barcode": barcode,
         "limit": limit})
    return cursor.fetchall()


def remove_box(cursor: Cursor, barcode: str) -> bool:
    cursor.execute(
        "UPDATE box SET state = 'removed' WHERE barcode = %s AND state = 'stored'",
        (barcode,))
    return cursor.rowcount > 0


# ------------------------------------------------------- выдача заданий

# Проекция задания вместе со всем, что нужно строке листа подбора: сборщик
# должен видеть адрес и готовность стикера, а не досбирать это вторым запросом.
_TASK_VIEW = (
    "SELECT t.id, t.wb_order_id, t.wb_order_uid, t.owner_id, t.sku_id, t.barcode, "
    "       t.quantity, t.deadline, t.state, t.wb_status, t.reservation_id, "
    "       t.package_ref, t.supply_id, t.assignee, t.claimed_at, t.claim_expires_at, "
    "       t.cancel_reason, t.manual_review_code, t.manual_review_reason, "
    "       t.last_reconciled_at, t.created_at, t.updated_at, t.version, "
    "       o.seller_external_id, a.external_id AS account_external_id, "
    "       s.seller_sku, s.name AS sku_name, "
    "       l.format AS label_format, l.version AS label_version, "
    "       l.checksum AS label_checksum, l.fetched_at AS label_fetched_at, "
    "       l.invalidated_at AS label_invalidated_at, "
    "       c.address AS cell_address, bx.barcode AS box_barcode "
    "  FROM wms_task t "
    "  JOIN owner o ON o.id = t.owner_id "
    "  JOIN wb_account a ON a.id = t.wb_account_id "
    "  LEFT JOIN sku s ON s.id = t.sku_id "
    "  LEFT JOIN wb_label l ON l.task_id = t.id "
    "  LEFT JOIN reservation r ON r.id = t.reservation_id "
    "  LEFT JOIN cell c ON c.id = r.cell_id "
    "  LEFT JOIN box bx ON bx.id = r.box_id ")


def task_view(cursor: Cursor, task_id: uuid.UUID, *,
              for_update: bool = False) -> dict[str, Any] | None:
    cursor.execute(_TASK_VIEW + " WHERE t.id = %s"
                   + (" FOR UPDATE OF t" if for_update else ""), (task_id,))
    return cursor.fetchone()


def release_expired_claims(cursor: Cursor) -> int:
    """Возвращает в очередь задания за сборщиками, которые не вернулись.

    Без этого задание, выданное ушедшему со смены человеку, не потеряно только
    на бумаге: очередь его больше не видит, и никто за ним не пойдёт.
    """
    cursor.execute(
        # Состояние не ограничивается. Лизинг истёк — значит человека нет, и
        # держать за ним задание незачем в любом состоянии: `picked` и
        # `packed` за ушедшим сборщиком прежде висели вечно, потому что
        # условие перечисляло только `reserved` и `picking`. Задание при этом
        # остаётся в своём состоянии, освобождается только исполнитель.
        "UPDATE wms_task SET assignee = NULL, claimed_at = NULL, claim_expires_at = NULL "
        " WHERE assignee IS NOT NULL AND claim_expires_at < now()")
    return cursor.rowcount


def claim_tasks(cursor: Cursor, *, assignee: Any, limit: int, states: Sequence[str],
                owner_external_ids: Sequence[str] | None, lease_seconds: int,
                claim: bool = True) -> list[dict[str, Any]]:
    """Выдача заданий сборщику: `FOR UPDATE SKIP LOCKED`, порядок по сроку WB.

    `claim = False` — только посмотреть: экран обновляется чаще, чем человек
    берёт работу, и занимать задание при каждом обновлении нельзя.
    """
    selection = (
        "SELECT t.id FROM wms_task t JOIN owner o ON o.id = t.owner_id "
        " WHERE t.state = ANY(%(states)s) AND t.assignee IS NULL "
        "   AND (%(owners)s::text[] IS NULL OR o.seller_external_id = ANY(%(owners)s)) "
        " ORDER BY t.deadline NULLS LAST, t.created_at "
        " LIMIT %(limit)s FOR UPDATE OF t SKIP LOCKED")
    arguments = {"states": list(states), "limit": limit,
                 "owners": list(owner_external_ids) if owner_external_ids else None,
                 "assignee": assignee, "lease": lease_seconds}

    if not claim:
        cursor.execute(f"WITH picked AS ({selection}) " + _TASK_VIEW
                       + " JOIN picked p ON p.id = t.id "
                         " ORDER BY t.deadline NULLS LAST, t.created_at", arguments)
        return cursor.fetchall()

    # Поля исполнителя берутся из `RETURNING`, а не из таблицы.
    #
    # `_TASK_VIEW` читает `wms_task` в том же операторе, что и UPDATE, а
    # видит снимок ДО него: в ответе на выдачу приезжали `assignee: null` и
    # `leased_until: null`. Рабочее место получало задание, за которым по
    # ответу никто не закреплён, и не могло показать, до какого времени оно
    # у сборщика.
    cursor.execute(
        f"WITH picked AS ({selection}), "
        "     taken AS ("
        "         UPDATE wms_task t SET assignee = %(assignee)s, claimed_at = now(), "
        "                claim_expires_at = now() + make_interval(secs => %(lease)s) "
        "           FROM picked p WHERE t.id = p.id "
        "       RETURNING t.id, t.assignee, t.claimed_at, t.claim_expires_at, t.version) "
        + _TASK_VIEW.replace("t.assignee, t.claimed_at, t.claim_expires_at, ",
                             "k.assignee, k.claimed_at, k.claim_expires_at, ")
                    .replace("t.version, ", "k.version, ")
        + " JOIN taken k ON k.id = t.id "
        " ORDER BY t.deadline NULLS LAST, t.created_at", arguments)
    return cursor.fetchall()


def available_for_pull(cursor: Cursor, *, states: Sequence[str],
                       owner_external_ids: Sequence[str] | None) -> int:
    cursor.execute(
        "SELECT count(*) AS n FROM wms_task t JOIN owner o ON o.id = t.owner_id "
        " WHERE t.state = ANY(%(states)s) AND t.assignee IS NULL "
        "   AND (%(owners)s::text[] IS NULL OR o.seller_external_id = ANY(%(owners)s))",
        {"states": list(states),
         "owners": list(owner_external_ids) if owner_external_ids else None})
    row = cursor.fetchone()
    return int(row["n"]) if row else 0


def placements_for_task(cursor: Cursor, task_id: uuid.UUID) -> list[dict[str, Any]]:
    """Откуда брать товар. Отсортировано по маршруту обхода — змейкой по стеллажам."""
    cursor.execute(
        "SELECT s.barcode, c.address AS cell_address, bx.barcode AS box_barcode, "
        "       b.state, b.qty AS quantity, c.route_order "
        "  FROM wms_task t "
        "  JOIN stock_balance b ON b.owner_id = t.owner_id AND b.sku_id = t.sku_id "
        "  JOIN sku s ON s.id = b.sku_id "
        "  JOIN cell c ON c.id = b.cell_id "
        "  LEFT JOIN box bx ON bx.id = b.box_id "
        " WHERE t.id = %s AND b.qty > 0 AND b.state IN ('good', 'reserved') "
        " ORDER BY c.route_order NULLS LAST, c.address", (task_id,))
    return [{"barcode": row["barcode"], "cell_address": row["cell_address"],
             "box_barcode": row["box_barcode"], "state": row["state"],
             "quantity": max(0, int(row["quantity"])), "route_order": row["route_order"]}
            for row in cursor.fetchall()]


def set_task_state(cursor: Cursor, task_id: uuid.UUID, state: str, *,
                   cancel_reason: str | None = None, package_ref: str | None = None,
                   clear_supply: bool = False, clear_assignee: bool = False,
                   clear_reservation: bool = False) -> None:
    cursor.execute(
        "UPDATE wms_task SET state = %(state)s, version = version + 1, "
        "    cancel_reason = COALESCE(%(reason)s, cancel_reason), "
        "    package_ref = COALESCE(%(package)s, package_ref), "
        "    supply_id = CASE WHEN %(clear_supply)s THEN NULL ELSE supply_id END, "
        "    assignee = CASE WHEN %(clear_assignee)s THEN NULL ELSE assignee END, "
        "    claimed_at = CASE WHEN %(clear_assignee)s THEN NULL ELSE claimed_at END, "
        "    claim_expires_at = CASE WHEN %(clear_assignee)s "
        "                            THEN NULL ELSE claim_expires_at END, "
        "    reservation_id = CASE WHEN %(clear_reservation)s THEN NULL ELSE reservation_id END "
        "  WHERE id = %(id)s",
        {"id": task_id, "state": state, "reason": cancel_reason, "package": package_ref,
         "clear_supply": clear_supply, "clear_assignee": clear_assignee,
         "clear_reservation": clear_reservation})


def record_scan(cursor: Cursor, *, task_id: uuid.UUID, owner_id: uuid.UUID,
                sku_id: uuid.UUID | None, result: str) -> None:
    """Сохраняет скан у стойки, включая отклонённый.

    Отклонённый скан обязан остаться: по нему видно, что именно человек взял
    не то (раздел 4, главный рубеж качества).
    """
    if sku_id is None:
        return
    cursor.execute(
        "UPDATE pick_line SET scanned_at = now(), scan_result = %s WHERE task_id = %s",
        (result, task_id))
    if cursor.rowcount:
        return
    # Строки листа подбора ещё нет — сессию заводит поток B, а скан уже
    # случился. Заводим одиночную строку, чтобы факт не потерялся.
    #
    # `actor_id` — исполнитель задания, а НЕ свежий uuid4. Случайный
    # идентификатор выглядел как настоящий человек: «кто это сделал»
    # отвечалось числом, которого нет ни в одной системе, и вопрос «кто
    # спотыкается на этой полке» оставался без ответа. Нет исполнителя — NULL,
    # и это честно.
    cursor.execute("SELECT assignee FROM wms_task WHERE id = %s", (task_id,))
    holder = cursor.fetchone()
    cursor.execute(
        "INSERT INTO pick_session (id, actor_id, state) VALUES (%s, %s, 'picking') "
        "RETURNING id", (uuid.uuid4(), holder["assignee"] if holder else None))
    session = cursor.fetchone()
    assert session is not None
    cursor.execute(
        "SELECT cell_id, box_id, qty FROM reservation "
        " WHERE task_id = %s ORDER BY created_at DESC LIMIT 1", (task_id,))
    reservation = cursor.fetchone() or {"cell_id": None, "box_id": None, "qty": 1}
    cursor.execute(
        "INSERT INTO pick_line (id, session_id, task_id, owner_id, sku_id, cell_id, box_id, "
        "                       qty, scanned_at, scan_result) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now(), %s) "
        "ON CONFLICT (task_id) DO UPDATE SET scanned_at = now(), scan_result = EXCLUDED.scan_result",
        (uuid.uuid4(), session["id"], task_id, owner_id, sku_id, reservation["cell_id"],
         reservation["box_id"], max(1, int(reservation["qty"])), result))


def supply_reference(cursor: Cursor, supply_id: uuid.UUID,
                     wb_order_id: int) -> tuple[str, int] | None:
    """Чем поставка называется у Wildberries — для освобождения заказа из неё."""
    cursor.execute("SELECT wb_supply_id FROM wb_supply WHERE id = %s", (supply_id,))
    row = cursor.fetchone()
    if row is None or not row["wb_supply_id"]:
        return None
    return str(row["wb_supply_id"]), wb_order_id


def box_contents(cursor: Cursor, box_id: uuid.UUID) -> list[dict[str, Any]]:
    cursor.execute(
        "SELECT sku_id, state, qty FROM stock_balance WHERE box_id = %s AND qty <> 0",
        (box_id,))
    return cursor.fetchall()


def storage_lookup(cursor: Cursor, *, owner_external_id: str | None = None,
                   barcode: str | None = None, cell_address: str | None = None,
                   box_barcode: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    """Где лежит товар и что лежит в ячейке — одним запросом на оба вопроса."""
    cursor.execute(
        "SELECT s.barcode, c.address AS cell_address, bx.barcode AS box_barcode, "
        "       b.state, b.qty, c.route_order, o.seller_external_id "
        "  FROM stock_balance b "
        "  JOIN owner o ON o.id = b.owner_id "
        "  JOIN sku s ON s.id = b.sku_id "
        "  JOIN cell c ON c.id = b.cell_id "
        "  LEFT JOIN box bx ON bx.id = b.box_id "
        " WHERE b.qty <> 0 "
        "   AND (%(owner)s::text IS NULL OR o.seller_external_id = %(owner)s) "
        "   AND (%(barcode)s::text IS NULL OR s.barcode = %(barcode)s) "
        "   AND (%(cell)s::text IS NULL OR c.address = %(cell)s) "
        "   AND (%(box)s::text IS NULL OR bx.barcode = %(box)s) "
        " ORDER BY c.route_order NULLS LAST, c.address, s.barcode LIMIT %(limit)s",
        {"owner": owner_external_id, "barcode": barcode, "cell": cell_address,
         "box": box_barcode, "limit": limit})
    return [{"barcode": row["barcode"], "cell_address": row["cell_address"],
             "box_barcode": row["box_barcode"], "state": row["state"],
             "quantity": max(0, int(row["qty"])), "route_order": row["route_order"],
             "owner_external_id": row["seller_external_id"]}
            for row in cursor.fetchall()]


def account_of_supply(cursor: Cursor, wb_supply_id: str) -> dict[str, Any] | None:
    cursor.execute(
        "SELECT a.id, a.external_id, a.secret_ref, a.mode, a.status, a.wb_warehouse_id "
        "  FROM wb_supply s JOIN wb_account a ON a.id = s.wb_account_id "
        " WHERE s.wb_supply_id = %s", (wb_supply_id,))
    return cursor.fetchone()


# --------------------------------------------------------------- отгрузка

def ensure_shipment(cursor: Cursor, *, owner_id: uuid.UUID,
                    supply_id: uuid.UUID) -> dict[str, Any]:
    """Отгрузка под поставку. Одна на поставку, заводится лениво."""
    cursor.execute(
        "SELECT id, owner_id, wb_supply_id, state, closed_at, handed_by, handed_at, "
        "       accepted_at FROM shipment WHERE wb_supply_id = %s", (supply_id,))
    row = cursor.fetchone()
    if row is not None:
        return row
    cursor.execute(
        "INSERT INTO shipment (id, owner_id, wb_supply_id, state) VALUES (%s, %s, %s, 'open') "
        "RETURNING id, owner_id, wb_supply_id, state, closed_at, handed_by, handed_at, "
        "          accepted_at",
        (uuid.uuid4(), owner_id, supply_id))
    row = cursor.fetchone()
    assert row is not None
    return row


def supply_of_account(cursor: Cursor, account_id: uuid.UUID,
                      wb_supply_id: str | None = None) -> dict[str, Any]:
    if wb_supply_id:
        cursor.execute(
            "SELECT id, wb_account_id, wb_supply_id, state FROM wb_supply "
            " WHERE wb_account_id = %s AND wb_supply_id = %s", (account_id, wb_supply_id))
        row = cursor.fetchone()
        if row is not None:
            return row
    return open_supply(cursor, account_id)


def close_supply(cursor: Cursor, supply_id: uuid.UUID, state: str = "closed") -> None:
    cursor.execute(
        "UPDATE wb_supply SET state = %s, closed_at = COALESCE(closed_at, now()) "
        " WHERE id = %s AND state <> %s", (state, supply_id, state))


def supply_order_count(cursor: Cursor, supply_id: uuid.UUID) -> int:
    cursor.execute("SELECT count(*) AS n FROM wms_task WHERE supply_id = %s", (supply_id,))
    row = cursor.fetchone()
    return int(row["n"]) if row else 0


def tasks_of_supply(cursor: Cursor, supply_id: uuid.UUID) -> list[dict[str, Any]]:
    cursor.execute(
        "SELECT id, wb_order_id, owner_id, state FROM wms_task WHERE supply_id = %s "
        " ORDER BY created_at", (supply_id,))
    return cursor.fetchall()


def tasks_not_in_supply(cursor: Cursor, task_ids: Sequence[uuid.UUID],
                        supply_id: uuid.UUID) -> list[dict[str, Any]]:
    cursor.execute(
        "SELECT id, wb_order_id FROM wms_task "
        " WHERE id = ANY(%s) AND (supply_id IS NULL OR supply_id <> %s)",
        (list(task_ids), supply_id))
    return cursor.fetchall()


def tasks_without_metadata(cursor: Cursor, supply_id: uuid.UUID) -> list[dict[str, Any]]:
    """Задания, на которых WB откажет в передаче поставки.

    Валидация `metaDetails` обязательна с релизов 2026-03/04 (приложение D).
    Практически она означает одно: у задания должен быть штрихкод и стикер.
    """
    cursor.execute(
        "SELECT t.id, t.wb_order_id FROM wms_task t "
        "  LEFT JOIN wb_label l ON l.task_id = t.id AND l.invalidated_at IS NULL "
        " WHERE t.supply_id = %s AND t.state NOT IN ('cancelled', 'manual_review') "
        "   AND (t.barcode IS NULL OR l.id IS NULL)", (supply_id,))
    return cursor.fetchall()


def set_shipment_state(cursor: Cursor, shipment_id: uuid.UUID, state: str) -> None:
    cursor.execute(
        "UPDATE shipment SET state = %s, closed_at = COALESCE(closed_at, now()) "
        " WHERE id = %s AND state NOT IN ('handed_to_wb', 'accepted_by_wb')",
        (state, shipment_id))


def hand_over_shipment(cursor: Cursor, shipment_id: uuid.UUID, handed_by: str) -> None:
    """Передачу подтверждает человек, и его подпись сохраняется.

    `handed_by` в схеме uuid — тот же случай, что и с `assignee`: имя
    разворачивается в постоянный uuid (находка 7 в FINDINGS.md).
    """
    cursor.execute(
        "UPDATE shipment SET state = 'handed_to_wb', handed_by = %s, handed_at = now(), "
        "                    closed_at = COALESCE(closed_at, now()) "
        " WHERE id = %s", (_person(handed_by), shipment_id))


def accept_shipment(cursor: Cursor, shipment_id: uuid.UUID) -> None:
    cursor.execute(
        "UPDATE shipment SET state = 'accepted_by_wb', accepted_at = now() WHERE id = %s",
        (shipment_id,))


def tasks_ready_for_supply(cursor: Cursor, *, owner_external_id: str | None = None,
                           limit: int = 100) -> list[dict[str, Any]]:
    cursor.execute(
        _TASK_VIEW + " WHERE t.state IN ('picked', 'packed', 'labeled') "
        "   AND (%(owner)s::text IS NULL OR o.seller_external_id = %(owner)s) "
        " ORDER BY t.deadline NULLS LAST, t.created_at LIMIT %(limit)s",
        {"owner": owner_external_id, "limit": limit})
    return cursor.fetchall()


# Пространство имён для людей, названных именем, а не идентификатором identity.
_PERSON_NAMESPACE = uuid.UUID("2f1c9a44-7b8e-4d5c-9a3f-1e6b0d8c5a72")


def _person(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError):
        return uuid.uuid5(_PERSON_NAMESPACE, value)


# ------------------------------------------------------- рабочие места

def station(cursor: Cursor, station_id: uuid.UUID) -> dict[str, Any] | None:
    cursor.execute(
        "SELECT id, name, printer_host, printer_port, printer_model, transport, active "
        "  FROM station WHERE id = %s", (station_id,))
    return cursor.fetchone()


# ------------------------------------------------------------- возвраты

def upsert_return(cursor: Cursor, *, return_event_id: str, owner_id: uuid.UUID,
                  task_id: uuid.UUID | None,
                  reason: str | None) -> tuple[dict[str, Any], bool]:
    cursor.execute(
        "INSERT INTO wms_return (id, task_id, return_event_id, owner_id, state, reason) "
        "VALUES (%s, %s, %s, %s, 'expected', %s) "
        "ON CONFLICT (return_event_id) DO UPDATE SET reason = COALESCE(EXCLUDED.reason, "
        "                                                              wms_return.reason) "
        "RETURNING id, task_id, return_event_id, owner_id, state, decision, reason, "
        "          (xmax = 0) AS created",
        (uuid.uuid4(), task_id, return_event_id, owner_id, reason))
    row = cursor.fetchone()
    assert row is not None
    return row, bool(row.pop("created"))


def return_by_id_or_event(cursor: Cursor, value: str) -> dict[str, Any] | None:
    """Возврат по нашему uuid или по внешнему ключу события.

    Клиенты зовут маршрут и так и так: `wb-returns` знает событие, экран —
    наш идентификатор.
    """
    try:
        identifier = uuid.UUID(str(value))
    except (ValueError, AttributeError):
        identifier = None
    cursor.execute(
        "SELECT r.id, r.task_id, r.return_event_id, r.owner_id, r.state, r.decision, "
        "       r.reason, o.seller_external_id "
        "  FROM wms_return r JOIN owner o ON o.id = r.owner_id "
        " WHERE (%s::uuid IS NOT NULL AND r.id = %s::uuid) "
        "    OR r.return_event_id = %s",
        (identifier, identifier, str(value)))
    return cursor.fetchone()


def set_return_state(cursor: Cursor, return_id: uuid.UUID, state: str) -> None:
    cursor.execute(
        "UPDATE wms_return SET state = %s, received_at = COALESCE(received_at, now()) "
        " WHERE id = %s", (state, return_id))


def decide_return(cursor: Cursor, return_id: uuid.UUID, decision: str) -> None:
    cursor.execute(
        "UPDATE wms_return SET state = 'decided', decision = %s, "
        "                      received_at = COALESCE(received_at, now()) "
        " WHERE id = %s", (decision, return_id))


def returns_list(cursor: Cursor, *, owner_external_id: str | None = None,
                 states: Any = None, limit: int = 100) -> list[dict[str, Any]]:
    cursor.execute(
        "SELECT r.id, r.return_event_id, r.state, r.decision, r.reason, r.received_at, "
        "       r.created_at, o.seller_external_id, r.task_id "
        "  FROM wms_return r JOIN owner o ON o.id = r.owner_id "
        " WHERE (%(owner)s::text IS NULL OR o.seller_external_id = %(owner)s) "
        "   AND (%(states)s::text[] IS NULL OR r.state = ANY(%(states)s)) "
        " ORDER BY r.created_at DESC LIMIT %(limit)s",
        {"owner": owner_external_id, "states": list(states) if states else None,
         "limit": limit})
    return [{"return_id": str(row["id"]), "return_event_id": row["return_event_id"],
             "state": row["state"], "decision": row["decision"], "reason": row["reason"],
             "owner_external_id": row["seller_external_id"],
             "task_id": str(row["task_id"]) if row["task_id"] else None,
             "received_at": row["received_at"].isoformat() if row["received_at"] else None,
             "created_at": row["created_at"].isoformat()} for row in cursor.fetchall()]


def mark_account_verified(cursor: Cursor, account_id: uuid.UUID, *, verified: bool) -> None:
    """Отметка проверки кабинета.

    Провалившаяся проверка не выключает кабинет молча: она ставит `AUTH_ERROR`,
    чтобы это было видно и опросчику, и человеку.
    """
    cursor.execute(
        "UPDATE wb_account SET last_verified_at = now(), "
        "    status = CASE WHEN %(ok)s THEN 'ACTIVE' "
        "                  WHEN status = 'ACTIVE' THEN 'AUTH_ERROR' ELSE status END "
        "  WHERE id = %(id)s", {"id": account_id, "ok": verified})


# -------------------------------------------------------- журнал команд

def claim_command(cursor: Cursor, *, idempotency_key: str, command: str,
                  aggregate_id: uuid.UUID | None = None) -> dict[str, Any] | None:
    """Занять ключ идемпотентности. `None` — команда уже выполнялась.

    Инвариант 5: повтор команды возвращает тот же ответ, а не делает работу
    второй раз. До журнала ключ только проверялся на непустоту — повтор
    `deliver` заводил вторую отгрузку и второе тарифицируемое событие
    `wb.supply.shipped.v1`, то есть второй счёт клиенту за ту же машину.

    Возвращает запись первой попытки при конфликте — с её сохранённым
    ответом. Ответ может быть ещё пуст: первая попытка идёт прямо сейчас, в
    соседней транзакции. Это тоже повтор, и работа не делается.
    """
    cursor.execute(
        "INSERT INTO command_log (idempotency_key, command, aggregate_id) "
        "VALUES (%s, %s, %s) ON CONFLICT (idempotency_key) DO NOTHING "
        "RETURNING idempotency_key",
        (idempotency_key, command, aggregate_id))
    if cursor.fetchone() is not None:
        return None
    cursor.execute(
        "SELECT idempotency_key, command, aggregate_id, result, created_at "
        "  FROM command_log WHERE idempotency_key = %s", (idempotency_key,))
    return cursor.fetchone()


def save_command_result(cursor: Cursor, *, idempotency_key: str,
                        aggregate_id: uuid.UUID | None,
                        result: dict[str, Any]) -> None:
    """Запомнить ответ команды, чтобы повтор вернул именно его."""
    cursor.execute(
        "UPDATE command_log SET result = %s, "
        "       aggregate_id = COALESCE(%s, aggregate_id) "
        "  WHERE idempotency_key = %s",
        (json.dumps(result, ensure_ascii=False), aggregate_id, idempotency_key))


def record_print(cursor: Cursor, label_id: uuid.UUID) -> bool:
    """Отмечает печать стикера. `True` — печатали впервые.

    Счётчик печатей — метрика качества этикетки и принтера: доля перепечаток
    показывает, где рвётся лента и где стикер не читается. Событие «этикетка
    наклеена» при этом уходит один раз, по первой печати.
    """
    cursor.execute(
        "UPDATE wb_label SET prints = prints + 1, "
        "       printed_at = COALESCE(printed_at, now()) "
        " WHERE id = %s RETURNING prints", (label_id,))
    row = cursor.fetchone()
    return bool(row) and int(row["prints"]) == 1


def forget_command(cursor: Cursor, *, idempotency_key: str) -> None:
    """Освободить ключ: команда отказала и не выполнена.

    Без этого один отказ — испорченная накладная, не заведённый кабинет —
    навсегда занимал бы ключ, и повторить исправленную команду тем же ключом
    стало бы нельзя. Строка снимается только если ответа в ней нет: успешная
    команда остаётся в журнале навсегда.
    """
    cursor.execute(
        "DELETE FROM command_log WHERE idempotency_key = %s AND result IS NULL",
        (idempotency_key,))


# Что считать собранным. `picked` сюда не входит: вещь снята с полки, но не
# упакована, и в коробе её нет.
# `shipped` сюда НЕ входит: уехавшее задание уже не «готово уехать». Пока
# входило, повтор `deliver` находил те же задания собранными и отгружал их
# второй раз — вместе со вторым счётом клиенту.
ASSEMBLED_STATES = ("packed", "labeled")


def assembled_tasks_of_supply(cursor: Cursor, supply_id: uuid.UUID) -> list[dict[str, Any]]:
    """Задания поставки, которые физически собраны и готовы уехать."""
    cursor.execute(
        "SELECT t.id, t.wb_order_id, t.state, "
        "       (t.barcode IS NOT NULL AND l.id IS NOT NULL) AS ready_metadata "
        "  FROM wms_task t "
        "  LEFT JOIN wb_label l ON l.task_id = t.id AND l.invalidated_at IS NULL "
        " WHERE t.supply_id = %s AND t.state = ANY(%s) ORDER BY t.created_at",
        (supply_id, list(ASSEMBLED_STATES)))
    return cursor.fetchall()


def unassembled_tasks_of_supply(cursor: Cursor, supply_id: uuid.UUID) -> list[dict[str, Any]]:
    """Задания поставки, которые ещё лежат на складе.

    В поставку они попали ради стикера — WB выдаёт его только заданию в
    поставке (раздел 6.6). Уехать они не могут: их никто не собирал.
    """
    cursor.execute(
        "SELECT id, wb_order_id, state FROM wms_task "
        " WHERE supply_id = %s AND state <> ALL(%s) AND state <> 'cancelled'",
        (supply_id, list(ASSEMBLED_STATES)))
    return cursor.fetchall()


# ------------------------------------------------------------------ сверка

def accounts_due_for_reconcile(cursor: Cursor, *, limit: int, older_than_seconds: float,
                               only: Sequence[str] | None = None) -> list[dict[str, Any]]:
    """Кабинеты, у которых давно не сверялись задания.

    Режим не важен: сверка читает только `GET /api/v3/orders` и ничего не
    пишет, поэтому работает и в shadow — там она и есть главный инструмент
    (раздел 11, шаг 2).
    """
    cursor.execute(
        "SELECT a.id, a.external_id, a.secret_ref, a.mode, a.owner_id "
        "  FROM wb_account a "
        " WHERE a.status IN ('ACTIVE', 'RATE_LIMITED') "
        "   AND (%(only)s::text[] IS NULL OR a.external_id = ANY(%(only)s)) "
        "   AND EXISTS (SELECT 1 FROM wms_task t "
        "                WHERE t.wb_account_id = a.id "
        "                  AND t.state NOT IN ('cancelled', 'accepted', 'diverged') "
        "                  AND (t.last_reconciled_at IS NULL "
        "                       OR t.last_reconciled_at < now() "
        "                          - make_interval(secs => %(age)s))) "
        " ORDER BY a.external_id LIMIT %(limit)s",
        {"limit": limit, "age": float(older_than_seconds),
         "only": list(only) if only else None})
    return cursor.fetchall()


def open_tasks_of_account(cursor: Cursor, account_id: uuid.UUID, *,
                          limit: int) -> list[int]:
    """Номера незакрытых заданий кабинета — то, о чём надо спросить WB.

    Сверка читала первую страницу `GET /api/v3/orders`. Эта страница — начало
    истории кабинета, то есть самые старые заказы за всё время; открытое
    задание месячной давности в неё не попадает никогда, и его расхождения не
    видит никто. Спрашивать надо адресно: вот наши незакрытые — что с ними.

    `diverged` исключён: он уже остановлен и ждёт человека. `accepted` и
    `cancelled` закрыты.
    """
    cursor.execute(
        "SELECT wb_order_id FROM wms_task "
        " WHERE wb_account_id = %s AND wb_order_id IS NOT NULL "
        "   AND state NOT IN ('cancelled', 'accepted', 'diverged') "
        " ORDER BY COALESCE(last_reconciled_at, to_timestamp(0)), wb_order_id "
        " LIMIT %s", (account_id, limit))
    return [int(row["wb_order_id"]) for row in cursor.fetchall()]


def tasks_for_reconcile(cursor: Cursor, wb_order_ids: Sequence[int]) -> list[dict[str, Any]]:
    cursor.execute(
        # `supply_id` нужен сверке: задание в поставке законно имеет у WB
        # статус `confirm`, даже когда у нас оно ещё `reserved`.
        "SELECT id, wb_order_id, state, wb_status, supply_id FROM wms_task "
        " WHERE wb_order_id = ANY(%s) AND state <> 'diverged' FOR UPDATE",
        (list(wb_order_ids),))
    return cursor.fetchall()


def mark_reconciled(cursor: Cursor, task_id: uuid.UUID, *, wb_status: str | None) -> None:
    cursor.execute(
        "UPDATE wms_task SET last_reconciled_at = now(), wb_status = COALESCE(%s, wb_status) "
        " WHERE id = %s", (wb_status, task_id))


def mark_diverged(cursor: Cursor, task_id: uuid.UUID, *, wb_status: str | None) -> None:
    """Задание останавливается и ждёт человека, а не перезаписывается.

    Кто прав — неизвестно: у WB может быть отмена, которой мы не видели, а у
    нас отгрузка, о которой WB ещё не знает. Догадка здесь дороже разбора.
    """
    cursor.execute(
        "UPDATE wms_task SET state = 'diverged', wb_status = COALESCE(%s, wb_status), "
        "                    last_reconciled_at = now(), version = version + 1 "
        " WHERE id = %s", (wb_status, task_id))


def owners_with_stock(cursor: Cursor) -> dict[uuid.UUID, set[uuid.UUID]]:
    """Владельцы и их товары, у которых есть остаток.

    Нужна страховочной публикации: потерянный вызов оставляет остаток в
    Wildberries расходящимся до следующего движения по тому же товару, а у
    редкого товара это недели (инвариант 7).
    """
    cursor.execute(
        "SELECT owner_id, sku_id FROM stock_balance "
        " WHERE state = 'good' AND qty <> 0 GROUP BY owner_id, sku_id")
    result: dict[uuid.UUID, set[uuid.UUID]] = {}
    for row in cursor.fetchall():
        result.setdefault(row["owner_id"], set()).add(row["sku_id"])
    return result


def divergence_report(cursor: Cursor, *, limit: int = 500) -> list[dict[str, Any]]:
    """Отчёт расхождений по владельцу и товару — то, что читают в shadow.

    Раздел 11, шаг 2: `wms` строит свою таблицу рядом с боевым контуром, и
    ежедневный отчёт показывает, сходятся ли они.
    """
    cursor.execute(
        "SELECT o.seller_external_id, s.barcode, t.state, t.wb_status, count(*) AS tasks, "
        "       min(t.updated_at) AS oldest "
        "  FROM wms_task t "
        "  JOIN owner o ON o.id = t.owner_id "
        "  LEFT JOIN sku s ON s.id = t.sku_id "
        " WHERE t.state = 'diverged' "
        " GROUP BY o.seller_external_id, s.barcode, t.state, t.wb_status "
        " ORDER BY count(*) DESC LIMIT %s", (limit,))
    return cursor.fetchall()


def save_divergence_report(cursor: Cursor, lines: Sequence[dict[str, Any]]) -> int:
    """Сохранить снимок отчёта. Раздел 11, шаг 2.

    Лог для недели наблюдения не годится: он ротируется, теряется при
    пересоздании контейнера и не даёт сравнить «вчера двенадцать, сегодня
    три». А именно это сравнение и есть смысл шага 2 — сходятся ли наши
    задания с боевыми и убывает ли разница.
    """
    if not lines:
        return 0
    cursor.executemany(
        "INSERT INTO shadow_divergence_report "
        "    (seller_external_id, barcode, state, wb_status, tasks, oldest) "
        "VALUES (%(seller_external_id)s, %(barcode)s, %(state)s, %(wb_status)s, "
        "        %(tasks)s, %(oldest)s)",
        [{"seller_external_id": line["seller_external_id"], "barcode": line.get("barcode"),
          "state": line["state"], "wb_status": line.get("wb_status"),
          "tasks": int(line["tasks"]), "oldest": line.get("oldest")} for line in lines])
    return len(lines)
