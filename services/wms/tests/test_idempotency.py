"""Инвариант 5: повтор команды — тот же ответ, а не вторая работа.

До журнала команд ключ идемпотентности только проверялся на непустоту. Повтор
`deliver` находил те же задания собранными (`shipped` входил в
`ASSEMBLED_STATES`), отгружал их второй раз и публиковал второе
`wb.supply.shipped.v1` — то есть второй счёт клиенту за ту же машину.
"""
from __future__ import annotations

import uuid

import pytest

from app import repositories as repo
from app.postgres import ConnectionPool, single
from app.receiving import ReceivingOperations
from app.service import CatalogOperations, StockOperations, WmsService
from app.shipments import ShipmentOperations
from app.tasks import TaskOperations

from dbfixtures import require_database, unique


@pytest.fixture(scope="module")
def pool() -> ConnectionPool:
    connections = ConnectionPool(require_database(), max_size=8)
    yield connections
    connections.close()


def make_client(pool: ConnectionPool, name: str) -> dict:
    catalog = CatalogOperations(pool)
    seller, account = unique("seller"), unique("wb")
    barcode = f"46{uuid.uuid4().int % 10**11:011d}"
    catalog.upsert_owner({"seller_external_id": seller, "name": name})
    catalog.upsert_wb_account({
        "op": "upsert", "external_id": account, "seller_external_id": seller,
        "display_name": f"Кабинет {name}", "secret_ref": f"vault://mmx/test/{account}",
        "mode": "live", "status": "ACTIVE"})
    catalog.ensure_product({"seller_external_id": seller, "barcode": barcode})
    StockOperations(pool).apply_document({
        "seller_external_id": seller, "reference": unique("open"), "doc_type": "opening",
        "lines": [{"barcode": barcode, "quantity": 20,
                   "cell_address": unique("cell").upper(), "state": "good"}]})
    return {"seller": seller, "account": account, "barcode": barcode}


@pytest.fixture()
def client(pool: ConnectionPool) -> dict:
    return make_client(pool, "Идемпотентность")


def rows(pool: ConnectionPool, sql: str, params: tuple) -> list[dict]:
    with pool.connection() as connection:
        with single(connection) as cursor:
            cursor.execute(sql, params)
            return cursor.fetchall()


def assembled_task(pool: ConnectionPool, client: dict) -> str:
    outcome = WmsService(pool).reserve({
        "idempotency_key": unique("idem"), "seller_external_id": client["seller"],
        "wb_account_external_id": client["account"],
        "wb_order_id": uuid.uuid4().int % 10**12, "sku": client["barcode"],
        "barcode": client["barcode"], "quantity": 1, "correlation_id": unique("corr")})
    assert outcome.status == "reserved", outcome.error_code
    with pool.connection() as connection:
        with single(connection) as cursor:
            repo.save_label(cursor, task_id=uuid.UUID(outcome.task_id),
                            payload=b"^XA^FDtest^FS^XZ", checksum="a" * 64,
                            label_format="zplv")
    tasks = TaskOperations(pool, WmsService(pool))
    tasks.scan(outcome.task_id, {"barcode": client["barcode"]})
    tasks.pack(outcome.task_id, {"idempotency_key": unique("pack"),
                                 "control_scan_barcode": client["barcode"]})
    return outcome.task_id


# ------------------------------------------------------------------ отгрузка

def test_repeating_a_delivery_does_not_bill_the_client_twice(
        pool: ConnectionPool, client: dict) -> None:
    """Повтор `deliver` тем же ключом — тот же ответ и одно событие.

    `orders` в `wb.supply.shipped.v1` — то, по чему выставляется счёт
    (приложение E). Второе событие — второй счёт за ту же машину.
    """
    task_id = assembled_task(pool, client)
    shipments = ShipmentOperations(pool, WmsService(pool))
    opened = shipments.handle({"seller_external_id": client["seller"],
                               "idempotency_key": unique("open"), "action": "open"})
    shipments.handle({"seller_external_id": client["seller"],
                      "idempotency_key": unique("add"), "action": "add_orders",
                      "wb_supply_id": opened["wb_supply_id"], "task_ids": [task_id]})

    key = unique("deliver")
    first = shipments.handle({"seller_external_id": client["seller"],
                              "idempotency_key": key, "action": "deliver",
                              "wb_supply_id": opened["wb_supply_id"]})
    second = shipments.handle({"seller_external_id": client["seller"],
                               "idempotency_key": key, "action": "deliver",
                               "wb_supply_id": opened["wb_supply_id"]})

    assert second["duplicate"], "повтор выдал себя за первую отгрузку"
    assert second["shipment_id"] == first["shipment_id"]
    assert second["orders"] == first["orders"]

    events = rows(pool, "SELECT count(*) AS n FROM outbox o JOIN owner w "
                        "    ON w.seller_external_id = %s "
                        " WHERE o.type = 'wb.supply.shipped.v1' "
                        "   AND o.payload->>'seller_id' = %s",
                  (client["seller"], client["seller"]))
    assert int(events[0]["n"]) == 1, (
        f"тарифицируемых событий {events[0]['n']} на одну машину: "
        f"клиенту выставится счёт дважды")


def test_a_second_delivery_with_a_new_key_finds_nothing_to_ship(
        pool: ConnectionPool, client: dict) -> None:
    """Уехавшее задание больше не «готово уехать».

    Пока `shipped` входил в `ASSEMBLED_STATES`, повтор `deliver` с новым
    ключом находил те же задания собранными и отгружал их второй раз.
    """
    task_id = assembled_task(pool, client)
    shipments = ShipmentOperations(pool, WmsService(pool))
    opened = shipments.handle({"seller_external_id": client["seller"],
                               "idempotency_key": unique("open"), "action": "open"})
    shipments.handle({"seller_external_id": client["seller"],
                      "idempotency_key": unique("add"), "action": "add_orders",
                      "wb_supply_id": opened["wb_supply_id"], "task_ids": [task_id]})
    shipments.handle({"seller_external_id": client["seller"],
                      "idempotency_key": unique("deliver"), "action": "deliver",
                      "wb_supply_id": opened["wb_supply_id"]})

    with pytest.raises(ValueError, match="везти нечего"):
        shipments.handle({"seller_external_id": client["seller"],
                          "idempotency_key": unique("deliver-again"), "action": "deliver",
                          "wb_supply_id": opened["wb_supply_id"]})


def test_a_refused_command_does_not_burn_its_key(
        pool: ConnectionPool, client: dict) -> None:
    """Отказ — не выполненная команда: тем же ключом можно повторить.

    Иначе одна испорченная накладная навсегда занимала бы ключ, и исправленную
    команду пришлось бы слать под новым — то есть без идемпотентности вовсе.
    """
    shipments = ShipmentOperations(pool, WmsService(pool))
    opened = shipments.handle({"seller_external_id": client["seller"],
                               "idempotency_key": unique("open"), "action": "open"})
    key = unique("deliver")
    with pytest.raises(ValueError, match="везти нечего"):
        shipments.handle({"seller_external_id": client["seller"],
                          "idempotency_key": key, "action": "deliver",
                          "wb_supply_id": opened["wb_supply_id"]})

    task_id = assembled_task(pool, client)
    shipments.handle({"seller_external_id": client["seller"],
                      "idempotency_key": unique("add"), "action": "add_orders",
                      "wb_supply_id": opened["wb_supply_id"], "task_ids": [task_id]})
    delivered = shipments.handle({"seller_external_id": client["seller"],
                                  "idempotency_key": key, "action": "deliver",
                                  "wb_supply_id": opened["wb_supply_id"]})
    assert delivered["orders"] == 1, "ключ сгорел на отказе — повторить нечем"
    assert not delivered["duplicate"]


# ------------------------------------------- номер документа у своего клиента

def test_a_document_number_belongs_to_its_owner(pool: ConnectionPool) -> None:
    """«ТН-1» есть у каждого второго клиента, и это разные накладные.

    Поиск по всей таблице отдавал клиенту A приёмку клиента B вместе с чужими
    строками: изоляция владельца кончалась на номере накладной (инвариант 6).
    """
    first = make_client(pool, "Клиент А")
    second = make_client(pool, "Клиент Б")
    reference = unique("ТН")
    receiving = ReceivingOperations(pool)

    receiving.receive({
        "seller_external_id": first["seller"], "reference": reference,
        "lines": [{"barcode": first["barcode"], "expected_qty": 5, "actual_qty": 5,
                   "cell_address": unique("cell").upper(), "comment": "первая"}]})

    with pytest.raises(ValueError, match="занят другим владельцем"):
        receiving.receive({
            "seller_external_id": second["seller"], "reference": reference,
            "lines": [{"barcode": second["barcode"], "expected_qty": 7, "actual_qty": 7,
                       "cell_address": unique("cell").upper(), "comment": "вторая"}]})

    mine = rows(pool, "SELECT count(*) AS n FROM receipt r JOIN owner o ON o.id = r.owner_id "
                      " WHERE r.reference = %s AND o.seller_external_id = %s",
                (reference, second["seller"]))
    assert int(mine[0]["n"]) == 0, "чужой номер завёл приёмку не тому клиенту"


def test_the_same_document_number_in_two_cabinets_still_moves_both(
        pool: ConnectionPool) -> None:
    """Ключ движения включает владельца.

    Без него «ОТК-1» клиента A и «ОТК-1» клиента B — один ключ, и документ
    второго молча не применялся: его строки считались повтором чужих.
    """
    first = make_client(pool, "Клиент В")
    second = make_client(pool, "Клиент Г")
    reference = unique("ОТК")
    stock = StockOperations(pool)

    one = stock.apply_document({
        "seller_external_id": first["seller"], "reference": reference,
        "doc_type": "adjustment",
        "lines": [{"barcode": first["barcode"], "quantity": 3,
                   "cell_address": unique("cell").upper(), "state": "good"}]})
    two = stock.apply_document({
        "seller_external_id": second["seller"], "reference": reference,
        "doc_type": "adjustment",
        "lines": [{"barcode": second["barcode"], "quantity": 4,
                   "cell_address": unique("cell").upper(), "state": "good"}]})

    assert one["moves"] == 1
    assert two["moves"] == 1, (
        "документ второго клиента не применён: его строки приняли за повтор "
        "чужих, и товар не появился на складе")
