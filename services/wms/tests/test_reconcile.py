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

from app.domain import agrees_with_wb
from app.postgres import ConnectionPool, single
from app.service import CatalogOperations, StockOperations, WmsService
from app.wb import WbClient
from app.workers.wb_labels import WbLabelWorker
from app.workers.wb_reconcile import WbReconcileWorker
from app.workers.wb_sync import WbSyncWorker

from dbfixtures import require_database, unique

SIMULATOR = (os.getenv("WB_SIMULATOR_URL") or "http://127.0.0.1:8090").rstrip("/")

# Ссылка на настоящий постраничный метод: тест ниже подменяет его на отказ
# и обязан вернуть на место именно исходный, а не свою же подмену.
_ORDERS_PAGE = WbClient.orders


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


# --------------------------------------------------- поставка, отмена, пропажа

@pytest.fixture()
def live_cabinet(pool: ConnectionPool) -> dict:
    """Кабинет, которому разрешено писать в WB: без этого не собрать поставку."""
    catalog, stock = CatalogOperations(pool), StockOperations(pool)
    seller, account = unique("seller"), unique("wb")
    barcode = f"46{uuid.uuid4().int % 10**11:011d}"
    catalog.upsert_owner({"seller_external_id": seller, "name": "Сверка и поставка"})
    catalog.upsert_wb_account({
        "op": "upsert", "external_id": account, "seller_external_id": seller,
        "display_name": "Кабинет поставки", "secret_ref": f"vault://mmx/test/{account}",
        "mode": "live", "status": "ACTIVE"})
    catalog.ensure_product({"seller_external_id": seller, "barcode": barcode})
    stock.apply_document({
        "seller_external_id": seller, "reference": unique("open"), "doc_type": "opening",
        "lines": [{"barcode": barcode, "quantity": 10,
                   "cell_address": unique("cell").upper(), "state": "good"}]})
    return {"seller": seller, "account": account, "barcode": barcode}


def _by_state(pool: ConnectionPool, seller: str) -> dict[str, int]:
    """Остаток владельца по состояниям. `stock_balance` — проекция (инвариант 1)."""
    return {row["state"]: int(row["qty"]) for row in rows(
        pool, "SELECT b.state, sum(b.qty) AS qty FROM stock_balance b "
              "  JOIN owner o ON o.id = b.owner_id "
              " WHERE o.seller_external_id = %s GROUP BY b.state", (seller,))}


def test_an_order_inside_a_supply_is_not_a_divergence(
        pool: ConnectionPool, live_cabinet: dict) -> None:
    """Задание в поставке законно имеет у WB статус `confirm`.

    Стикеры берутся заранее (инвариант 9), а стикер WB выдаёт только заданию,
    уже положенному в поставку. Значит сразу после резерва заказ у WB —
    `confirm`, а у нас — `reserved`. Таблица 1 ждала `new`, и каждое живое
    задание уходило в `diverged` в течение минуты: сверка отменяла работу
    стикеровщика, а склад получал остановленные задания на ровном месте.
    """
    order_ids = seed(live_cabinet["account"], 2, live_cabinet["barcode"])
    WbSyncWorker(pool, only_accounts=[live_cabinet["account"]]).tick()
    fetched = WbLabelWorker(pool, only_accounts=[live_cabinet["account"]]).tick()
    assert fetched == 2, "стикеры не взяты — проверять нечего"

    WbReconcileWorker(pool, only_accounts=[live_cabinet["account"]]).tick()

    tasks = rows(pool, "SELECT state, wb_status, supply_id FROM wms_task "
                       " WHERE wb_order_id = ANY(%s)", (order_ids,))
    assert tasks and all(task["supply_id"] for task in tasks), "заказ не в поставке"
    assert all(task["wb_status"] == "confirm" for task in tasks), \
        "WB не вернул confirm — сцена не воспроизведена"
    assert all(task["state"] == "reserved" for task in tasks), (
        "задание ушло в diverged из-за собственной же поставки: "
        f"состояния {[task['state'] for task in tasks]}")


def test_a_cancellation_at_wildberries_cancels_the_task_and_frees_the_stock(
        pool: ConnectionPool, cabinet: dict) -> None:
    """Отмена у WB — команда, а не расхождение.

    В боевом контуре 2645 отмен лежали с пустой причиной, а товар оставался в
    резерве под заказ, которого больше нет: склад собирал бы его вручную.
    Отмена обязана снять резерв, вернуть товар в good и записать разбираемую
    причину (инвариант 11).
    """
    order_ids = seed(cabinet["account"], 1, cabinet["barcode"])
    WbSyncWorker(pool, only_accounts=[cabinet["account"]]).tick()

    before = _by_state(pool, cabinet["seller"])
    assert before.get("reserved") == 1, "резерв не встал — отменять нечего"

    httpx.post(f"{SIMULATOR}/__stand__/cancel-orders", timeout=10.0,
               json={"account": cabinet["account"], "orders": order_ids}).raise_for_status()

    WbReconcileWorker(pool, only_accounts=[cabinet["account"]]).tick()

    task = rows(pool, "SELECT state, cancel_reason FROM wms_task WHERE wb_order_id = %s",
                (order_ids[0],))[0]
    assert task["state"] == "cancelled", (
        f"состояние {task['state']}: заказ отменён у клиента, а задание живо — "
        f"склад соберёт то, чего никто не ждёт")
    assert task["cancel_reason"] and str(order_ids[0]) in task["cancel_reason"], \
        f"причина отмены не разбираема: {task['cancel_reason']!r}"

    after = _by_state(pool, cabinet["seller"])
    assert after.get("reserved", 0) == 0, "резерв не снят под отменённый заказ"
    assert after.get("good", 0) == before.get("good", 0) + 1, "товар не вернулся в good"

    held = rows(pool, "SELECT r.state FROM reservation r JOIN wms_task t ON t.id = r.task_id "
                      " WHERE t.wb_order_id = %s", (order_ids[0],))
    assert held and all(row["state"] == "released" for row in held), "резерв остался held"


def test_reconciliation_asks_about_our_own_tasks_not_the_first_page(
        pool: ConnectionPool, cabinet: dict) -> None:
    """Сверка спрашивает поимённо про свои незакрытые задания.

    `GET /api/v3/orders` отдаёт историю кабинета с курсором, и её первая
    страница — самые старые заказы за всё время. Открытое задание, за которым
    у WB накопилась тысяча более ранних, в эту страницу не попадает никогда:
    его расхождения не видит никто, а кабинет при этом числится сверенным.
    """
    order_ids = seed(cabinet["account"], 1, cabinet["barcode"])
    WbSyncWorker(pool, only_accounts=[cabinet["account"]]).tick()

    asked: list[list[int]] = []
    real_status = WbClient.orders_status

    def watch(self, order_ids_arg):
        asked.append([int(value) for value in order_ids_arg])
        return real_status(self, order_ids_arg)

    def refuse_page(*_args, **_kwargs):
        raise AssertionError(
            "сверка снова читает первую страницу GET /api/v3/orders: "
            "открытые задания старше тысячи заказов она так не увидит")

    WbClient.orders_status, WbClient.orders = watch, refuse_page
    try:
        WbReconcileWorker(pool, only_accounts=[cabinet["account"]]).tick()
    finally:
        WbClient.orders_status, WbClient.orders = real_status, _ORDERS_PAGE

    assert asked, "сверка не спросила статусы поимённо"
    assert order_ids[0] in asked[0], "нашего задания нет в запросе статусов"


def test_a_task_wildberries_does_not_know_raises_an_alarm(
        pool: ConnectionPool, cabinet: dict, caplog: pytest.LogCaptureFixture) -> None:
    """Задание, о котором WB промолчал, — находка, а не тишина.

    Молчание значит, что заказа в кабинете нет: подменили токен, смотрим не
    тот кабинет, заказ удалён. Раньше такое задание просто не попадало в
    выборку и жило у нас вечно.
    """
    seed(cabinet["account"], 1, cabinet["barcode"])
    WbSyncWorker(pool, only_accounts=[cabinet["account"]]).tick()

    # Наше задание есть, а у WB такого заказа нет вовсе.
    ghost = 990_000_000 + uuid.uuid4().int % 9_000_000
    with pool.connection() as connection:
        with single(connection) as cursor:
            cursor.execute(
                "UPDATE wms_task SET wb_order_id = %s, last_reconciled_at = NULL "
                "  WHERE wb_account_id = (SELECT id FROM wb_account WHERE external_id = %s)",
                (ghost, cabinet["account"]))

    with caplog.at_level("ERROR", logger="wms.wb_reconcile"):
        WbReconcileWorker(pool, only_accounts=[cabinet["account"]]).tick()

    assert any("не знает" in record.getMessage() for record in caplog.records), (
        "пропавшее у WB задание не подняло алерта: "
        f"{[record.getMessage() for record in caplog.records]}")


def test_acceptance_is_confirmed_only_by_what_wildberries_actually_says(
        pool: ConnectionPool, live_cabinet: dict) -> None:
    """`ACCEPTED_BY_WB` встаёт по фактическому `complete`, а не по нашему слову.

    Раньше `reconcile` просто ставил `accepted` всем заданиям поставки — то
    есть подтверждал приёмку сам себе. Так в боевом контуре 6072 задания
    оказались в терминальном успехе, ничего не доказав.
    """
    from app.shipments import ShipmentOperations
    from app.tasks import TaskOperations

    order_ids = seed(live_cabinet["account"], 1, live_cabinet["barcode"])
    WbSyncWorker(pool, only_accounts=[live_cabinet["account"]]).tick()
    WbLabelWorker(pool, only_accounts=[live_cabinet["account"]]).tick()

    task_id = rows(pool, "SELECT id FROM wms_task WHERE wb_order_id = %s",
                   (order_ids[0],))[0]["id"]
    tasks = TaskOperations(pool, WmsService(pool))
    tasks.scan(str(task_id), {"barcode": live_cabinet["barcode"]})
    tasks.pack(str(task_id), {"idempotency_key": unique("pack"),
                              "control_scan_barcode": live_cabinet["barcode"]})

    supply = rows(pool, "SELECT s.wb_supply_id FROM wb_supply s JOIN wb_account a "
                        "    ON a.id = s.wb_account_id WHERE a.external_id = %s",
                  (live_cabinet["account"],))[0]["wb_supply_id"]
    shipments = ShipmentOperations(pool, WmsService(pool))
    shipments.handle({"seller_external_id": live_cabinet["seller"],
                      "idempotency_key": unique("deliver"), "action": "deliver",
                      "wb_supply_id": supply})
    shipments.handle({"seller_external_id": live_cabinet["seller"],
                      "idempotency_key": unique("hand"), "action": "hand_over",
                      "wb_supply_id": supply, "handed_over_by": "кладовщик Пётр"})

    accepted = shipments.handle({"seller_external_id": live_cabinet["seller"],
                                 "idempotency_key": unique("recon"), "action": "reconcile",
                                 "wb_supply_id": supply})
    assert accepted["state"] == "accepted_by_wb"
    assert accepted["accepted_at"], "приёмка без времени сверки"

    task = rows(pool, "SELECT state FROM wms_task WHERE id = %s", (task_id,))[0]
    assert task["state"] == "accepted", f"состояние задания {task['state']}"


def test_a_cancellation_while_the_sticker_is_being_fetched_does_not_revive_the_task(
        pool: ConnectionPool, live_cabinet: dict) -> None:
    """Между «спросили стикер» и «сохранили ответ» проходит вызов в сеть.

    Задание за это время успевают отменить. Ответ WB, записанный вслепую,
    ВОСКРЕШАЛ его: `invalidated_at` сбрасывался в NULL, и отменённое задание
    снова выглядело готовым к отгрузке — со стикером и в поставке.
    """
    from app.tasks import TaskOperations
    from app.workers import wb_labels

    order_ids = seed(live_cabinet["account"], 1, live_cabinet["barcode"])
    WbSyncWorker(pool, only_accounts=[live_cabinet["account"]]).tick()
    task_id = rows(pool, "SELECT id FROM wms_task WHERE wb_order_id = %s",
                   (order_ids[0],))[0]["id"]

    # Отмена происходит ровно в окне между запросом стикера и его записью.
    tasks = TaskOperations(pool, WmsService(pool))
    real_stickers = wb_labels.WbClient.stickers

    def cancel_in_the_window(self, ids, *, sticker_format="zplv"):
        result = real_stickers(self, ids, sticker_format=sticker_format)
        tasks.cancel(str(task_id), {"cancellation_event_id": unique("race"),
                                    "handed_over": False})
        return result

    wb_labels.WbClient.stickers = cancel_in_the_window
    try:
        WbLabelWorker(pool, only_accounts=[live_cabinet["account"]]).tick()
    finally:
        wb_labels.WbClient.stickers = real_stickers

    task = rows(pool, "SELECT state, cancel_reason FROM wms_task WHERE id = %s",
                (task_id,))[0]
    assert task["state"] == "cancelled", (
        f"состояние {task['state']}: ответ Wildberries воскресил отменённое задание")

    label = rows(pool, "SELECT invalidated_at FROM wb_label WHERE task_id = %s", (task_id,))
    assert not label or label[0]["invalidated_at"] is not None, (
        "стикер отменённого задания снова действителен — его напечатают и наклеят")
