"""Средние находки этапа 2: ограничитель, публикация, запись в WB.

Все три про одно: «работает» и «работает правильно» — разные вещи, и разница
видна только когда что-то пошло не так. Пауза после 429 забывалась со сменой
минуты, остаток уезжал на чужой склад, а одна переменная окружения открывала
стенду дорогу в боевой кабинет клиента.
"""
from __future__ import annotations

import os
import uuid

import pytest

from app import rate_limit, repositories as repo
from app.postgres import ConnectionPool, single
from app.service import CatalogOperations, StockOperations, WmsService
from app.wb import WbClient, WbWriteRefused, default_base_url

from dbfixtures import require_database, unique


@pytest.fixture(scope="module")
def pool() -> ConnectionPool:
    connections = ConnectionPool(require_database(), max_size=8)
    yield connections
    connections.close()


@pytest.fixture()
def account(pool: ConnectionPool) -> dict:
    catalog = CatalogOperations(pool)
    seller, external = unique("seller"), unique("wb")
    catalog.upsert_owner({"seller_external_id": seller, "name": "Ограничитель"})
    view = catalog.upsert_wb_account({
        "op": "upsert", "external_id": external, "seller_external_id": seller,
        "display_name": "Кабинет лимита", "secret_ref": f"vault://mmx/test/{external}",
        "mode": "live", "status": "ACTIVE", "wb_warehouse_id": 7})
    with pool.connection() as connection:
        with single(connection) as cursor:
            row = repo.find_account(cursor, external_id=external)
    return {"seller": seller, "external": external, "id": row["id"], "view": view}


# --------------------------------------------------------------- ограничитель

def test_a_pause_after_429_outlives_the_minute(pool: ConnectionPool, account: dict) -> None:
    """Пауза кабинета живёт у кабинета, а не у строки минутного окна.

    `blocked_until` лежал в `wb_rate_limit` по ключу (кабинет, минута): со
    сменой минуты строка становилась другой, и пауза после 429 забывалась
    через считаные секунды. Кабинет шёл долбить Wildberries дальше, а WB
    считает повторные 429 поводом для настоящей блокировки.
    """
    with pool.connection() as connection:
        with single(connection) as cursor:
            rate_limit.block(cursor, account["id"], 600.0)
            assert not rate_limit.take(cursor, account["id"]), "пауза не действует вовсе"

            # Минута сменилась: строка окна другая, пауза обязана остаться.
            cursor.execute("DELETE FROM wb_rate_limit WHERE account_id = %s", (account["id"],))
            permit = rate_limit.take(cursor, account["id"])

    assert not permit, (
        "со сменой минутного окна пауза забылась: кабинет пойдёт в Wildberries "
        "прямо во время наложенной им паузы")
    assert permit.wait_seconds > 60, \
        f"остаток паузы {permit.wait_seconds:.0f} с — она укоротилась до минутного окна"


def test_retention_clears_the_service_journals(pool: ConnectionPool, account: dict) -> None:
    """`wb_rate_limit` — строка на кабинет в минуту, и нужна она ровно минуту."""
    with pool.connection() as connection:
        with single(connection) as cursor:
            cursor.execute(
                "INSERT INTO wb_rate_limit (account_id, window_start, used) "
                "VALUES (%s, now() - interval '30 days', 5)", (account["id"],))
            cursor.execute(
                "INSERT INTO wb_stock_push (id, account_id, pushed_at, rows, result) "
                "VALUES (%s, %s, now() - interval '30 days', 1, '{}'::jsonb)",
                (uuid.uuid4(), account["id"]))
            removed = rate_limit.retention(cursor, days=7)

            cursor.execute("SELECT count(*) AS n FROM wb_rate_limit "
                           " WHERE account_id = %s AND window_start < now() - interval '7 days'",
                           (account["id"],))
            left = int(cursor.fetchone()["n"])

    assert removed >= 2, f"убрано {removed} строк: старьё осталось лежать"
    assert left == 0


def test_our_own_refusal_does_not_block_the_cabinet(pool: ConnectionPool, account: dict) -> None:
    """Собственный отказ ограничителя — не блокировка со стороны Wildberries.

    В сеть мы не пошли, и пауза после своего же отказа удлиняет её на ровном
    месте: следующий такт тоже откажет, уже по её причине.
    """
    from app.wb import WbError
    from app.workers.wb_sync import WbSyncWorker

    worker = WbSyncWorker(pool, only_accounts=[account["external"]])
    worker._after_failure(
        {"id": account["id"], "external_id": account["external"], "sync_attempts": 0},
        WbError(429, "LOCAL_RATE_LIMIT", retry_after=30.0))

    with pool.connection() as connection:
        with single(connection) as cursor:
            cursor.execute("SELECT blocked_until FROM wb_account WHERE id = %s",
                           (account["id"],))
            blocked = cursor.fetchone()["blocked_until"]
    assert blocked is None, (
        "кабинет заблокирован из-за отказа НАШЕГО ограничителя: пауза "
        "выглядит как наложенная Wildberries, хотя в сеть никто не ходил")


# --------------------------------------------------------------- запись в WB

def test_writing_to_anything_but_the_simulator_is_refused() -> None:
    """Последние ворота перед кабинетом клиента (раздел 12).

    Режим кабинета говорит «этому кабинету писать можно», а не «писать можно
    ВОТ СЮДА». Одна переменная окружения — и стенд создаёт поставки в
    настоящем кабинете клиента. Необратимо, и узнает об этом клиент.
    """
    previous = os.environ.get("WB_API_URL")
    os.environ["WB_API_URL"] = "https://marketplace-api.wildberries.ru"
    try:
        assert "wildberries.ru" in default_base_url()
        with WbClient(account_external_id="acc", secret_ref="vault://mmx/test/acc") as client:
            with pytest.raises(WbWriteRefused, match="не симулятор"):
                client.create_supply()
            with pytest.raises(WbWriteRefused):
                client.put_stocks(1, [{"sku": "4600000000001", "amount": 1}])
            with pytest.raises(WbWriteRefused):
                client.add_orders("WB-GI-00000001", [1])
    finally:
        if previous is None:
            os.environ.pop("WB_API_URL", None)
        else:
            os.environ["WB_API_URL"] = previous


def test_reading_from_the_real_wildberries_is_not_blocked() -> None:
    """Чтение — не запись. В shadow читать боевой кабинет как раз и нужно.

    Парная проверка: ворота, закрывающие и чтение, сделали бы невозможным
    шаг 2 раздела 11 — наблюдение за боевым контуром.
    """
    previous = os.environ.get("WB_API_URL")
    os.environ["WB_API_URL"] = "https://marketplace-api.wildberries.ru"
    try:
        with WbClient(account_external_id="acc", secret_ref="vault://mmx/test/acc") as client:
            assert client._is_read("GET", "/api/v3/orders")
            assert client._is_read("POST", "/api/v3/orders/status"), (
                "сверка статусов — чтение, хотя и POST: у Wildberries список "
                "номеров не помещается в строку запроса")
            assert not client._is_read("POST", "/api/v3/supplies")
            assert not client._is_read("PUT", "/api/v3/stocks/1")
    finally:
        if previous is None:
            os.environ.pop("WB_API_URL", None)
        else:
            os.environ["WB_API_URL"] = previous


# -------------------------------------------------------- публикация остатка

def test_a_cabinet_without_a_warehouse_is_not_published_silently(
        pool: ConnectionPool, caplog: pytest.LogCaptureFixture) -> None:
    """Остаток не уезжает на чужой склад №1 и не теряется молча.

    Подставлялся склад №1 — чужой. Остаток клиента уезжал туда, где его нет, а
    инвариант 7 при этом считался выполненным.
    """
    from app.stock_push import StockPublisher

    catalog, stock = CatalogOperations(pool), StockOperations(pool)
    seller, external = unique("seller"), unique("wb")
    barcode = f"46{uuid.uuid4().int % 10**11:011d}"
    catalog.upsert_owner({"seller_external_id": seller, "name": "Без склада"})
    catalog.upsert_wb_account({
        "op": "upsert", "external_id": external, "seller_external_id": seller,
        "display_name": "Кабинет без склада", "secret_ref": f"vault://mmx/test/{external}",
        "mode": "live", "status": "ACTIVE"})          # wb_warehouse_id не задан
    catalog.ensure_product({"seller_external_id": seller, "barcode": barcode})
    stock.apply_document({
        "seller_external_id": seller, "reference": unique("open"), "doc_type": "opening",
        "lines": [{"barcode": barcode, "quantity": 4,
                   "cell_address": unique("cell").upper(), "state": "good"}]})

    with pool.connection() as connection:
        with single(connection) as cursor:
            owner = repo.find_owner(cursor, seller)
            sku, _ = repo.find_sku(cursor, owner["id"], barcode=barcode, sku_field=None)

    published: list[tuple] = []
    publisher = StockPublisher(pool)
    with caplog.at_level("ERROR", logger="wms.stock_push"):
        publisher._push(owner["id"], {sku["id"]}, None)
    publisher.stop()

    assert not published
    assert any("wb_warehouse_id" in record.getMessage() for record in caplog.records), (
        "кабинет без склада промолчал: остаток не публикуется, а инвариант 7 "
        "считается выполненным")


# ------------------------------------------------------- отказы без задания

def test_a_rejected_order_raises_its_alarm_once_not_every_poll(
        pool: ConnectionPool) -> None:
    """Событие отказа — на первый отказ, а не на каждый опрос.

    Заказ неизвестного продавца приезжает каждые две секунды и каждые две
    секунды рождал `wms.reservation.failed.v1`: сорок тысяч событий об одном
    заказе за сутки, и в этом шуме тонули настоящие отказы.
    """
    wms = WmsService(pool)
    order_id = uuid.uuid4().int % 10**12
    params = {
        "idempotency_key": unique("idem"), "seller_external_id": unique("nobody"),
        "wb_account_external_id": unique("nowhere"), "wb_order_id": order_id,
        "sku": "4600000000001", "barcode": "4600000000001", "quantity": 1,
        "correlation_id": unique("corr")}

    first = wms.reserve(params)
    assert first.status == "rejected"
    assert len(first.events) == 1, "первый отказ обязан сказать о себе"

    for _ in range(4):
        again = wms.reserve(dict(params, idempotency_key=unique("idem")))
        assert again.status == "rejected"
        assert again.error_code == first.error_code
        assert again.events == [], (
            "повторный опрос того же заказа снова эмитит событие отказа")

    with pool.connection() as connection:
        with single(connection) as cursor:
            cursor.execute("SELECT seen, error_code FROM wb_order_rejected "
                           " WHERE wb_order_id = %s", (order_id,))
            remembered = cursor.fetchone()
    assert remembered and int(remembered["seen"]) == 5, "отказы не считаются"


def test_an_inactive_client_gets_a_task_for_a_human_not_a_silent_refusal(
        pool: ConnectionPool) -> None:
    """Клиент отключён — заказ всё равно существует, и срок по нему идёт.

    Молчаливый отказ раз в две секунды не поможет никому: задание обязано
    попасть человеку на глаза с кодом.
    """
    catalog = CatalogOperations(pool)
    seller, external = unique("seller"), unique("wb")
    barcode = f"46{uuid.uuid4().int % 10**11:011d}"
    catalog.upsert_owner({"seller_external_id": seller, "name": "Отключённый"})
    catalog.upsert_wb_account({
        "op": "upsert", "external_id": external, "seller_external_id": seller,
        "display_name": "Кабинет отключённого", "secret_ref": f"vault://mmx/test/{external}",
        "mode": "live", "status": "ACTIVE", "wb_warehouse_id": 3})
    catalog.ensure_product({"seller_external_id": seller, "barcode": barcode})
    with pool.connection() as connection:
        with single(connection) as cursor:
            cursor.execute("UPDATE owner SET active = false WHERE seller_external_id = %s",
                           (seller,))

    order_id = uuid.uuid4().int % 10**12
    outcome = WmsService(pool).reserve({
        "idempotency_key": unique("idem"), "seller_external_id": seller,
        "wb_account_external_id": external, "wb_order_id": order_id,
        "sku": barcode, "barcode": barcode, "quantity": 1,
        "correlation_id": unique("corr")})

    assert outcome.error_code == "OWNER_INACTIVE"
    assert outcome.task_id, "задание не заведено — заказ исчез из виду"

    with pool.connection() as connection:
        with single(connection) as cursor:
            cursor.execute("SELECT state, manual_review_code, manual_review_reason "
                           "  FROM wms_task WHERE wb_order_id = %s", (order_id,))
            task = cursor.fetchone()
    assert task["state"] == "manual_review"
    assert task["manual_review_code"] == "OWNER_INACTIVE"
    assert seller in task["manual_review_reason"]
