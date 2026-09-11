"""Таблица переходов: команда из неправильного состояния — отказ, а не работа.

До `ALLOWED_FROM` каждая команда решала сама и решала по-разному. `pack`
пускал из любого состояния вообще, `hand_over` работал по неотгруженной
поставке, `return_to_shelf` снимал резерв и оставлял задание в `reserved` —
после чего то же самое задание выдавалось снова, уже без брони, и один и тот
же товар уезжал по двум заказам.

Разрешённый переход — свойство автомата, а не отдельной функции: здесь
проверяется, что автомат один и он один на всех.
"""
from __future__ import annotations

import uuid

import pytest

from app import repositories as repo
from app.domain import ALLOWED_FROM, TaskState, TransitionRefused, transition_allowed
from app.postgres import ConnectionPool, single
from app.service import CatalogOperations, StockOperations, WmsService
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
    catalog.upsert_owner({"seller_external_id": seller, "name": "Переходы"})
    catalog.upsert_wb_account({
        "op": "upsert", "external_id": account, "seller_external_id": seller,
        "display_name": "Кабинет переходов", "secret_ref": f"vault://mmx/test/{account}",
        "mode": "shadow", "status": "ACTIVE"})
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


def reserve(pool: ConnectionPool, client: dict) -> str:
    outcome = WmsService(pool).reserve({
        "idempotency_key": unique("idem"), "seller_external_id": client["seller"],
        "wb_account_external_id": client["account"],
        "wb_order_id": uuid.uuid4().int % 10**12, "sku": client["barcode"],
        "barcode": client["barcode"], "quantity": 1, "correlation_id": unique("corr")})
    assert outcome.status == "reserved", outcome.error_code
    return outcome.task_id


def force_state(pool: ConnectionPool, task_id: str, state: str) -> None:
    """Поставить состояние в обход команд — чтобы проверить саму команду."""
    with pool.connection() as connection:
        with single(connection) as cursor:
            # Схема требует причину у отменённого (инвариант 11) — ставим и её,
            # иначе не собрать сцену «команда пришла в отменённое задание».
            cursor.execute(
                "UPDATE wms_task SET state = %s, "
                "       cancel_reason = CASE WHEN %s = 'cancelled' "
                "                            THEN 'подготовка сцены теста' END "
                "  WHERE id = %s", (state, state, uuid.UUID(task_id)))


def balance(pool: ConnectionPool, seller: str, state: str) -> int:
    return int(rows(pool, "SELECT COALESCE(SUM(b.qty), 0) AS qty FROM stock_balance b "
                          "  JOIN owner o ON o.id = b.owner_id "
                          " WHERE o.seller_external_id = %s AND b.state = %s",
                    (seller, state))[0]["qty"])


# ------------------------------------------------------------ сама таблица

def test_the_table_says_what_the_machine_says() -> None:
    """Таблица покрывает все команды и только настоящие состояния."""
    known = {state.value for state in TaskState}
    for command, states in ALLOWED_FROM.items():
        assert states, f"команда {command} не выполняется ниоткуда"
        unknown = states - known
        assert not unknown, f"команда {command} разрешена из несуществующих {unknown}"

    # Уехавший товар не отменяют — его разбирают как возврат (раздел 2.12).
    for terminal in (TaskState.SHIPPED, TaskState.HANDED, TaskState.ACCEPTED):
        assert not transition_allowed("cancel", terminal.value), (
            f"отмена разрешена из {terminal.value}: товар уже уехал, "
            f"и это разбор возврата, а не отмена")

    # Незнакомая команда не разрешена ниоткуда: белый список, а не чёрный.
    assert not transition_allowed("самовольная_команда", TaskState.RESERVED.value)


# ------------------------------------------------------- запрещённые входы

@pytest.mark.parametrize("state", ["packed", "shipped", "cancelled", "diverged"])
def test_scanning_from_a_state_that_has_no_scan_is_refused(
        pool: ConnectionPool, client: dict, state: str) -> None:
    """Скан имеет смысл только у того, что сейчас в подборе."""
    task_id = reserve(pool, client)
    force_state(pool, task_id, state)
    tasks = TaskOperations(pool, WmsService(pool))
    if state in ("packed",):
        # Собранное отвечает повтором, а не отказом: ответ мог потеряться.
        assert tasks.scan(task_id, {"barcode": client["barcode"]})["duplicate"]
        return
    with pytest.raises(TransitionRefused):
        tasks.scan(task_id, {"barcode": client["barcode"]})


@pytest.mark.parametrize("state", ["reserved", "cancelled", "handed"])
def test_packing_what_was_not_scanned_is_refused(
        pool: ConnectionPool, client: dict, state: str) -> None:
    """Упаковка идёт только после подтверждённого скана.

    `pack` пускал из любого состояния: непросканированное задание уезжало
    собранным, и рубеж качества (раздел 4) не срабатывал вовсе.
    """
    task_id = reserve(pool, client)
    force_state(pool, task_id, state)
    with pytest.raises(TransitionRefused):
        TaskOperations(pool, WmsService(pool)).pack(
            task_id, {"idempotency_key": unique("pack"),
                      "control_scan_barcode": client["barcode"]})


@pytest.mark.parametrize("state", ["reserved", "packed", "shipped"])
def test_returning_to_the_shelf_what_nobody_holds_is_refused(
        pool: ConnectionPool, client: dict, state: str) -> None:
    """Вернуть на полку может только тот, кто держит вещь в руках."""
    task_id = reserve(pool, client)
    force_state(pool, task_id, state)
    tasks = TaskOperations(pool, WmsService(pool))
    if state == "reserved":
        # Ничего не взято — возврат уже случился: повтор, а не отказ.
        assert tasks.return_to_shelf(task_id, {"reason": "передумал"})["duplicate"]
        return
    with pytest.raises(TransitionRefused):
        tasks.return_to_shelf(task_id, {"reason": "передумал"})


@pytest.mark.parametrize("state", ["shipped", "handed", "accepted"])
def test_cancelling_what_has_already_left_is_a_return_not_a_cancellation(
        pool: ConnectionPool, client: dict, state: str) -> None:
    """Отмена после передачи заводит возврат, а не откатывает резерв.

    `_unwind` возвращал в `good` товар, который физически уехал: остаток рос
    на отменах. Уехавшая вещь возвращается через `wms_return`, и только
    решение по ней вернёт товар в остаток (раздел 2.12).
    """
    task_id = reserve(pool, client)
    force_state(pool, task_id, state)
    before = balance(pool, client["seller"], "good")

    result = TaskOperations(pool, WmsService(pool)).cancel(
        task_id, {"cancellation_event_id": unique("cancel"), "handed_over": True})

    assert result["cancel_reason"], "причина отмены пуста (инвариант 11)"
    assert balance(pool, client["seller"], "good") == before, (
        "товар вернулся в good, хотя физически уехал: остаток растёт на отменах")

    returns = rows(pool, "SELECT state, reason FROM wms_return WHERE task_id = %s",
                   (uuid.UUID(task_id),))
    assert returns, "возврат не заведён — вещь приедет, а принять её нечем"
    assert returns[0]["state"] == "expected"
    assert returns[0]["reason"], "возврат без причины неразбираем"

    task = rows(pool, "SELECT state FROM wms_task WHERE id = %s",
                (uuid.UUID(task_id),))[0]
    assert task["state"] == state, (
        f"состояние стало {task['state']}: факт отгрузки был, и переписывать его нечем")


# ---------------------------------------------------------- возврат на полку

def test_returning_to_the_shelf_keeps_the_reservation(
        pool: ConnectionPool, client: dict) -> None:
    """Вещь осталась на полке и по-прежнему нужна этому заказу.

    Раньше возврат звал `_unwind`: тот писал `reserved → good`, снимал бронь и
    оставлял задание в `reserved`. Задание выдавалось снова уже без резерва —
    тот же товар уезжал по другому заказу, а этот собирали из воздуха.
    """
    task_id = reserve(pool, client)
    tasks = TaskOperations(pool, WmsService(pool))
    pulled = tasks.pull({"assignee": "picker-1", "claim": True, "limit": 10,
                         "owner_external_ids": [client["seller"]]})
    assert any(item["task"]["task_id"] == task_id for item in pulled["tasks"]), \
        "задание не выдалось — возвращать нечего"
    reserved_before = balance(pool, client["seller"], "reserved")
    good_before = balance(pool, client["seller"], "good")

    force_state(pool, task_id, TaskState.PICKING.value)
    tasks.return_to_shelf(task_id, {"reason": "коробка не открывается"})

    with pool.connection() as connection:
        with single(connection) as cursor:
            held = repo.held_reservation(cursor, uuid.UUID(task_id))
    assert held is not None, (
        "резерв снят: задание вернётся в очередь без брони, и тот же товар "
        "уедет по другому заказу")
    assert balance(pool, client["seller"], "reserved") == reserved_before, \
        "возврат на полку тронул резерв"
    assert balance(pool, client["seller"], "good") == good_before, \
        "возврат на полку записал движение, хотя вещь никуда не двигалась"

    task = rows(pool, "SELECT state, assignee FROM wms_task WHERE id = %s",
                (uuid.UUID(task_id),))[0]
    assert task["state"] == TaskState.RESERVED.value
    assert task["assignee"] is None, "задание осталось за сборщиком, который его вернул"

    again = tasks.pull({"assignee": "picker-2", "claim": True, "limit": 50,
                        "owner_external_ids": [client["seller"]]})
    assert any(item["task"]["task_id"] == task_id for item in again["tasks"]), \
        "возвращённое задание не выдаётся заново — работа потеряна"

    line = rows(pool, "SELECT scan_result FROM pick_line WHERE task_id = %s",
                (uuid.UUID(task_id),))
    assert line and line[0]["scan_result"] == "returned", (
        "факт «взял и положил обратно» не записан: по нему видно, "
        "на какой полке сборщик спотыкается")


# ------------------------------------------------------------- выдача заданий

def test_the_queue_never_hands_out_what_has_already_left(
        pool: ConnectionPool, client: dict) -> None:
    """`states` в запросе — не «что прислали», а белый список.

    `states: ["shipped"]` выдавало сборщику задание, товар которого уже уехал.
    """
    task_id = reserve(pool, client)
    force_state(pool, task_id, TaskState.SHIPPED.value)
    tasks = TaskOperations(pool, WmsService(pool))

    with pytest.raises(ValueError, match="не выдаются сборщику"):
        tasks.pull({"assignee": "picker-1", "claim": True, "states": ["shipped"]})
    with pytest.raises(ValueError, match="не выдаются сборщику"):
        tasks.pull({"assignee": "picker-1", "claim": True,
                    "states": ["cancelled", "diverged"]})

    # Смешанный запрос отдаёт только то, что законно выдавать.
    mixed = tasks.pull({"assignee": "picker-1", "claim": False,
                        "states": ["reserved", "shipped"]})
    assert all(item["task"]["state"] in ("reserved", "picking")
               for item in mixed["tasks"])


def test_an_expired_lease_frees_the_task_in_any_state(
        pool: ConnectionPool, client: dict) -> None:
    """Лизинг истёк — человека нет, и держать за ним задание незачем.

    Условие перечисляло только `reserved` и `picking`: `picked` и `packed` за
    ушедшим со смены сборщиком висели вечно.
    """
    task_id = reserve(pool, client)
    with pool.connection() as connection:
        with single(connection) as cursor:
            cursor.execute(
                "UPDATE wms_task SET state = 'picked', assignee = %s, "
                "       claimed_at = now() - interval '2 hours', "
                "       claim_expires_at = now() - interval '1 hour' WHERE id = %s",
                (uuid.uuid4(), uuid.UUID(task_id)))

    TaskOperations(pool, WmsService(pool)).pull(
        {"assignee": "picker-2", "claim": True, "limit": 1,
         "owner_external_ids": [client["seller"]]})

    task = rows(pool, "SELECT assignee, claim_expires_at FROM wms_task WHERE id = %s",
                (uuid.UUID(task_id),))[0]
    assert task["assignee"] is None, (
        "задание в `picked` осталось за пропавшим сборщиком: "
        "оно не потеряно только на бумаге")
    assert task["claim_expires_at"] is None


def test_a_claimed_task_comes_back_with_its_lease(
        pool: ConnectionPool, client: dict) -> None:
    """В ответе на выдачу — кто взял и до какого времени.

    `_TASK_VIEW` читал `wms_task` в том же операторе, что и UPDATE, а видел
    снимок ДО него: приезжали `assignee: null` и `leased_until: null`. Рабочее
    место получало задание, за которым по ответу никто не закреплён.
    """
    task_id = reserve(pool, client)
    answer = TaskOperations(pool, WmsService(pool)).pull(
        {"assignee": "picker-1", "claim": True, "limit": 10,
         "owner_external_ids": [client["seller"]], "lease_seconds": 600})

    item = next(one for one in answer["tasks"] if one["task"]["task_id"] == task_id)
    assert item["leased_until"], (
        "leased_until пуст: рабочему месту нечем показать, до какого времени "
        "задание за сборщиком, и нечем понять, что лизинг истёк")
    assert item["task"]["assignee"], "в ответе никто не взял задание"
    assert item["task"]["claim_expires_at"] == item["leased_until"]


def test_reading_the_queue_shows_what_is_already_in_hands(
        pool: ConnectionPool, client: dict) -> None:
    """`claim: false` с исполнителем отдаёт и свободное, и его собственное.

    Условие «исполнителя нет» действовало всегда, и проекция рабочего места
    теряла всё, что сборщик взял: задание исчезало с экрана ровно в тот
    момент, когда человек его взял, и появлялось обратно, только когда отдавал.
    """
    from app.tasks import assignee_id

    mine, theirs = reserve(pool, client), reserve(pool, client)
    tasks = TaskOperations(pool, WmsService(pool))
    tasks.pull({"assignee": "picker-1", "claim": True, "limit": 1,
                "owner_external_ids": [client["seller"]]})
    tasks.pull({"assignee": "picker-2", "claim": True, "limit": 1,
                "owner_external_ids": [client["seller"]]})

    taken = {row["id"]: row["assignee"] for row in rows(
        pool, "SELECT id, assignee FROM wms_task WHERE id = ANY(%s)",
        ([uuid.UUID(mine), uuid.UUID(theirs)],))}
    assert all(taken.values()), "сцена не собрана: задания никто не взял"

    screen = tasks.pull({"claim": False, "limit": 50, "assignee": "picker-1",
                         "owner_external_ids": [client["seller"]]})
    shown = {item["task"]["task_id"] for item in screen["tasks"]}
    ours = next(task_id for task_id, holder in taken.items()
                if holder == assignee_id("picker-1"))
    others = next(task_id for task_id, holder in taken.items()
                  if holder != assignee_id("picker-1"))

    assert str(ours) in shown, (
        "задание в руках у этого сборщика пропало с его экрана — он стоит с "
        "коробкой, а экран говорит «работы нет»")
    assert str(others) not in shown, "на экране появилось чужое занятое задание"

    # Чтение ничего не занимает: инвариант выдачи не ослаблен.
    after = rows(pool, "SELECT assignee FROM wms_task WHERE id = %s", (ours,))[0]
    assert after["assignee"] == assignee_id("picker-1"), "чтение перезаняло задание"


def test_reading_the_queue_may_ask_for_states_the_handout_refuses(
        pool: ConnectionPool, client: dict) -> None:
    """Экран показывает и собранное, и уехавшее — выдавать их нельзя.

    Парная проверка к белому списку: запрет на выдачу не должен превращаться в
    запрет смотреть.
    """
    task_id = reserve(pool, client)
    force_state(pool, task_id, TaskState.SHIPPED.value)
    tasks = TaskOperations(pool, WmsService(pool))

    screen = tasks.pull({"claim": False, "limit": 50,
                         "states": ["reserved", "packed", "shipped"],
                         "owner_external_ids": [client["seller"]]})
    assert any(item["task"]["task_id"] == task_id for item in screen["tasks"]), (
        "уехавшее задание не видно на экране — работа, которая сделана, "
        "пропала из виду")

    with pytest.raises(ValueError, match="не выдаются сборщику"):
        tasks.pull({"assignee": "picker-9", "claim": True, "states": ["shipped"]})
