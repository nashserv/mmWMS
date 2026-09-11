"""Сверка с Wildberries: расхождение будит человека, а не молчит.

В боевом контуре 2467 заданий из 6374 расходились с WB — `CANCELLED` у нас
против `complete` у них, — и ни одно расхождение не подняло алерта (раздел 3).
Этот тест существует, чтобы такое молчание нельзя было вернуть незаметно.
"""
from __future__ import annotations

import os
import time
import uuid

import httpx
import pytest

from app import repositories as repo
from app.domain import agrees_with_wb
from app.postgres import ConnectionPool, single
from app.service import CatalogOperations, StockOperations, WmsService
from app.workers.wb_reconcile import WbReconcileWorker
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
        pytest.skip(f"нужен симулятор WB по {SIMULATOR}")


@pytest.fixture()
def cabinet(pool: ConnectionPool) -> dict:
    catalog, stock = CatalogOperations(pool), StockOperations(pool)
    seller, account = unique("seller"), unique("wb")
    barcode = f"46{uuid.uuid4().int % 10**11:011d}"
    catalog.upsert_owner({"seller_external_id": seller, "name": "Сверка"})
    catalog.upsert_wb_account({
        "op": "upsert", "external_id": account, "seller_external_id": seller,
        "display_name": "Кабинет сверки", "secret_ref": f"vault://mmx/test/{account}",
        "mode": "shadow", "status": "ACTIVE"})
    catalog.ensure_product({"seller_external_id": seller, "barcode": barcode})
    stock.apply_document({
        "seller_external_id": seller, "reference": unique("open"), "doc_type": "opening",
        "lines": [{"barcode": barcode, "quantity": 10,
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


def test_the_mapping_table_is_followed_literally() -> None:
    """Таблица 1 docs/state-mapping.md: одно состояние — один статус WB."""
    assert agrees_with_wb("reserved", "new")
    assert agrees_with_wb("in_supply", "confirm")
    assert agrees_with_wb("shipped", "complete")
    assert agrees_with_wb("handed", "complete")
    assert agrees_with_wb("accepted", "complete")
    assert agrees_with_wb("cancelled", "cancel")

    # То самое расхождение боевого контура: у нас отменено, у WB отгружено.
    assert not agrees_with_wb("cancelled", "complete")
    assert not agrees_with_wb("reserved", "cancel")
    # Незнакомый статус разбирает человек, а не догадка.
    assert not agrees_with_wb("reserved", "unexpected")
    # Уже разошедшееся заново расходиться некуда.
    assert agrees_with_wb("diverged", "complete")


def test_a_matching_status_is_only_recorded(pool: ConnectionPool, cabinet: dict) -> None:
    """Согласованное задание отмечается сверенным и остаётся в своём состоянии."""
    order_ids = seed(cabinet["account"], 2, cabinet["barcode"])
    WbSyncWorker(pool, only_accounts=[cabinet["account"]]).tick()

    checked = WbReconcileWorker(pool, only_accounts=[cabinet["account"]]).tick()
    assert checked >= 2

    tasks = rows(pool, "SELECT state, wb_status, last_reconciled_at FROM wms_task "
                       " WHERE wb_order_id = ANY(%s)", (order_ids,))
    assert tasks and all(task["state"] == "reserved" for task in tasks)
    assert all(task["wb_status"] == "new" for task in tasks)
    assert all(task["last_reconciled_at"] for task in tasks), "сверка не оставила следа"


def test_a_disagreement_stops_the_task_instead_of_overwriting_it(
        pool: ConnectionPool, cabinet: dict) -> None:
    """Инвариант 10: расхождение — состояние `diverged` и алерт.

    Задание не перезаписывается: кто прав — неизвестно. У WB может быть
    отмена, которой мы не видели, а у нас отгрузка, о которой WB ещё не знает.
    """
    order_ids = seed(cabinet["account"], 1, cabinet["barcode"])
    WbSyncWorker(pool, only_accounts=[cabinet["account"]]).tick()

    # Wildberries считает заказ отгруженным, у нас он лежит зарезервированным.
    supply = httpx.post(f"{SIMULATOR}/api/v3/supplies", timeout=10.0, json={"name": "x"},
                        headers={"X-Stand-Account": cabinet["account"]}).json()["id"]
    httpx.patch(f"{SIMULATOR}/api/marketplace/v3/supplies/{supply}/orders", timeout=10.0,
                json={"orders": order_ids},
                headers={"X-Stand-Account": cabinet["account"]})
    httpx.patch(f"{SIMULATOR}/api/v3/supplies/{supply}/deliver", timeout=10.0,
                headers={"X-Stand-Account": cabinet["account"]})

    WbReconcileWorker(pool, only_accounts=[cabinet["account"]]).tick()

    task = rows(pool, "SELECT state, wb_status FROM wms_task WHERE wb_order_id = %s",
                (order_ids[0],))[0]
    assert task["state"] == "diverged", (
        f"состояние {task['state']}: расхождение с WB обязано будить человека, "
        f"а не тихо переписываться")
    assert task["wb_status"] == "complete", "статус WB не сохранён — разбирать нечего"

    report = rows(pool, "SELECT seller_external_id, tasks FROM ("
                        "  SELECT o.seller_external_id, count(*) AS tasks "
                        "    FROM wms_task t JOIN owner o ON o.id = t.owner_id "
                        "   WHERE t.state = 'diverged' AND o.seller_external_id = %s "
                        "   GROUP BY 1) r", (cabinet["seller"],))
    assert report and int(report[0]["tasks"]) >= 1, "расхождение не попало в отчёт"


def test_reconciliation_never_writes_to_wildberries(
        pool: ConnectionPool, cabinet: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    """Раздел 11, шаг 2: в shadow — только GET, ни одной записи.

    Кабинет теста именно в shadow, и сверка обязана в нём работать: она и есть
    главный инструмент теневого прогона. Проверяется буквально — каждый
    пишущий метод клиента WB на время сверки запрещён.
    """
    from app.wb import WbClient

    seed(cabinet["account"], 1, cabinet["barcode"])
    WbSyncWorker(pool, only_accounts=[cabinet["account"]]).tick()

    for method in ("create_supply", "add_orders", "release_from_supply",
                   "deliver", "put_stocks", "stickers"):
        monkeypatch.setattr(WbClient, method, _forbidden(method))

    assert WbReconcileWorker(pool, only_accounts=[cabinet["account"]]).tick() >= 1


def _forbidden(name: str):
    def guard(*_args, **_kwargs):
        raise AssertionError(
            f"сверка позвала {name}: в shadow в Wildberries не пишут ничего "
            f"(раздел 11, шаг 2)")
    return guard


def test_a_diverged_task_is_not_reconciled_again(pool: ConnectionPool, cabinet: dict) -> None:
    """Разошедшееся задание ждёт человека и не переоткрывается сверкой."""
    order_ids = seed(cabinet["account"], 1, cabinet["barcode"])
    WbSyncWorker(pool, only_accounts=[cabinet["account"]]).tick()
    with pool.connection() as connection:
        with single(connection) as cursor:
            cursor.execute("UPDATE wms_task SET state = 'diverged' WHERE wb_order_id = %s",
                           (order_ids[0],))

    WbReconcileWorker(pool, only_accounts=[cabinet["account"]]).tick()
    task = rows(pool, "SELECT state FROM wms_task WHERE wb_order_id = %s", (order_ids[0],))[0]
    assert task["state"] == "diverged"


def test_reconciliation_paces_itself_and_does_not_burn_the_cabinet_limit(
        pool: ConnectionPool, cabinet: dict) -> None:
    """Сверка не крутится без пауз, даже когда работа находится каждый цикл.

    Лимит Wildberries — 300 запросов в минуту и общий на кабинет. Воркер без
    пауз выбирает его целиком и лишает вызовов соседей: опрос заданий и
    публикацию остатка. Ровно это и случилось на стенде — 395 вызовов сверки
    за несколько минут, после чего задания перестали доезжать вовсе.

    Причина была в метке: `last_reconciled_at` проставляется только заданиям,
    которые пришли в ответе WB. Заданий, которых в ответе нет, метка не
    касается, и кабинет оставался «просроченным» навсегда.
    """
    seed(cabinet["account"], 1, cabinet["barcode"])
    WbSyncWorker(pool, only_accounts=[cabinet["account"]]).tick()

    # Задание, которого у Wildberries нет вовсе: именно такие и держали
    # кабинет вечно просроченным.
    with pool.connection() as connection:
        with single(connection) as cursor:
            cursor.execute(
                "UPDATE wms_task SET last_reconciled_at = NULL "
                "  WHERE wb_account_id = (SELECT id FROM wb_account WHERE external_id = %s)",
                (cabinet["account"],))

    worker = WbReconcileWorker(pool, only_accounts=[cabinet["account"]])
    assert worker.tick() >= 1, "первый цикл обязан что-то сверить"
    assert worker.tick() == 0, (
        "второй цикл подряд снова пошёл в Wildberries — сверка выберет "
        "общий лимит кабинета и оставит без вызовов опрос заданий")


def test_the_divergence_report_is_written_down_and_not_only_logged(
        pool: ConnectionPool, cabinet: dict) -> None:
    """Раздел 11, шаг 2: отчёт расхождений ложится в таблицу, а не только в лог.

    Наблюдают неделю, и вопрос недели — «убывает ли разница». По логу его не
    задать: он ротируется и исчезает вместе с контейнером. Копить расхождения
    молча — ровно то, что делает боевой контур сегодня.
    """
    order_ids = seed(cabinet["account"], 2, cabinet["barcode"])
    WbSyncWorker(pool, only_accounts=[cabinet["account"]]).tick()

    # Разводим наше состояние с тем, что скажет WB: у нас отменено, у него
    # отгружено. То самое расхождение боевого контура — 2467 заданий.
    with pool.connection() as connection:
        with single(connection) as cursor:
            cursor.execute(
                "UPDATE wms_task SET state = 'diverged', wb_status = 'complete' "
                " WHERE wb_order_id = ANY(%s)", (order_ids,))

    WbReconcileWorker(pool, only_accounts=[cabinet["account"]])._report()

    saved = rows(pool,
                 "SELECT r.state, r.wb_status, r.tasks, r.observed_on "
                 "  FROM shadow_divergence_report r "
                 " WHERE r.seller_external_id = %s", (cabinet["seller"],))
    assert saved, (
        "расхождение не попало в shadow_divergence_report — за неделю наблюдения "
        "сравнить день с днём будет нечем")
    assert saved[0]["state"] == "diverged"
    assert saved[0]["wb_status"] == "complete"
    assert int(saved[0]["tasks"]) == len(order_ids)
