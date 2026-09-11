"""Поставка, этикетка, возврат — остаток контрактной поверхности потока A.

Главное здесь одно различие, из-за которого в боевом контуре подтверждённых
передач меньше одного процента: «мы отдали» и «они взяли» — разные факты
(раздел 2.12). Первый ставит человек, второй — сверка.
"""
from __future__ import annotations

import base64
import time
import uuid

import pytest

from app import repositories as repo
from app.labels import LabelOperations
from app.postgres import ConnectionPool, single
from app.returns import ReturnOperations
from app.service import CatalogOperations, StockOperations, WmsService
from app.shipments import ShipmentOperations
from app.tasks import TaskOperations

from dbfixtures import require_database, unique


@pytest.fixture(scope="module")
def pool() -> ConnectionPool:
    connections = ConnectionPool(require_database(), max_size=8)
    yield connections
    connections.close()


@pytest.fixture()
def client(pool: ConnectionPool) -> dict:
    catalog = CatalogOperations(pool)
    seller, account = unique("seller"), unique("wb")
    barcode = f"46{uuid.uuid4().int % 10**11:011d}"
    catalog.upsert_owner({"seller_external_id": seller, "name": "Поставки"})
    catalog.upsert_wb_account({
        "op": "upsert", "external_id": account, "seller_external_id": seller,
        "display_name": "Кабинет поставок", "secret_ref": f"vault://mmx/test/{account}",
        "mode": "live", "status": "ACTIVE"})
    catalog.ensure_product({"seller_external_id": seller, "barcode": barcode})
    StockOperations(pool).apply_document({
        "seller_external_id": seller, "reference": unique("open"), "doc_type": "opening",
        "lines": [{"barcode": barcode, "quantity": 20,
                   "cell_address": unique("cell").upper(), "state": "good"}]})
    return {"seller": seller, "account": account, "barcode": barcode}


def rows(pool: ConnectionPool, sql: str, params: tuple) -> list[dict]:
    with pool.connection() as connection:
        with single(connection) as cursor:
            cursor.execute(sql, params)
            return cursor.fetchall()


def reserve(pool: ConnectionPool, client: dict, count: int = 1) -> list[str]:
    wms = WmsService(pool)
    ids = []
    for _ in range(count):
        outcome = wms.reserve({
            "idempotency_key": unique("idem"), "seller_external_id": client["seller"],
            "wb_account_external_id": client["account"],
            "wb_order_id": uuid.uuid4().int % 10**12, "sku": client["barcode"],
            "barcode": client["barcode"], "quantity": 1, "correlation_id": unique("corr")})
        assert outcome.status == "reserved", outcome.error_code
        ids.append(outcome.task_id)
    return ids


def give_label(pool: ConnectionPool, task_id: str) -> None:
    with pool.connection() as connection:
        with single(connection) as cursor:
            repo.save_label(cursor, task_id=uuid.UUID(task_id), payload=b"^XA^FDtest^FS^XZ",
                            checksum="a" * 64, label_format="zplv")


def pack(pool: ConnectionPool, client: dict, task_id: str) -> None:
    operations = TaskOperations(pool, WmsService(pool))
    operations.scan(task_id, {"barcode": client["barcode"]})
    operations.pack(task_id, {"idempotency_key": unique("pack"),
                              "control_scan_barcode": client["barcode"]})


# ----------------------------------------------------------------- поставки

def test_a_supply_ships_what_was_assembled_not_what_waits_for_a_sticker(
        pool: ConnectionPool, client: dict) -> None:
    """`orders` — число уехавших заданий, и на нём стоит счёт клиенту.

    Накопительная поставка держит и задания, которым стикер уже вытянут, но
    которые лежат на полке: стикер выдаётся только заданию в поставке
    (раздел 6.6). Везти их нельзя — их никто не собирал.
    """
    task_ids = reserve(pool, client, 3)
    for task_id in task_ids:
        give_label(pool, task_id)
    pack(pool, client, task_ids[0])          # собрано только одно

    shipments = ShipmentOperations(pool, WmsService(pool))
    opened = shipments.handle({"seller_external_id": client["seller"],
                               "idempotency_key": unique("open"), "action": "open"})
    assert opened["state"] == "open"

    shipments.handle({"seller_external_id": client["seller"],
                      "idempotency_key": unique("add"), "action": "add_orders",
                      "wb_supply_id": opened["wb_supply_id"], "task_ids": task_ids})
    delivered = shipments.handle({"seller_external_id": client["seller"],
                                  "idempotency_key": unique("deliver"), "action": "deliver",
                                  "wb_supply_id": opened["wb_supply_id"]})

    assert delivered["orders"] == 1, (
        f"уехало {delivered['orders']} заданий при одном собранном — "
        f"счёт клиенту выставится по этому числу")

    shipped = rows(pool, "SELECT state, supply_id FROM wms_task WHERE id = ANY(%s)",
                   (task_ids,))
    assert sum(1 for row in shipped if row["state"] == "shipped") == 1
    left = [row for row in shipped if row["state"] != "shipped"]
    assert all(row["supply_id"] is None for row in left), \
        "несобранное осталось в поставке — WB ждёт его в машине, а его там нет"


def balance_by_state(pool: ConnectionPool, seller: str) -> dict[str, int]:
    """Остаток владельца по состояниям. `stock_balance` — проекция (инвариант 1)."""
    return {row["state"]: int(row["qty"]) for row in rows(
        pool, "SELECT b.state, sum(b.qty) AS qty FROM stock_balance b "
              "  JOIN owner o ON o.id = b.owner_id "
              " WHERE o.seller_external_id = %s GROUP BY b.state", (seller,))}


def test_delivery_writes_the_goods_out_of_the_warehouse(
        pool: ConnectionPool, client: dict) -> None:
    """Инвариант 3: товар уехал — журнал обязан это увидеть.

    `deliver` не писал ни одного движения. Задание закрывалось в `shipped`, а
    резерв оставался `held` навсегда: в остатке вечно висел `reserved` под
    заказ, который уже уехал, а `available` считался по складу, половины
    которого физически нет. Состояние `consumed` стояло в схеме с первого дня
    и не использовалось нигде.
    """
    task_ids = reserve(pool, client, 2)
    for task_id in task_ids:
        give_label(pool, task_id)
        pack(pool, client, task_id)

    before = balance_by_state(pool, client["seller"])
    assert before.get("reserved") == 2, "резерв не встал — отгружать нечего"

    shipments = ShipmentOperations(pool, WmsService(pool))
    opened = shipments.handle({"seller_external_id": client["seller"],
                               "idempotency_key": unique("open"), "action": "open"})
    shipments.handle({"seller_external_id": client["seller"],
                      "idempotency_key": unique("add"), "action": "add_orders",
                      "wb_supply_id": opened["wb_supply_id"], "task_ids": task_ids})
    shipments.handle({"seller_external_id": client["seller"],
                      "idempotency_key": unique("deliver"), "action": "deliver",
                      "wb_supply_id": opened["wb_supply_id"]})

    after = balance_by_state(pool, client["seller"])
    assert after.get("reserved", 0) == 0, (
        f"после отгрузки в резерве осталось {after.get('reserved')}: "
        f"товар уехал, а склад про него ещё помнит")
    assert after.get("good", 0) == before.get("good", 0), \
        "отгрузка тронула good — уезжать должен зарезервированный товар"

    reservations = rows(pool, "SELECT state FROM reservation WHERE task_id = ANY(%s)",
                        ([uuid.UUID(task_id) for task_id in task_ids],))
    assert reservations and all(row["state"] == "consumed" for row in reservations), (
        f"состояния резервов {[row['state'] for row in reservations]}: "
        f"израсходованный резерв — не снятый, товар не вернулся на полку")

    moves = rows(pool, "SELECT m.state_from, m.state_to, m.cell_to, m.qty, m.doc_ref "
                       "  FROM stock_move m JOIN owner o ON o.id = m.owner_id "
                       " WHERE o.seller_external_id = %s AND m.doc_type = 'shipment'",
                 (client["seller"],))
    assert len(moves) == 2, f"движений выхода {len(moves)} при двух уехавших заданиях"
    for move in moves:
        assert move["state_from"] == "reserved", "уехал не зарезервированный товар"
        assert move["state_to"] is None and move["cell_to"] is None, \
            "у движения выхода есть сторона `to`: товар остался на складе"
        assert move["doc_ref"] == opened["wb_supply_id"], "движение не привязано к поставке"


def test_the_shipped_event_carries_the_billable_count(
        pool: ConnectionPool, client: dict) -> None:
    """Приложение E: `wb.supply.shipped.v1` — тарифицируемое событие."""
    task_id = reserve(pool, client, 1)[0]
    give_label(pool, task_id)
    pack(pool, client, task_id)

    shipments = ShipmentOperations(pool, WmsService(pool))
    opened = shipments.handle({"seller_external_id": client["seller"],
                               "idempotency_key": unique("open"), "action": "open"})
    shipments.handle({"seller_external_id": client["seller"],
                      "idempotency_key": unique("add"), "action": "add_orders",
                      "wb_supply_id": opened["wb_supply_id"], "task_ids": [task_id]})
    shipments.handle({"seller_external_id": client["seller"],
                      "idempotency_key": unique("deliver"), "action": "deliver",
                      "wb_supply_id": opened["wb_supply_id"]})

    event = rows(pool, "SELECT payload FROM outbox WHERE type = 'wb.supply.shipped.v1' "
                       "   AND payload->>'seller_id' = %s ORDER BY occurred_at DESC LIMIT 1",
                 (client["seller"],))
    assert event, "событие отгрузки не записано в outbox"
    payload = event[0]["payload"]
    assert payload["orders"] == 1
    assert payload["seller_id"] == client["seller"]
    assert payload["accepted_at"] is None, (
        "приёмка WB не подтверждена: `complete` её не доказывает (раздел 2.12)")
    for field in ("supply", "seller_id", "orders", "accepted_at", "name"):
        assert field in payload


def test_handover_needs_a_human_and_acceptance_needs_reconciliation(
        pool: ConnectionPool, client: dict) -> None:
    """Раздел 2.12: «мы отдали» и «они взяли» — разные факты.

    Сегодня подтверждённых передач меньше одного процента отгруженных
    (раздел 10). Схема не даёт поставить `handed_to_wb` без подписи.
    """
    task_id = reserve(pool, client, 1)[0]
    give_label(pool, task_id)
    pack(pool, client, task_id)
    shipments = ShipmentOperations(pool, WmsService(pool))
    opened = shipments.handle({"seller_external_id": client["seller"],
                               "idempotency_key": unique("open"), "action": "open"})
    shipments.handle({"seller_external_id": client["seller"],
                      "idempotency_key": unique("add"), "action": "add_orders",
                      "wb_supply_id": opened["wb_supply_id"], "task_ids": [task_id]})
    shipments.handle({"seller_external_id": client["seller"],
                      "idempotency_key": unique("deliver"), "action": "deliver",
                      "wb_supply_id": opened["wb_supply_id"]})

    unsigned = shipments.handle({"seller_external_id": client["seller"],
                                 "idempotency_key": unique("nobody"), "action": "hand_over",
                                 "wb_supply_id": opened["wb_supply_id"]})
    assert unsigned["state"] != "handed_to_wb", "передача принята без подписи человека"

    handed = shipments.handle({"seller_external_id": client["seller"],
                               "idempotency_key": unique("hand"), "action": "hand_over",
                               "wb_supply_id": opened["wb_supply_id"],
                               "handed_over_by": "кладовщик Пётр"})
    assert handed["state"] == "handed_to_wb"
    assert handed["handed_by"], "не сохранён тот, кто подтвердил передачу"

    # Приёмку подтверждает Wildberries, а не мы сами себе. Заказов этой
    # поставки у WB нет — значит `accepted` не встаёт, и это правильный отказ:
    # в боевом контуре 6072 задания оказались в терминальном успехе именно
    # потому, что команда ставила его, ничего не спросив.
    with pytest.raises(ValueError, match="не подтвердил приёмку"):
        shipments.handle({"seller_external_id": client["seller"],
                          "idempotency_key": unique("recon"), "action": "reconcile",
                          "wb_supply_id": opened["wb_supply_id"]})

    still = rows(pool, "SELECT state FROM wms_task WHERE id = %s", (uuid.UUID(task_id),))[0]
    assert still["state"] == "handed", (
        f"состояние {still['state']}: неподтверждённая приёмка изменила задание")


def test_handover_is_refused_before_the_supply_has_left(
        pool: ConnectionPool, client: dict) -> None:
    """Подписать передачу можно только у того, что уехало.

    Раньше `hand_over` работал по открытой поставке: человек подтверждал
    передачу машины, которую ещё не собрали, и `handed` вставал у заданий,
    лежащих на полке.
    """
    task_id = reserve(pool, client, 1)[0]
    give_label(pool, task_id)
    pack(pool, client, task_id)
    shipments = ShipmentOperations(pool, WmsService(pool))
    opened = shipments.handle({"seller_external_id": client["seller"],
                               "idempotency_key": unique("open"), "action": "open"})
    shipments.handle({"seller_external_id": client["seller"],
                      "idempotency_key": unique("add"), "action": "add_orders",
                      "wb_supply_id": opened["wb_supply_id"], "task_ids": [task_id]})

    with pytest.raises(ValueError, match="только после deliver"):
        shipments.handle({"seller_external_id": client["seller"],
                          "idempotency_key": unique("early"), "action": "hand_over",
                          "wb_supply_id": opened["wb_supply_id"],
                          "handed_over_by": "кладовщик Пётр"})

    task = rows(pool, "SELECT state FROM wms_task WHERE id = %s",
                (uuid.UUID(task_id),))[0]
    assert task["state"] == "packed", (
        f"состояние {task['state']}: задание лежит на полке, а числится переданным")


def test_delivering_an_empty_supply_is_refused(pool: ConnectionPool, client: dict) -> None:
    """Везти нечего — значит, и передавать нечего."""
    shipments = ShipmentOperations(pool, WmsService(pool))
    opened = shipments.handle({"seller_external_id": client["seller"],
                               "idempotency_key": unique("open"), "action": "open"})
    with pytest.raises(ValueError, match="везти нечего"):
        shipments.handle({"seller_external_id": client["seller"],
                          "idempotency_key": unique("deliver"), "action": "deliver",
                          "wb_supply_id": opened["wb_supply_id"]})


def test_a_task_without_a_sticker_blocks_delivery(
        pool: ConnectionPool, client: dict) -> None:
    """Приложение D: с релизов 2026-03/04 перед deliver обязательна metaDetails."""
    task_id = reserve(pool, client, 1)[0]
    pack(pool, client, task_id)              # упаковали, но стикера нет
    shipments = ShipmentOperations(pool, WmsService(pool))
    opened = shipments.handle({"seller_external_id": client["seller"],
                               "idempotency_key": unique("open"), "action": "open"})
    shipments.handle({"seller_external_id": client["seller"],
                      "idempotency_key": unique("add"), "action": "add_orders",
                      "wb_supply_id": opened["wb_supply_id"], "task_ids": [task_id]})
    with pytest.raises(ValueError, match="metaDetails"):
        shipments.handle({"seller_external_id": client["seller"],
                          "idempotency_key": unique("deliver"), "action": "deliver",
                          "wb_supply_id": opened["wb_supply_id"]})


# ---------------------------------------------------------------- этикетка

def test_the_label_comes_from_local_storage(pool: ConnectionPool, client: dict) -> None:
    """Инвариант 9: вызов в Wildberries в момент упаковки запрещён."""
    task_id = reserve(pool, client, 1)[0]
    give_label(pool, task_id)
    labels = LabelOperations(pool, WmsService(pool))

    body = labels.read(task_id, {})
    assert base64.b64decode(body["payload"]) == b"^XA^FDtest^FS^XZ"
    assert body["content_type"] == "application/x-zpl", (
        "тип содержимого не ZPL: принтер печатает его нативно, без растеризации")
    task = rows(pool, "SELECT wb_order_id FROM wms_task WHERE id = %s", (task_id,))[0]
    assert body["order_id"] == int(task["wb_order_id"]), (
        "order_id не совпал с заданием: напечатанный чужой стикер отправит "
        "вещь другому покупателю")


def test_printing_hands_ready_bytes_to_the_station(
        pool: ConnectionPool, client: dict) -> None:
    """Раздел 6.6: агент пишет байты в устройство как есть, без растеризации."""
    task_id = reserve(pool, client, 1)[0]
    give_label(pool, task_id)
    station_id = uuid.uuid4()
    with pool.connection() as connection:
        with single(connection) as cursor:
            cursor.execute(
                "INSERT INTO station (id, name, transport) VALUES (%s, %s, 'agent')",
                (station_id, unique("station")))

    result = LabelOperations(pool, WmsService(pool)).print(
        task_id, {"station_id": str(station_id), "idempotency_key": unique("print")})

    assert result["printer_transport"] == "agent", "принтеры USB — печать через агента"
    assert base64.b64decode(result["payload"]).startswith(b"^XA")
    assert result["format"] == "zplv"
    state = rows(pool, "SELECT state FROM wms_task WHERE id = %s", (task_id,))[0]
    assert state["state"] == "reserved", (
        f"состояние стало {state['state']}: печать — не подбор. Стикер лежит "
        f"локально с момента резерва (инвариант 9), и печатать его можно "
        f"когда угодно; собранным задание делает скан и упаковка")

    # Половина пути, за которую отвечает поток A: достать локальный ZPL.
    # Раздел 6.6 отводит на неё 1–3 мс из общего бюджета в 300 мс; шаг 10
    # прогона требует уложить весь путь до устройства в 50 мс, и если здесь
    # уйдёт хотя бы десяток, агенту с принтером не останется ничего.
    worst = 0.0
    for _attempt in range(10):
        started = time.perf_counter()
        LabelOperations(pool, WmsService(pool)).print(
            task_id, {"station_id": str(station_id), "idempotency_key": unique("print"),
                      "reprint": True, "reason": "замер задержки"})
        worst = max(worst, (time.perf_counter() - started) * 1000)
    assert worst < 25.0, (
        f"стикер отдаётся за {worst:.1f} мс — на агента и принтер не остаётся ничего")


def test_printing_a_packed_task_is_what_makes_it_labeled(
        pool: ConnectionPool, client: dict) -> None:
    """`labeled` ставит печать, и только у собранного задания.

    Парная проверка к предыдущей: «печать не меняет состояние» легко написать
    так, что она не меняет его никогда, и `labeled` исчезнет из автомата.
    """
    task_id = reserve(pool, client, 1)[0]
    give_label(pool, task_id)
    pack(pool, client, task_id)
    station_id = uuid.uuid4()
    with pool.connection() as connection:
        with single(connection) as cursor:
            cursor.execute(
                "INSERT INTO station (id, name, transport) VALUES (%s, %s, 'agent')",
                (station_id, unique("station")))

    LabelOperations(pool, WmsService(pool)).print(
        task_id, {"station_id": str(station_id), "idempotency_key": unique("print")})

    state = rows(pool, "SELECT state FROM wms_task WHERE id = %s", (task_id,))[0]
    assert state["state"] == "labeled"


def test_the_attached_event_is_emitted_once_however_many_times_it_is_printed(
        pool: ConnectionPool, client: dict) -> None:
    """Наклейка одна, печатей сколько угодно.

    `wms.label.attached.v1` уходило при КАЖДОЙ печати: пять перепечаток из-за
    зажёванной ленты давали пять наклеек в отчёте потребителя события.
    """
    task_id = reserve(pool, client, 1)[0]
    give_label(pool, task_id)
    station_id = uuid.uuid4()
    with pool.connection() as connection:
        with single(connection) as cursor:
            cursor.execute(
                "INSERT INTO station (id, name, transport) VALUES (%s, %s, 'agent')",
                (station_id, unique("station")))

    labels = LabelOperations(pool, WmsService(pool))
    first = labels.print(task_id, {"station_id": str(station_id),
                                   "idempotency_key": unique("print")})
    assert not first["duplicate"]
    for _ in range(4):
        again = labels.print(task_id, {"station_id": str(station_id),
                                       "idempotency_key": unique("print"),
                                       "reprint": True, "reason": "лента зажевало"})
        assert again["duplicate"], "перепечатка выдаёт себя за первую печать"

    events = rows(pool, "SELECT count(*) AS n FROM outbox "
                        " WHERE aggregate_id = %s AND type = %s",
                  (uuid.UUID(task_id), "wms.label.attached.v1"))
    assert int(events[0]["n"]) == 1, (
        f"событий «этикетка наклеена» {events[0]['n']} на пять печатей: "
        f"потребитель посчитает по ним пять наклеек вместо одной")

    label = rows(pool, "SELECT prints, printed_at FROM wb_label WHERE task_id = %s",
                 (task_id,))[0]
    assert int(label["prints"]) == 5, "счётчик печатей не ведётся — нечем мерить перепечатки"
    assert label["printed_at"], "не записано, когда стикер напечатали впервые"


def test_a_reprint_must_say_why(pool: ConnectionPool, client: dict) -> None:
    """Доля перепечаток — метрика качества этикетки и принтера."""
    task_id = reserve(pool, client, 1)[0]
    give_label(pool, task_id)
    with pytest.raises(ValueError, match="reason обязателен"):
        LabelOperations(pool, WmsService(pool)).print(
            task_id, {"station_id": str(uuid.uuid4()),
                      "idempotency_key": unique("print"), "reprint": True})


def test_an_invalidated_label_is_not_given_out(pool: ConnectionPool, client: dict) -> None:
    """Стикер отменённого задания печатать нельзя (раздел 6.6)."""
    task_id = reserve(pool, client, 1)[0]
    give_label(pool, task_id)
    TaskOperations(pool, WmsService(pool)).cancel(
        task_id, {"cancellation_event_id": unique("cancel"), "handed_over": False})
    with pytest.raises(ValueError, match="недействительным"):
        LabelOperations(pool, WmsService(pool)).read(task_id, {})


# ---------------------------------------------------------------- возвраты

def test_a_return_is_expected_then_received_then_decided(
        pool: ConnectionPool, client: dict) -> None:
    """Раздел 6.7: возвраты минимально — принять и решить, без нового процесса."""
    task_id = reserve(pool, client, 1)[0]
    returns = ReturnOperations(pool, WmsService(pool))
    event_id = unique("ret")

    expected = returns.expect(task_id, {"return_event_id": event_id,
                                        "seller_external_id": client["seller"],
                                        "reason": "покупатель отказался"})
    assert expected["return_id"] and expected["duplicate"] is False
    again = returns.expect(task_id, {"return_event_id": event_id,
                                     "seller_external_id": client["seller"]})
    assert again["duplicate"] is True, "возврат заведён второй раз по тому же событию"

    received = returns.receive(expected["return_id"], {"barcode": client["barcode"]})
    assert received["state"] == "received"

    good_before = int(rows(pool,
                           "SELECT COALESCE(SUM(b.qty),0) AS q FROM stock_balance b "
                           "  JOIN owner o ON o.id=b.owner_id JOIN sku s ON s.id=b.sku_id "
                           " WHERE o.seller_external_id=%s AND s.barcode=%s AND b.state='good'",
                           (client["seller"], client["barcode"]))[0]["q"])
    decided = returns.decide(expected["return_id"], {
        "decision": "resellable", "barcode": client["barcode"], "quantity": 1})
    assert decided["state"] == "decided" and decided["decision"] == "resellable"
    good_after = int(rows(pool,
                          "SELECT COALESCE(SUM(b.qty),0) AS q FROM stock_balance b "
                          "  JOIN owner o ON o.id=b.owner_id JOIN sku s ON s.id=b.sku_id "
                          " WHERE o.seller_external_id=%s AND s.barcode=%s AND b.state='good'",
                          (client["seller"], client["barcode"]))[0]["q"])
    assert good_after == good_before + 1, "годный возврат не вернулся в оборот"


def test_a_defective_return_does_not_become_sellable_stock(
        pool: ConnectionPool, client: dict) -> None:
    """Брак остаётся остатком владельца, но продать его нельзя."""
    task_id = reserve(pool, client, 1)[0]
    returns = ReturnOperations(pool, WmsService(pool))
    created = returns.expect(task_id, {"return_event_id": unique("ret"),
                                       "seller_external_id": client["seller"]})
    returns.receive(created["return_id"], {})
    returns.decide(created["return_id"], {"decision": "defective",
                                          "barcode": client["barcode"], "quantity": 1})

    defect = rows(pool, "SELECT COALESCE(SUM(b.qty),0) AS q FROM stock_balance b "
                        "  JOIN owner o ON o.id=b.owner_id JOIN sku s ON s.id=b.sku_id "
                        " WHERE o.seller_external_id=%s AND s.barcode=%s AND b.state='defect'",
                  (client["seller"], client["barcode"]))
    assert int(defect[0]["q"]) >= 1, "брак не попал в состояние defect"
