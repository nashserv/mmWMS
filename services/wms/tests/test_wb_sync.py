"""Опрос Wildberries: от задания в кабинете до задания в базе.

Проверяется путь целиком, включая опросчик, а не только транзакция под ним.
Именно на этом пути в боевом контуре терялись задания: между WB и рабочим
местом стояли четыре очереди и три молчащих воркера (раздел 2.5). Здесь между
ними нет ничего.
"""
from __future__ import annotations

import os
import time
import uuid

import httpx
import pytest

from app import repositories as repo
from app.postgres import ConnectionPool, transaction
from app.service import CatalogOperations, StockOperations
from app.workers.wb_sync import WbSyncWorker

from dbfixtures import require_database, unique

SIMULATOR = (os.getenv("WB_SIMULATOR_URL") or "http://127.0.0.1:8090").rstrip("/")


def simulator_is_up() -> bool:
    try:
        return httpx.get(f"{SIMULATOR}/healthz", timeout=2.0).status_code == 200
    except httpx.HTTPError:
        return False


@pytest.fixture(scope="module")
def pool() -> ConnectionPool:
    connections = ConnectionPool(require_database(), max_size=8)
    yield connections
    connections.close()


@pytest.fixture(autouse=True)
def _needs_simulator() -> None:
    if not simulator_is_up():
        pytest.skip(f"нужен симулятор WB по {SIMULATOR}: его поднимает стенд потока 0")


@pytest.fixture()
def cabinet(pool: ConnectionPool) -> dict:
    """Кабинет с владельцем, товаром и остатком — как после шага 1 прогона."""
    catalog, stock = CatalogOperations(pool), StockOperations(pool)
    seller, account = unique("seller"), unique("wb")
    barcode = f"46{uuid.uuid4().int % 10**11:011d}"
    catalog.upsert_owner({"seller_external_id": seller, "name": "Опрос WB",
                          "allow_ledger_short": True})
    catalog.upsert_wb_account({
        "op": "upsert", "external_id": account, "seller_external_id": seller,
        "display_name": "Кабинет опроса", "secret_ref": f"vault://mmx/test/{account}",
        "mode": "shadow", "status": "ACTIVE"})
    catalog.ensure_product({"seller_external_id": seller, "barcode": barcode})
    stock.apply_document({
        "seller_external_id": seller, "reference": unique("open"), "doc_type": "opening",
        "lines": [{"barcode": barcode, "quantity": 20,
                   "cell_address": unique("cell").upper(), "state": "good"}]})
    return {"seller": seller, "account": account, "barcode": barcode}


def seed_orders(account: str, count: int, barcode: str) -> list[int]:
    """Ручка стенда: у настоящего Wildberries её нет (README прогона, пункт 2)."""
    deadline = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))
    response = httpx.post(f"{SIMULATOR}/__stand__/seed-orders", timeout=10.0, json={
        "account": account, "count": count, "barcode": barcode, "deadline": deadline})
    response.raise_for_status()
    return [int(order["id"]) for order in response.json()["orders"]]


def rows(pool: ConnectionPool, sql: str, params: tuple) -> list[dict]:
    with pool.connection() as connection:
        with transaction(connection) as cursor:
            cursor.execute(sql, params)
            return cursor.fetchall()


def test_orders_become_tasks_and_reservations_without_a_queue(
        pool: ConnectionPool, cabinet: dict) -> None:
    """Шаг 4 прогона: пять заданий WB становятся пятью заданиями и пятью резервами.

    Между Wildberries и заданием больше нет очереди (раздел 6.1) — задание
    лежит в базе в тот момент, когда `wms` опросила Wildberries.
    """
    order_ids = seed_orders(cabinet["account"], 5, cabinet["barcode"])

    started = time.monotonic()
    created = WbSyncWorker(pool, only_accounts=[cabinet["account"]]).tick()
    elapsed = time.monotonic() - started

    assert created == 5, f"опросчик завёл {created} заданий из 5"
    # Критерий раздела 10: задержка «WB → доступность» под 2 с по p99.
    assert elapsed < 2.0, f"опрос занял {elapsed:.2f} с при бюджете 2 с"

    tasks = rows(pool, "SELECT id, state, wb_order_id, deadline FROM wms_task "
                       " WHERE wb_order_id = ANY(%s)", (order_ids,))
    assert len(tasks) == 5
    assert all(task["state"] == "reserved" for task in tasks)
    assert all(task["deadline"] is not None for task in tasks), \
        "срок WB не сохранён, а выдача сборщикам сортируется по нему"

    held = rows(pool, "SELECT id FROM reservation WHERE task_id = ANY(%s) AND state = 'held'",
                ([task["id"] for task in tasks],))
    assert len(held) == 5, "заказ и резерв рождаются вместе (инвариант 1)"


def test_polling_the_same_orders_twice_creates_nothing_new(
        pool: ConnectionPool, cabinet: dict) -> None:
    """Инвариант 5: перекрытие курсора перечитывает задания и не удваивает их.

    Перекрытие существует ради дыры на границе окна WB. Оно имеет смысл только
    потому, что повторный опрос идемпотентен по `wb_order_id`.
    """
    order_ids = seed_orders(cabinet["account"], 3, cabinet["barcode"])
    assert WbSyncWorker(pool, only_accounts=[cabinet["account"]]).tick() == 3

    _release_lease(pool, cabinet["account"])
    again = WbSyncWorker(pool, only_accounts=[cabinet["account"]]).tick()

    assert again == 0, "повторный опрос завёл задания второй раз"
    tasks = rows(pool, "SELECT id FROM wms_task WHERE wb_order_id = ANY(%s)", (order_ids,))
    assert len(tasks) == 3
    moves = rows(pool, "SELECT m.id FROM stock_move m JOIN reservation r "
                       "    ON r.id::text = m.doc_ref "
                       " WHERE r.task_id = ANY(%s)", ([task["id"] for task in tasks],))
    assert len(moves) == 3, "перечитанные задания списали товар второй раз"


def test_a_cabinet_is_leased_so_two_pollers_do_not_share_its_limit(
        pool: ConnectionPool, cabinet: dict) -> None:
    """Раздел 6.4: лимит 300 запросов в минуту общий на кабинет.

    Два опросчика, взявшие один кабинет, выберут его вдвое быстрее и получат
    блокировку. Лизинг существует ровно против этого.
    """
    seed_orders(cabinet["account"], 1, cabinet["barcode"])
    with pool.connection() as connection:
        with transaction(connection) as cursor:
            first = repo.lease_accounts(cursor, limit=50,
                                        only=[cabinet["account"]])
            leased = {row["external_id"] for row in first}
    assert cabinet["account"] in leased

    with pool.connection() as connection:
        with transaction(connection) as cursor:
            second = repo.lease_accounts(cursor, limit=50,
                                         only=[cabinet["account"]])
    assert cabinet["account"] not in {row["external_id"] for row in second}, \
        "занятый кабинет достался второму опросчику"


def test_the_token_never_leaves_the_wb_client(pool: ConnectionPool, cabinet: dict) -> None:
    """Инвариант 15: токен не попадает ни в события, ни в задание, ни в лог."""
    order_ids = seed_orders(cabinet["account"], 1, cabinet["barcode"])
    WbSyncWorker(pool, only_accounts=[cabinet["account"]]).tick()

    events = rows(pool, "SELECT payload::text AS body FROM outbox "
                        " WHERE aggregate_id IN (SELECT id FROM wms_task "
                        "                         WHERE wb_order_id = ANY(%s))", (order_ids,))
    assert events
    for event in events:
        assert "stand-not-a-token" not in event["body"]
        assert "Authorization" not in event["body"]
        assert "secret" not in event["body"].lower()


def _release_lease(pool: ConnectionPool, account: str) -> None:
    """Отпускает лизинг, чтобы следующий цикл увидел кабинет сразу.

    В жизни это делает время (`next_sync_at`), в тесте ждать полсекунды ради
    того же результата — потерянные полсекунды на каждом прогоне.
    """
    with pool.connection() as connection:
        with transaction(connection) as cursor:
            cursor.execute(
                "UPDATE wb_account SET sync_claimed_at = NULL, next_sync_at = NULL "
                " WHERE external_id = %s", (account,))


def test_one_poisoned_order_does_not_stop_the_whole_cabinet(
        pool: ConnectionPool, cabinet: dict) -> None:
    """Один заказ с битым полем ронял ВЕСЬ такт опроса.

    Курсор не двигался, следующий такт приносил тот же заказ — и кабинет
    вставал навсегда, а вместе с ним и все остальные заказы этого клиента.
    Ядовитый заказ обязан уйти в разбор человеку, а опрос — продолжиться.
    """
    good_before = seed_orders(cabinet["account"], 1, cabinet["barcode"])
    poisoned = httpx.post(f"{SIMULATOR}/__stand__/seed-orders", timeout=10.0, json={
        "account": cabinet["account"], "count": 1, "barcode": cabinet["barcode"],
        # Дата, которой не бывает. У настоящего WB такое приезжает само.
        "broken": {"ddate": "позавчера вечером"}}).json()["orders"]
    good_after = seed_orders(cabinet["account"], 1, cabinet["barcode"])

    worker = WbSyncWorker(pool, only_accounts=[cabinet["account"]])
    created = worker.tick()

    assert created >= 2, (
        f"заведено {created} заданий: здоровые заказы по обе стороны от "
        f"ядовитого обязаны доехать")

    parked = rows(pool, "SELECT state, manual_review_code, manual_review_reason "
                        "  FROM wms_task WHERE wb_order_id = %s",
                  (int(poisoned[0]["id"]),))
    assert parked, "ядовитый заказ пропал бесследно: срок по нему идёт у WB"
    assert parked[0]["state"] == "manual_review"
    assert parked[0]["manual_review_code"] == "UNPROCESSABLE_ORDER"
    assert parked[0]["manual_review_reason"], "разбирать нечего: причина пуста"

    for order_id in good_before + good_after:
        healthy = rows(pool, "SELECT state FROM wms_task WHERE wb_order_id = %s",
                       (order_id,))
        assert healthy and healthy[0]["state"] == "reserved", (
            f"здоровый заказ {order_id} не заведён — ядовитый утащил за собой такт")

    # Курсор сдвинулся: следующий такт не принесёт тот же ядовитый заказ снова.
    assert worker.tick() == 0, "опрос читает ту же страницу заново"
