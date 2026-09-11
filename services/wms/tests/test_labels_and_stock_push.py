"""Стикер лежит до печати, а остаток уезжает сразу после движения.

Два обещания раздела 6, которые сегодня не выполняются вовсе: этикетка
запрашивается в момент упаковки (37 % стикеров печатается через систему,
раздел 3.3), а остаток в Wildberries публикует отдельный сервис по своему
расписанию.
"""
from __future__ import annotations

import hashlib
import os
import time
import uuid

import httpx
import pytest

from app import repositories as repo
from app.postgres import ConnectionPool, single, transaction
from app.service import CatalogOperations, StockOperations, WmsService
from app.stock_push import StockPublisher
from app.workers.wb_labels import WbLabelWorker
from app.workers.wb_sync import WbSyncWorker

from dbfixtures import require_database, unique

SIMULATOR = (os.getenv("WB_SIMULATOR_URL") or "http://127.0.0.1:8090").rstrip("/")


@pytest.fixture(scope="module")
def pool() -> ConnectionPool:
    connections = ConnectionPool(require_database(), max_size=8)
    yield connections
    connections.close()


@pytest.fixture(autouse=True)
def _needs_simulator() -> None:
    try:
        up = httpx.get(f"{SIMULATOR}/healthz", timeout=2.0).status_code == 200
    except httpx.HTTPError:
        up = False
    if not up:
        pytest.skip(f"нужен симулятор WB по {SIMULATOR}: его поднимает стенд потока 0")


@pytest.fixture()
def cabinet(pool: ConnectionPool) -> dict:
    catalog, stock = CatalogOperations(pool), StockOperations(pool)
    seller, account = unique("seller"), unique("wb")
    barcode = f"46{uuid.uuid4().int % 10**11:011d}"
    catalog.upsert_owner({"seller_external_id": seller, "name": "Стикеры и остаток"})
    catalog.upsert_wb_account({
        "op": "upsert", "external_id": account, "seller_external_id": seller,
        "display_name": "Кабинет стикеров", "secret_ref": f"vault://mmx/test/{account}",
        # Склад обязателен: без него остаток не публикуется вовсе, а не
        # уезжает на чужой склад №1 (инвариант 7).
        "mode": "live", "status": "ACTIVE", "wb_warehouse_id": 1})
    catalog.ensure_product({"seller_external_id": seller, "barcode": barcode})
    stock.apply_document({
        "seller_external_id": seller, "reference": unique("open"), "doc_type": "opening",
        "lines": [{"barcode": barcode, "quantity": 20,
                   "cell_address": unique("cell").upper(), "state": "good"}]})
    return {"seller": seller, "account": account, "barcode": barcode}


def seed(account: str, count: int, barcode: str) -> list[int]:
    deadline = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))
    response = httpx.post(f"{SIMULATOR}/__stand__/seed-orders", timeout=10.0, json={
        "account": account, "count": count, "barcode": barcode, "deadline": deadline})
    response.raise_for_status()
    return [int(order["id"]) for order in response.json()["orders"]]


def rows(pool: ConnectionPool, sql: str, params: tuple) -> list[dict]:
    with pool.connection() as connection:
        with single(connection) as cursor:
            cursor.execute(sql, params)
            return cursor.fetchall()


# ------------------------------------------------------------------ стикеры

def test_labels_are_local_before_anyone_asks_to_print(
        pool: ConnectionPool, cabinet: dict) -> None:
    """Инвариант 9: стикер лежит локально до того, как человек нажал печать.

    Проверяется буквально так же, как шаг 7 прогона: у заданий ещё нет ни
    строки подбора, ни сборщика, а этикетки уже свои.
    """
    seed(cabinet["account"], 5, cabinet["barcode"])
    WbSyncWorker(pool, only_accounts=[cabinet["account"]]).tick()

    fetched = WbLabelWorker(pool, only_accounts=[cabinet["account"]]).tick()
    assert fetched == 5, f"получено {fetched} стикеров из 5"

    labels = rows(pool,
                  "SELECT l.format, l.payload, l.checksum, l.invalidated_at, t.state, t.assignee "
                  "  FROM wb_label l JOIN wms_task t ON t.id = l.task_id "
                  "  JOIN wb_account a ON a.id = t.wb_account_id "
                  " WHERE a.external_id = %s", (cabinet["account"],))
    assert len(labels) == 5
    for label in labels:
        assert label["format"] == "zplv", (
            "формат не ZPL: 1–3 КБ текста против 20–100 КБ картинки, и принтер "
            "печатает ZPL нативно, без растеризации драйвером")
        assert label["invalidated_at"] is None
        assert label["state"] == "reserved" and label["assignee"] is None, \
            "подбор уже начался — проверка «стикер готов ДО подбора» потеряла смысл"
        assert bytes(label["payload"]).startswith(b"^XA"), "в стикере не ZPL"
        assert label["checksum"] == hashlib.sha256(bytes(label["payload"])).hexdigest()


def test_the_order_goes_into_a_supply_before_the_sticker_is_asked_for(
        pool: ConnectionPool, cabinet: dict) -> None:
    """Стикер выдаётся только заданию в поставке — требование WB.

    Правило верное, неправильным было выполнять его в момент упаковки, пока
    человек стоит у принтера (раздел 3.3).
    """
    seed(cabinet["account"], 2, cabinet["barcode"])
    WbSyncWorker(pool, only_accounts=[cabinet["account"]]).tick()
    WbLabelWorker(pool, only_accounts=[cabinet["account"]]).tick()

    supplies = rows(pool, "SELECT s.wb_supply_id, s.state FROM wb_supply s "
                          "  JOIN wb_account a ON a.id = s.wb_account_id "
                          " WHERE a.external_id = %s", (cabinet["account"],))
    assert len(supplies) == 1, "накопительная поставка кабинета одна (приложение D)"
    assert supplies[0]["wb_supply_id"], "поставка не создана в Wildberries"
    assert supplies[0]["state"] == "open"

    tasks = rows(pool, "SELECT supply_id FROM wms_task t JOIN wb_account a "
                       "    ON a.id = t.wb_account_id WHERE a.external_id = %s",
                 (cabinet["account"],))
    assert all(task["supply_id"] for task in tasks), "задание не положено в поставку"


def test_labels_are_not_fetched_twice(pool: ConnectionPool, cabinet: dict) -> None:
    """Повторный цикл не тратит лимит кабинета на уже полученные стикеры."""
    seed(cabinet["account"], 3, cabinet["barcode"])
    WbSyncWorker(pool, only_accounts=[cabinet["account"]]).tick()
    worker = WbLabelWorker(pool, only_accounts=[cabinet["account"]])
    assert worker.tick() == 3
    assert worker.tick() == 0, "стикеры запрошены второй раз"


def test_shadow_cabinets_never_write_to_wildberries(
        pool: ConnectionPool, cabinet: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    """Раздел 11, шаг 2: в shadow — только GET, ни одной записи.

    Запрос стикера открывает поставку, то есть пишет в кабинет клиента. В
    защищённом окружении это запрещено абсолютно.
    """
    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.setenv("TRUSTED_HOSTS", "localhost")
    with pool.connection() as connection:
        with single(connection) as cursor:
            cursor.execute("UPDATE wb_account SET mode = 'shadow' WHERE external_id = %s",
                           (cabinet["account"],))
    seed(cabinet["account"], 2, cabinet["barcode"])
    WbSyncWorker(pool, only_accounts=[cabinet["account"]]).tick()

    assert WbLabelWorker(pool, only_accounts=[cabinet["account"]]).tick() == 0, \
        "в shadow запрошены стикеры — это запись в кабинет клиента"


# ------------------------------------------------------------------ остаток

def test_stock_publication_leaves_immediately_after_the_movement(
        pool: ConnectionPool, cabinet: dict) -> None:
    """Инвариант 7: вызов уходит сразу после движения, без таймеров.

    «Немедленно» проверяется по журналу публикаций: строка обязана появиться
    в пределах секунды после движения, а не по расписанию.
    """
    before = int(rows(pool, "SELECT count(*) AS n FROM wb_stock_push p "
                            "  JOIN wb_account a ON a.id = p.account_id "
                            " WHERE a.external_id = %s", (cabinet["account"],))[0]["n"])

    publisher = StockPublisher(pool)
    stock = StockOperations(pool, publisher.notify)
    started = time.monotonic()
    stock.apply_document({
        "seller_external_id": cabinet["seller"], "reference": unique("push"),
        "doc_type": "receipt",
        "lines": [{"barcode": cabinet["barcode"], "quantity": 3,
                   "cell_address": unique("cell").upper(), "state": "good"}]})
    assert publisher.wait_idle(timeout=5.0), "публикация не завершилась"
    elapsed = time.monotonic() - started
    publisher.stop()

    after = rows(pool, "SELECT p.rows, p.result, p.pushed_at FROM wb_stock_push p "
                       "  JOIN wb_account a ON a.id = p.account_id "
                       " WHERE a.external_id = %s ORDER BY p.pushed_at DESC LIMIT 1",
                 (cabinet["account"],))
    total = int(rows(pool, "SELECT count(*) AS n FROM wb_stock_push p "
                           "  JOIN wb_account a ON a.id = p.account_id "
                           " WHERE a.external_id = %s", (cabinet["account"],))[0]["n"])
    assert total == before + 1, "после движения не ушла публикация остатка"
    assert elapsed < 1.0, f"публикация ушла через {elapsed:.2f} с — это уже накопление"
    assert after[0]["result"]["status"] == "ok"


def test_published_amount_is_lowered_by_the_buffer_only(
        pool: ConnectionPool, cabinet: dict) -> None:
    """Инвариант 7: `available = good − buffer`, всегда занижая.

    Резерв не вычитается: движение `good → reserved` уже вывело его из `good`
    (раздел 6.2). Тест держит именно это — вычесть резерв второй раз значит
    занизить публикацию вдвое по активным резервам.
    """
    with pool.connection() as connection:
        with single(connection) as cursor:
            cursor.execute(
                "UPDATE sku SET buffer = 2 WHERE barcode = %s "
                "  AND owner_id = (SELECT id FROM owner WHERE seller_external_id = %s)",
                (cabinet["barcode"], cabinet["seller"]))

    seed(cabinet["account"], 1, cabinet["barcode"])
    WbSyncWorker(pool, only_accounts=[cabinet["account"]]).tick()

    with pool.connection() as connection:
        with single(connection) as cursor:
            owner = repo.find_owner(cursor, cabinet["seller"])
            published = repo.available_for_push(cursor, owner["id"])
            good = repo.balance_of(cursor, owner["id"],
                                   repo.find_sku(cursor, owner["id"],
                                                 barcode=cabinet["barcode"],
                                                 sku_field=None)[0]["id"], "good")
    row = next(row for row in published if row["barcode"] == cabinet["barcode"])
    assert row["available"] == max(0, good - 2), (
        f"публикуем {row['available']} при good={good}, buffer=2: резерв уже "
        f"вычтен движением, вычитать его второй раз нельзя")
    assert row["available"] >= 0, "в WB нельзя публиковать отрицательный остаток"


def test_a_push_in_flight_does_not_lose_the_next_change(
        pool: ConnectionPool, cabinet: dict) -> None:
    """Раздел 6.4: пока вызов в полёте, изменение уезжает следующим.

    Это конвейер, а не задержка: накопленное не теряется и не ждёт таймера.
    """
    publisher = StockPublisher(pool)
    with pool.connection() as connection:
        with single(connection) as cursor:
            owner = repo.find_owner(cursor, cabinet["seller"])
            sku, _ = repo.find_sku(cursor, owner["id"], barcode=cabinet["barcode"],
                                   sku_field=None)

    before = int(rows(pool, "SELECT count(*) AS n FROM wb_stock_push p "
                            "  JOIN wb_account a ON a.id = p.account_id "
                            " WHERE a.external_id = %s", (cabinet["account"],))[0]["n"])
    for _ in range(5):
        publisher.notify(owner["id"], {sku["id"]})
    assert publisher.wait_idle(timeout=10.0)
    publisher.stop()

    after = int(rows(pool, "SELECT count(*) AS n FROM wb_stock_push p "
                           "  JOIN wb_account a ON a.id = p.account_id "
                           " WHERE a.external_id = %s", (cabinet["account"],))[0]["n"])
    # Пять уведомлений подряд не обязаны дать пять вызовов: пока летел первый,
    # остальные схлопнулись в один. Но хотя бы один вызов уйти обязан, и ни
    # одно уведомление не имеет права потеряться молча.
    assert before < after <= before + 5
