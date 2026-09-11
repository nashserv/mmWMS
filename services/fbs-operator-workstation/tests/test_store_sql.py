"""Каждый запрос `Store` — против настоящего Postgres.

`FakeStore` в остальных тестах проверяет логику рабочего места, но не SQL:
опечатка в имени колонки, забытый `RETURNING`, ключ, которого нет в схеме, —
всё это он пропускает молча, потому что у него этих запросов нет вовсе. В
боевом контуре такая опечатка выглядит как «база рабочего места недоступна»:
`execute` ловит любое исключение и возвращает `None`.

Здесь исполняется КАЖДЫЙ метод `Store`, по-настоящему, в базе
`workstation_test` (правило 4 промта аудита: тесты не ходят в рабочую базу).
"""
from __future__ import annotations

import asyncio
import os
import uuid
from urllib.parse import urlparse

import pytest

from app.domain import Task
from app.store import Store, _is_connection_broken

pytestmark = pytest.mark.stand


def _database_url() -> str:
    """Только `workstation_test`. Рабочая база тестам не полагается."""
    dsn = (os.getenv("WORKSTATION_TEST_DATABASE_URL") or "").strip()
    if not dsn:
        pytest.skip("нужен WORKSTATION_TEST_DATABASE_URL: запросы Store "
                    "проверяются на настоящем SQL")
    name = (urlparse(dsn).path or "").lstrip("/").split("?")[0]
    if not name.endswith("_test"):
        pytest.exit(
            f"WORKSTATION_TEST_DATABASE_URL указывает на базу {name!r}, а имя "
            f"обязано оканчиваться на '_test'.", returncode=2)
    return dsn


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def store():
    handle = Store(_database_url(), max_connections=4)
    assert run(handle.ping()), (
        f"база рабочего места недоступна: {handle.last_error}. "
        f"Схему накатывает `docker compose run --rm workstation-test-migrate`")
    yield handle
    run(handle.close())


def _task(task_id: str, **overrides) -> Task:
    row = {
        "task_id": task_id, "wb_order_id": 123456, "owner_external_id": "seller-1",
        "barcode": "4600000000011", "seller_sku": "ART-1", "name": "Платье",
        "quantity": 1, "state": "reserved",
    }
    route_order = overrides.pop("route_order", 10)
    row.update(overrides)
    from app.domain import task_from_contract
    # Адрес и порядок обхода живут в размещении, а не в самом задании:
    # `add_lines` читает их именно оттуда.
    return task_from_contract(row, placements=[{
        "barcode": row["barcode"], "cell_address": "A-01-01",
        "box_barcode": "BOX-1", "quantity": row["quantity"],
        "route_order": route_order, "state": "good"}])


def uid() -> str:
    return str(uuid.uuid4())


# ------------------------------------------------------------------ сессии

def test_a_session_lives_through_its_whole_cycle(store: Store) -> None:
    """Открыть, наполнить, прочитать, найти по штрихкоду, закрыть."""
    actor, station = uid(), uid()
    barcode = f"PL-{uuid.uuid4().hex[:10].upper()}"

    session_id = run(store.open_session(actor_id=actor, station_id=station,
                                        picklist_barcode=barcode))
    assert session_id, f"сессия не заведена: {store.last_error}"

    tasks = [_task(uid(), route_order=20), _task(uid(), route_order=10)]
    added = run(store.add_lines(session_id, tasks))
    assert added == 2, f"строк добавлено {added}: {store.last_error}"

    # Повтор ничего не добавляет: сборщик обновляет лист, не теряя сканов.
    assert run(store.add_lines(session_id, tasks)) == 0

    lines = run(store.session_lines(session_id))
    assert [line["route_order"] for line in lines] == [10, 20], (
        "строки листа идут не по маршруту обхода склада")

    view = run(store.session(session_id))
    assert view and str(view["id"]) == session_id
    assert view["picklist_barcode"] == barcode

    found = run(store.session_by_barcode(barcode))
    assert found and str(found["id"]) == session_id, (
        "лист не находится по своему штрихкоду — сборщик не вернётся к работе")

    opened = run(store.open_sessions())
    assert any(str(row["id"]) == session_id for row in opened)

    run(store.finish_session(session_id))
    assert not any(str(row["id"]) == session_id for row in run(store.open_sessions()))


def test_a_scan_result_is_recorded_and_a_rejected_one_is_visible(store: Store) -> None:
    """Отклонённый скан обязан остаться: по нему видно, где берут не то."""
    session_id = run(store.open_session(
        actor_id=uid(), station_id=uid(),
        picklist_barcode=f"PL-{uuid.uuid4().hex[:10].upper()}"))
    task = _task(uid())
    run(store.add_lines(session_id, [task]))

    run(store.record_scan(session_id=session_id, task_id=task.task_id,
                          stage="rack", barcode="9999999999999",
                          scan_result="wrong_barcode", accepted=False,
                          actor_id="picker-1", station_id=None))

    rejected = run(store.rejected_scans(limit=50))
    assert any(row["task_id"] == task.task_id for row in rejected), (
        f"отклонённый скан не сохранён: {store.last_error}")


# ------------------------------------------------------------------- печать

def test_a_print_is_started_finished_and_readable(store: Store) -> None:
    key, task_id, station = uid(), uid(), uid()
    run(store.start_print(idempotency_key=key, task_id=task_id, station_id=station,
                          label_format="zplv", checksum="a" * 64, payload_bytes=1024,
                          copies=1, reprint=False, reason=None, actor_id="picker-1"))
    run(store.finish_print(idempotency_key=key, outcome="written",
                           click_to_agent_ms=12.5, agent_write_ms=3.5))

    job = run(store.print_job(key))
    assert job and job["outcome"] == "written", f"печать не записана: {store.last_error}"
    assert float(job["agent_write_ms"]) == pytest.approx(3.5)

    assert run(store.last_print()) is not None
    stats = run(store.print_stats())
    assert stats is not None


def test_an_unknown_outcome_is_stored_as_unknown(store: Store) -> None:
    """«Неизвестно» — законный исход печати, а не ошибка схемы.

    Агент не ответил: байты ушли, и принтер мог напечатать. Схема обязана
    принимать это значение, иначе исход записывается как отказ — и человек
    печатает вторую наклейку на ту же вещь.
    """
    key = uid()
    run(store.start_print(idempotency_key=key, task_id=uid(), station_id=uid(),
                          label_format="zplv", checksum="b" * 64, payload_bytes=512,
                          copies=1, reprint=False, reason=None, actor_id=None))
    run(store.finish_print(idempotency_key=key, outcome="unknown",
                           error="агент не подтвердил запись"))

    job = run(store.print_job(key))
    assert job and job["outcome"] == "unknown", (
        f"исход «неизвестно» не сохранился: {store.last_error}")


# ------------------------------------------------------- задания и отмены

def test_a_task_note_is_upserted_not_duplicated(store: Store) -> None:
    task = _task(uid())
    station, actor = uid(), "picker-1"
    run(store.note_task(task, station_id=station, actor_id=actor))
    run(store.note_task(task, station_id=station, actor_id=actor, printed=True))
    assert store.available, f"повтор записи задания упал: {store.last_error}"


def test_a_cancellation_without_a_reason_is_counted(store: Store) -> None:
    """Счётчик отмен без причины — тот самый, что показывает 2645 в боевом."""
    run(store.record_cancel(task_id=uid(), owner_external_id="seller-1",
                            barcode="4600000000011", reason="брак: порвана упаковка",
                            reason_code="operator_damaged", actor_id="picker-1"))
    counted = run(store.cancellations_without_reason())
    assert counted is not None, f"счётчик не прочитался: {store.last_error}"


# ------------------------------------------------------------------ принтеры

def test_a_printer_is_registered_and_its_probe_is_recorded(store: Store) -> None:
    """Проверка принтера может обогнать регистрацию станции — и не потеряться."""
    station = uid()
    run(store.record_probe(station_id=station, confirmed_format="zplv",
                           note="из XP-420B вышла этикетка ZPL"))
    row = run(store.printer(station))
    assert row and row["confirmed_format"] == "zplv", (
        f"ответ на вопрос 2 раздела 13 потерян: {store.last_error}")

    # Имя станции уникально в схеме: берём своё, чтобы тест не спотыкался
    # о станцию, оставшуюся от прошлого прогона.
    name = f"Станция {uuid.uuid4().hex[:8]}"
    run(store.upsert_printer(station_id=station, station_name=name,
                             printer_name="XP-420B", transport="agent"))
    assert store.available, f"станция не зарегистрирована: {store.last_error}"
    row = run(store.printer(station))
    assert row["printer_name"] == "XP-420B"
    assert row["confirmed_format"] == "zplv", "регистрация стёрла подтверждённый формат"

    assert any(str(item["station_id"]) == station for item in run(store.printers()))


# -------------------------------------------------- поведение при отказах

def test_bad_data_does_not_throw_away_the_connection(store: Store) -> None:
    """`DataError` — это наш запрос, а не сломанное соединение.

    Выбрасывать соединение на каждой опечатке в параметрах значит
    пересоздавать пул на ровном месте — и держать экран в очереди из ждущих
    корутин.
    """
    assert run(store.execute("SELECT 1 AS ok", fetch="one", operation="probe"))
    # Заведомо плохое значение: uuid из слова.
    run(store.execute("SELECT %s::uuid AS bad", ("не-uuid",), fetch="one",
                      operation="broken"))
    assert store.available is True, (
        "плохие данные посчитаны отказом базы: экран уйдёт в деградацию на "
        "ровном месте")
    # Соединение цело — следующий запрос проходит сразу, без предохранителя.
    assert run(store.execute("SELECT 2 AS ok", fetch="one", operation="probe"))


def test_a_dead_database_is_not_retried_on_every_request() -> None:
    """Предохранитель: после отказа соединение не пробуется чаще раза в 10 с.

    Каждый запрос вставал на `connect_timeout` и держал в этом ожидании
    экран — пять экранов превращали рабочее место в очередь из ждущих корутин.
    """
    import time

    store = Store("postgresql://nobody@127.0.0.1:1/nothing", retry_after_seconds=30.0)
    started = time.monotonic()
    assert run(store.ping()) is False
    first = time.monotonic() - started

    started = time.monotonic()
    assert run(store.ping()) is False
    second = time.monotonic() - started

    assert second < first / 2 or second < 0.05, (
        f"вторая попытка заняла {second:.3f} с против {first:.3f} с — "
        f"предохранитель не сработал")
    run(store.close())


def test_the_broken_connection_check_tells_data_errors_from_outages() -> None:
    """Разница между «сломано соединение» и «плохи данные» — в типе ошибки."""
    import psycopg

    assert _is_connection_broken(psycopg.OperationalError("сеть пропала")) is True
    assert _is_connection_broken(psycopg.DataError("не uuid")) is False
    assert _is_connection_broken(psycopg.ProgrammingError("нет колонки")) is False
