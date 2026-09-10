"""Приёмка по факту и выдача заданий, которая не теряет и не двоит.

Два места, где боевой контур врал молча: приёмка подгоняла факт под ожидание
(отсюда 92 единицы на 21 продавца, раздел 3.1), а выдача заданий существовала
ровно в одной записи `workstation_pick_session` на 6497 заданий (раздел 3).
"""
from __future__ import annotations

import threading
import uuid

import pytest

from app import repositories as repo
from app.postgres import ConnectionPool, single
from app.receiving import ReceivingOperations
from app.service import CatalogOperations, StockOperations, WmsService
from app.tasks import TaskOperations

from dbfixtures import require_database, unique


@pytest.fixture(scope="module")
def pool() -> ConnectionPool:
    connections = ConnectionPool(require_database(), max_size=16)
    yield connections
    connections.close()


@pytest.fixture()
def client(pool: ConnectionPool) -> dict:
    catalog = CatalogOperations(pool)
    seller, account = unique("seller"), unique("wb")
    barcode = f"46{uuid.uuid4().int % 10**11:011d}"
    catalog.upsert_owner({"seller_external_id": seller, "name": "Приёмка"})
    catalog.upsert_wb_account({
        "op": "upsert", "external_id": account, "seller_external_id": seller,
        "display_name": "Кабинет приёмки", "secret_ref": f"vault://mmx/test/{account}",
        "mode": "shadow", "status": "ACTIVE"})
    catalog.ensure_product({"seller_external_id": seller, "barcode": barcode})
    return {"seller": seller, "account": account, "barcode": barcode,
            "cell": unique("cell").upper(), "box": unique("box").upper()}


def rows(pool: ConnectionPool, sql: str, params: tuple) -> list[dict]:
    with pool.connection() as connection:
        with single(connection) as cursor:
            cursor.execute(sql, params)
            return cursor.fetchall()


def balance(pool: ConnectionPool, seller: str, barcode: str, state: str = "good") -> int:
    return int(rows(pool,
                    "SELECT COALESCE(SUM(b.qty), 0) AS qty FROM stock_balance b "
                    "  JOIN owner o ON o.id = b.owner_id JOIN sku s ON s.id = b.sku_id "
                    " WHERE o.seller_external_id = %s AND s.barcode = %s AND b.state = %s",
                    (seller, barcode, state))[0]["qty"])


# ------------------------------------------------------------------ приёмка

def test_receipt_follows_the_count_not_the_expectation(
        pool: ConnectionPool, client: dict) -> None:
    """Шаг 3 прогона: баланс идёт по факту, разница становится расхождением.

    Подгонка факта под ожидание — та самая молчаливая ложь учёта, из-за которой
    на 21 продавца осталось 92 единицы.
    """
    receiving = ReceivingOperations(pool)
    before = balance(pool, client["seller"], client["barcode"])
    reference = unique("receipt")

    result = receiving.receive({
        "seller_external_id": client["seller"], "warehouse_code": "RUM",
        "reference": reference,
        "lines": [{"barcode": client["barcode"], "expected_qty": 10, "actual_qty": 7,
                   "box_barcode": client["box"], "cell_address": client["cell"],
                   "comment": "приёмка с недостачей"}]})

    assert result["state"] == "accepted"
    assert balance(pool, client["seller"], client["barcode"]) == before + 7, \
        "баланс поехал по ожиданию, а не по факту"

    assert len(result["discrepancies"]) == 1
    discrepancy = result["discrepancies"][0]
    assert discrepancy["kind"] == "shortage" and discrepancy["qty"] == 3
    assert discrepancy["cell_address"] == client["cell"], "расхождение без адреса"

    stored = rows(pool, "SELECT d.kind, d.qty, d.decision FROM discrepancy d "
                        "  JOIN receipt r ON r.id = d.receipt_id WHERE r.reference = %s",
                  (reference,))
    assert stored and stored[0]["kind"] == "shortage" and int(stored[0]["qty"]) == 3
    assert stored[0]["decision"] == "pending", "расхождение обязано ждать решения человека"


def test_surplus_is_recorded_too(pool: ConnectionPool, client: dict) -> None:
    """Излишек — такое же расхождение, как недостача, и тоже требует решения."""
    receiving = ReceivingOperations(pool)
    before = balance(pool, client["seller"], client["barcode"])
    result = receiving.receive({
        "seller_external_id": client["seller"], "reference": unique("surplus"),
        "lines": [{"barcode": client["barcode"], "expected_qty": 5, "actual_qty": 8,
                   "cell_address": client["cell"], "comment": "приехало больше"}]})
    assert balance(pool, client["seller"], client["barcode"]) == before + 8
    assert result["discrepancies"][0]["kind"] == "surplus"
    assert result["discrepancies"][0]["qty"] == 3


def test_every_box_carries_a_comment(pool: ConnectionPool, client: dict) -> None:
    """Раздел 2.9: через месяц стоят сотни одинаковых коробок."""
    receiving = ReceivingOperations(pool)
    receiving.receive({
        "seller_external_id": client["seller"], "reference": unique("boxes"),
        "lines": [{"barcode": client["barcode"], "expected_qty": 2, "actual_qty": 2,
                   "box_barcode": client["box"], "cell_address": client["cell"],
                   "comment": "вторая полка, синяя наклейка"}]})
    box = rows(pool, "SELECT comment, cell_id FROM box WHERE barcode = %s",
               (client["box"],))[0]
    assert box["comment"].strip() == "вторая полка, синяя наклейка"
    assert box["cell_id"], "коробка не поставлена в ячейку"

    with pytest.raises(ValueError, match="comment обязателен"):
        receiving.create_box({"box_barcode": unique("box"),
                              "seller_external_id": client["seller"], "comment": "   "})


def test_receipt_is_idempotent_by_reference(pool: ConnectionPool, client: dict) -> None:
    """Инвариант 5: повтор приёмки не приходует товар второй раз."""
    receiving = ReceivingOperations(pool)
    reference = unique("twice")
    line = {"barcode": client["barcode"], "expected_qty": 4, "actual_qty": 4,
            "cell_address": client["cell"], "comment": "повтор"}
    first = receiving.receive({"seller_external_id": client["seller"],
                               "reference": reference, "lines": [line]})
    after_first = balance(pool, client["seller"], client["barcode"])
    second = receiving.receive({"seller_external_id": client["seller"],
                                "reference": reference, "lines": [line]})

    assert first["duplicate"] is False and second["duplicate"] is True
    assert second["receipt_id"] == first["receipt_id"]
    assert balance(pool, client["seller"], client["barcode"]) == after_first


def test_a_seller_seen_for_the_first_time_is_created_by_the_receipt(
        pool: ConnectionPool) -> None:
    """Приложение C: товар уже приехал, машину под разгрузкой не держат."""
    receiving = ReceivingOperations(pool)
    seller = unique("newcomer")
    result = receiving.receive({
        "seller_external_id": seller, "seller_name": "ИП Новый", "seller_inn": "1234567890",
        "reference": unique("first"),
        "lines": [{"barcode": f"46{uuid.uuid4().int % 10**11:011d}",
                   "expected_qty": 1, "actual_qty": 1, "cell_address": unique("cell").upper(),
                   "comment": "первый приход нового клиента"}]})
    assert result["owner_created"] is True
    owner = rows(pool, "SELECT name, inn FROM owner WHERE seller_external_id = %s",
                 (seller,))[0]
    assert owner["name"] == "ИП Новый" and owner["inn"] == "1234567890"


def test_inventory_writes_off_and_takes_on_by_movement(
        pool: ConnectionPool, client: dict) -> None:
    """Инвентаризация двигает журнал, а не правит число (инвариант 3)."""
    receiving = ReceivingOperations(pool)
    receiving.receive({
        "seller_external_id": client["seller"], "reference": unique("before-count"),
        "lines": [{"barcode": client["barcode"], "expected_qty": 10, "actual_qty": 10,
                   "cell_address": client["cell"], "comment": "перед пересчётом"}]})
    before = balance(pool, client["seller"], client["barcode"])

    result = receiving.count({
        "seller_external_id": client["seller"], "reference": unique("count"),
        "scope": "partial",
        "lines": [{"barcode": client["barcode"], "cell_address": client["cell"],
                   "fact_qty": before - 3}]})

    assert result["state"] == "applied"
    # Форма — InventoryCountResult: наружу едет число движений, а разбор
    # расхождений читается по /discrepancies. Ответ команды — не способ
    # доставки данных (раздел 6.1).
    assert result["moves"] >= 1
    assert balance(pool, client["seller"], client["barcode"]) == before - 3
    moves = rows(pool, "SELECT doc_type, reason, qty FROM stock_move "
                       " WHERE doc_ref = %s", (result["reference"],))
    assert moves and moves[0]["doc_type"] == "inventory"


def test_a_box_with_stock_cannot_be_removed(pool: ConnectionPool, client: dict) -> None:
    """Убранная коробка с товаром — это остаток, которого никто не найдёт."""
    receiving = ReceivingOperations(pool)
    receiving.receive({
        "seller_external_id": client["seller"], "reference": unique("full-box"),
        "lines": [{"barcode": client["barcode"], "expected_qty": 3, "actual_qty": 3,
                   "box_barcode": client["box"], "cell_address": client["cell"],
                   "comment": "полная коробка"}]})
    with pytest.raises(ValueError, match="ещё лежит товар"):
        receiving.remove_box({"box_barcode": client["box"]})


# ---------------------------------------------------------- выдача заданий

def prepared_tasks(pool: ConnectionPool, client: dict, count: int) -> list[str]:
    """Готовит `count` зарезервированных заданий одного владельца."""
    StockOperations(pool).apply_document({
        "seller_external_id": client["seller"], "reference": unique("open"),
        "doc_type": "opening",
        "lines": [{"barcode": client["barcode"], "quantity": count * 2,
                   "cell_address": client["cell"], "state": "good"}]})
    wms = WmsService(pool)
    ids = []
    for index in range(count):
        outcome = wms.reserve({
            "idempotency_key": unique(f"idem-{index}"),
            "seller_external_id": client["seller"],
            "wb_account_external_id": client["account"],
            "wb_order_id": uuid.uuid4().int % 10**12, "sku": client["barcode"],
            "barcode": client["barcode"], "quantity": 1,
            "correlation_id": unique("corr")})
        assert outcome.status == "reserved", outcome.error_code
        ids.append(outcome.task_id)
    return ids


def test_five_pickers_never_get_the_same_task(pool: ConnectionPool, client: dict) -> None:
    """Шаг 8 прогона: ни одно задание не выдано двоим, ни одно не потеряно.

    Дубль означает, что двое пойдут за одной вещью; потеря — что не пойдёт
    никто. Пять сборщиков работают одновременно (раздел 4).
    """
    expected = set(prepared_tasks(pool, client, 10))
    taken: dict[str, list[str]] = {}
    errors: list[BaseException] = []
    barrier = threading.Barrier(5)
    lock = threading.Lock()

    def pull(name: str) -> None:
        try:
            operations = TaskOperations(pool, WmsService(pool))
            barrier.wait(timeout=30)
            result = operations.pull({"assignee": name, "limit": 10,
                                      "owner_external_ids": [client["seller"]]})
            with lock:
                taken[name] = [item["task"]["task_id"] for item in result["tasks"]]
        except BaseException as failure:      # noqa: BLE001
            with lock:
                errors.append(failure)

    threads = [threading.Thread(target=pull, args=(f"picker-{n}",)) for n in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not errors, f"выдача упала под конкуренцией: {errors[0]!r}"
    handed = [task_id for ids in taken.values() for task_id in ids]
    duplicates = {task_id for task_id in handed if handed.count(task_id) > 1}
    assert not duplicates, f"задания выданы двоим: {sorted(duplicates)}"
    assert expected <= set(handed), f"задания не выданы никому: {sorted(expected - set(handed))}"


def test_tasks_are_handed_out_by_wb_deadline(pool: ConnectionPool, client: dict) -> None:
    """Раздел 7: порядок по сроку WB, а не по времени создания.

    Просроченное задание дороже свежего — иначе сборщик уносит то, что подождёт,
    а горящее остаётся в очереди.
    """
    prepared_tasks(pool, client, 3)
    with pool.connection() as connection:
        with single(connection) as cursor:
            cursor.execute(
                "UPDATE wms_task t SET deadline = now() + (n || ' hours')::interval "
                "  FROM (SELECT id, row_number() OVER (ORDER BY created_at) AS n "
                "          FROM wms_task WHERE owner_id = "
                "               (SELECT id FROM owner WHERE seller_external_id = %s) "
                "           AND state = 'reserved' AND assignee IS NULL) src "
                " WHERE t.id = src.id", (client["seller"],))

    result = TaskOperations(pool, WmsService(pool)).pull({
        "assignee": "picker-deadline", "limit": 3,
        "owner_external_ids": [client["seller"]]})
    deadlines = [item["task"]["deadline"] for item in result["tasks"]]
    assert deadlines == sorted(deadlines), "выдача не отсортирована по сроку WB"


def test_a_lease_returns_the_task_when_the_picker_disappears(
        pool: ConnectionPool, client: dict) -> None:
    """Раздел 7: задание освобождается, если сборщик пропал.

    Без этого задание, выданное ушедшему со смены человеку, не потеряно только
    на бумаге: очередь его больше не видит.
    """
    task_ids = prepared_tasks(pool, client, 1)
    operations = TaskOperations(pool, WmsService(pool))
    first = operations.pull({"assignee": "picker-gone", "limit": 1, "lease_seconds": 30,
                             "owner_external_ids": [client["seller"]]})
    assert [item["task"]["task_id"] for item in first["tasks"]] == task_ids

    # Сборщик не вернулся: лизинг истёк.
    with pool.connection() as connection:
        with single(connection) as cursor:
            cursor.execute("UPDATE wms_task SET claim_expires_at = now() - interval '1 minute' "
                           " WHERE id = %s", (task_ids[0],))

    second = operations.pull({"assignee": "picker-next", "limit": 1,
                              "owner_external_ids": [client["seller"]]})
    assert [item["task"]["task_id"] for item in second["tasks"]] == task_ids, \
        "задание не вернулось в очередь после истёкшего лизинга"


def test_looking_does_not_take(pool: ConnectionPool, client: dict) -> None:
    """`claim: false` — только посмотреть.

    Экран обновляется чаще, чем человек берёт работу, и занимать задание при
    каждом обновлении нельзя.
    """
    task_ids = prepared_tasks(pool, client, 2)
    operations = TaskOperations(pool, WmsService(pool))
    looked = operations.pull({"assignee": "screen", "limit": 5, "claim": False,
                              "owner_external_ids": [client["seller"]]})
    assert {item["task"]["task_id"] for item in looked["tasks"]} == set(task_ids)
    assert all(item["leased_until"] is None for item in looked["tasks"])

    still_free = rows(pool, "SELECT assignee FROM wms_task WHERE id = ANY(%s)", (task_ids,))
    assert all(row["assignee"] is None for row in still_free), "просмотр занял задания"


def test_a_pulled_task_carries_the_address_to_walk_to(
        pool: ConnectionPool, client: dict) -> None:
    """Сборщик должен видеть, куда идти, а не искать глазами."""
    prepared_tasks(pool, client, 1)
    result = TaskOperations(pool, WmsService(pool)).pull({
        "assignee": "picker-address", "limit": 1,
        "owner_external_ids": [client["seller"]]})
    placements = result["tasks"][0].get("placements")
    assert placements, "задание пришло без адреса"
    assert any(row["cell_address"] == client["cell"] for row in placements)


def test_a_foreign_barcode_is_rejected_and_remembered(
        pool: ConnectionPool, client: dict) -> None:
    """Раздел 4: контрольный скан — главный рубеж качества.

    Отклонённый скан обязан сохраниться: по нему видно, что именно человек
    взял не то.
    """
    task_id = prepared_tasks(pool, client, 1)[0]
    operations = TaskOperations(pool, WmsService(pool))
    result = operations.scan(task_id, {"barcode": "4600000000000"})

    assert result["scan_result"] == "wrong_barcode"
    assert result["status"] != "picked"
    line = rows(pool, "SELECT scan_result, scanned_at FROM pick_line WHERE task_id = %s",
                (task_id,))
    assert line and line[0]["scan_result"] == "wrong_barcode"
    assert line[0]["scanned_at"], "отклонённый скан записан без времени"

    good = operations.scan(task_id, {"barcode": client["barcode"]})
    assert good["status"] == "picked" and good["scan_result"] == "ok"


def test_cancel_after_the_label_unwinds_everything(
        pool: ConnectionPool, client: dict) -> None:
    """Шаг 13 прогона: резерв снят, товар вернулся, стикер недействителен.

    В боевом контуре у всех 2645 отмен причина пуста — отмена без причины
    неразбираема (инвариант 11).
    """
    task_id = prepared_tasks(pool, client, 1)[0]
    with pool.connection() as connection:
        with single(connection) as cursor:
            repo.save_label(cursor, task_id=uuid.UUID(task_id), payload=b"^XA^XZ",
                            checksum="0" * 64, label_format="zplv")
    good_before = balance(pool, client["seller"], client["barcode"])

    result = TaskOperations(pool, WmsService(pool)).cancel(
        task_id, {"cancellation_event_id": unique("cancel"), "handed_over": False})

    assert result["state"] == "cancelled"
    assert result["cancel_reason"].strip(), "отмена без причины (инвариант 11)"

    task = rows(pool, "SELECT state, cancel_reason, supply_id FROM wms_task WHERE id = %s",
                (task_id,))[0]
    assert task["supply_id"] is None, "заказ не освобождён из поставки"
    reservation = rows(pool, "SELECT state, release_reason FROM reservation "
                             " WHERE task_id = %s", (task_id,))[0]
    assert reservation["state"] == "released"
    assert reservation["release_reason"], "снятый резерв не объяснил, почему снят"
    assert balance(pool, client["seller"], client["barcode"]) == good_before + 1, \
        "товар не вернулся на полку"
    label = rows(pool, "SELECT invalidated_at FROM wb_label WHERE task_id = %s", (task_id,))[0]
    assert label["invalidated_at"], "стикер отменённого задания остался действительным"


def test_cancelling_twice_changes_nothing(pool: ConnectionPool, client: dict) -> None:
    """Инвариант 5: повтор отмены не возвращает товар на полку дважды."""
    task_id = prepared_tasks(pool, client, 1)[0]
    operations = TaskOperations(pool, WmsService(pool))
    event = unique("cancel-twice")
    operations.cancel(task_id, {"cancellation_event_id": event, "handed_over": False})
    after_first = balance(pool, client["seller"], client["barcode"])
    again = operations.cancel(task_id, {"cancellation_event_id": event, "handed_over": False})

    assert again["duplicate"] is True
    assert balance(pool, client["seller"], client["barcode"]) == after_first
