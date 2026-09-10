"""Опрос как источник истины.

Это ядро потока B: задание попадает на экран потому, что рабочее место
сходило и спросило, а не потому, что доехало сообщение. Тесты закрывают три
вещи, на которых раньше терялись заказы: экран не чистится догадкой, шина не
пишет проекцию, а нарушение `claim: false` не проходит молча.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest
from conftest import FakeWms, pull_result, task_projection

from app.domain import TaskState, task_from_contract
from app.projection import SCREEN_STATES, Projection
from app.puller import SCREEN_ASSIGNEE, Poller
from app.wms_client import WmsUnavailable


def run(coro):
    return asyncio.run(coro)


def make(task_id: str, *, cell: str = "A-01-01", route_order: int = 10, **overrides):
    return task_from_contract(
        task_projection(task_id, **overrides),
        placements=[{"barcode": "4600000000011", "cell_address": cell,
                     "box_barcode": "BOX-1", "state": "good", "quantity": 5,
                     "route_order": route_order}])


# --- проекция --------------------------------------------------------------

def test_projection_never_drops_a_task_just_because_it_stopped_arriving():
    """Задание, пропавшее из ответа, могло не поместиться в лимит.

    Стереть его с экрана — та самая потеря заказа, ради которой всё
    переписано, только сделанная своими руками.
    """
    projection = Projection()
    projection.apply([make("a"), make("b")], available_total=2)
    projection.apply([make("a")], available_total=1)
    assert {task.task_id for task in projection.all()} == {"a", "b"}


def test_projection_marks_the_missing_for_a_by_name_check():
    projection = Projection()
    projection.apply([make("a"), make("b")])
    projection.apply([make("a")])
    stale = projection.stale_candidates(grace_seconds=0, recheck_seconds=0, limit=10)
    assert stale == ["b"]


def test_a_failed_poll_keeps_the_screen_but_marks_it_stale():
    """Устаревший экран лучше пустого — но человек обязан знать, что он устарел."""
    projection = Projection()
    projection.apply([make("a")])
    projection.note_failure("wms молчит")
    assert len(projection.all()) == 1
    snapshot = projection.snapshot()
    assert snapshot["stale"] is True and snapshot["last_error"] == "wms молчит"


def test_picklist_is_ordered_by_route_not_by_order_of_arrival():
    """Сборщик идёт змейкой по стеллажам, а не по порядку заказов."""
    projection = Projection()
    far = make("far", assignee="picker", cell="C-09-01", route_order=90)
    near = make("near", assignee="picker", cell="A-01-01", route_order=10)
    for task in (far, near):
        task.state = TaskState.PICKING
        projection.upsert(task)
    assert [task.task_id for task in projection.picklist("picker")] == ["near", "far"]


def test_screen_queue_is_ordered_by_wildberries_deadline():
    projection = Projection()
    projection.apply([
        make("late", deadline="2026-09-20T12:00:00+00:00"),
        make("soon", deadline="2026-09-11T12:00:00+00:00"),
    ])
    assert [task.task_id for task in projection.for_screen()] == ["soon", "late"]


# --- опросчик --------------------------------------------------------------

def test_poller_asks_for_the_states_the_screen_shows(wms: FakeWms):
    wms.on("/tasks/pull", lambda params: pull_result(task_projection()))
    projection = Projection()
    poller = Poller(wms.client(), projection, interval_seconds=0.01, limit=50)
    run(poller.poll_once())
    sent = wms.route_calls("/tasks/pull")[0]
    assert sent["claim"] is False
    assert set(sent["states"]) == set(SCREEN_STATES)
    assert "cancelled" not in sent["states"], "экран показывает работу, а не архив"


def test_poller_removes_a_task_only_after_the_service_confirms_it_is_done(wms: FakeWms):
    answers = {"n": 0}

    def pull(params):
        answers["n"] += 1
        return pull_result(task_projection("a")) if answers["n"] == 1 else pull_result()

    wms.on("/tasks/pull", pull)
    wms.on("/tasks/a", lambda params: task_projection("a", state="handed"))
    projection = Projection()
    poller = Poller(wms.client(), projection, interval_seconds=0.01, limit=50)
    run(poller.poll_once())
    assert len(projection.all()) == 1

    import app.puller as puller_module
    original = puller_module.MISSING_GRACE_SECONDS
    puller_module.MISSING_GRACE_SECONDS = 0.0
    try:
        run(poller.poll_once())
    finally:
        puller_module.MISSING_GRACE_SECONDS = original
    assert projection.all() == [], "терминальное задание уходит с экрана"
    assert wms.route_calls("/tasks/a"), "убирать без переспроса нельзя"


def test_poller_keeps_a_task_the_service_still_calls_active(wms: FakeWms):
    answers = {"n": 0}

    def pull(params):
        answers["n"] += 1
        return pull_result(task_projection("a")) if answers["n"] == 1 else pull_result()

    wms.on("/tasks/pull", pull)
    wms.on("/tasks/a", lambda params: task_projection("a", state="picked"))
    projection = Projection()
    poller = Poller(wms.client(), projection, interval_seconds=0.01, limit=50)
    run(poller.poll_once())
    import app.puller as puller_module
    original = puller_module.MISSING_GRACE_SECONDS
    puller_module.MISSING_GRACE_SECONDS = 0.0
    try:
        run(poller.poll_once())
    finally:
        puller_module.MISSING_GRACE_SECONDS = original
    assert [task.task_id for task in projection.all()] == ["a"]


def test_poller_notices_when_the_service_claims_on_a_read(wms: FakeWms):
    """`claim: false` обязан ничего не занимать.

    Сервис, занимающий задание на чтении экрана, за минуту разложит очередь по
    несуществующей сессии. Симптом при этом выглядит как «заданий нет» — то
    есть неотличим от той беды, которую чиним. Поэтому нарушение считается и
    называется вслух.
    """
    wms.on("/tasks/pull", lambda params: pull_result(
        task_projection("a", assignee=SCREEN_ASSIGNEE, state="picking")))
    projection = Projection()
    poller = Poller(wms.client(), projection, interval_seconds=0.01, limit=50)
    run(poller.poll_once())
    assert poller.claim_ignored is True
    assert poller.status()["claim_ignored_note"]


def test_a_conforming_service_never_raises_the_violation_flag(wms: FakeWms):
    wms.on("/tasks/pull", lambda params: pull_result(task_projection("a")))
    projection = Projection()
    poller = Poller(wms.client(), projection, interval_seconds=0.01, limit=50)
    run(poller.poll_once())
    assert poller.claim_ignored is False


def test_a_dead_service_does_not_kill_the_poll_loop(wms: FakeWms):
    wms.fail_with = httpx.ConnectError("wms лежит")
    projection = Projection()
    poller = Poller(wms.client(), projection, interval_seconds=0.01, limit=50)
    with pytest.raises(WmsUnavailable):
        run(poller.poll_once())
    assert projection.snapshot()["stale"] is True
    assert poller.polls_failed == 1


def test_bus_only_wakes_the_poll_and_never_writes_the_projection(wms: FakeWms):
    """Событие приносит повод, а не данные.

    Пока данные приходят двумя путями, они однажды разойдутся — так экран и
    начал показывать не то, что лежит в складе.
    """
    wms.on("/tasks/pull", lambda params: pull_result())
    projection = Projection()
    poller = Poller(wms.client(), projection, interval_seconds=60, limit=50)
    poller.nudge("bus")
    assert projection.all() == [], "толчок с шины ничего не добавил сам по себе"


def test_claiming_a_batch_puts_it_in_the_projection(wms: FakeWms):
    wms.on("/tasks/pull", lambda params: pull_result(
        task_projection("a", assignee="picker-1", state="picking"),
        leased_until="2026-09-10T10:15:00+00:00"))
    projection = Projection()
    poller = Poller(wms.client(), projection, interval_seconds=60, limit=50)
    tasks = run(poller.claim(assignee="picker-1", limit=5, lease_seconds=900))
    assert len(tasks) == 1
    sent = wms.route_calls("/tasks/pull")[0]
    assert sent["claim"] is True and sent["lease_seconds"] == 900
    assert projection.get("a") is not None


def test_visible_delay_is_not_measured_against_an_invented_timestamp():
    """Идеальный ноль там, где мерить нечем, — ложный зелёный.

    Критерий раздела 10 («WB → экран меньше 5 секунд») проверяется по
    `created_at` сервиса. Нет его — наблюдения нет, и это видно счётчиком
    расхождения контракта.
    """
    from app import metrics
    counter = metrics.CONTRACT_FALLBACKS.labels(route="/tasks/pull",
                                                field="created_at_missing")
    before = counter._value.get()
    histogram = metrics.TASK_VISIBLE_DELAY._sum.get()

    projection = Projection()
    naked = task_projection("no-time")
    naked.pop("created_at")
    projection.apply([task_from_contract(naked)])

    assert counter._value.get() > before
    assert metrics.TASK_VISIBLE_DELAY._sum.get() == histogram, (
        "наблюдение записано по выдуманному времени")


def test_visible_delay_is_measured_when_the_service_reports_creation_time():
    from datetime import datetime, timedelta, timezone

    from app import metrics
    before = metrics.TASK_VISIBLE_DELAY._sum.get()
    born = (datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat()
    projection = Projection()
    projection.apply([make("timed", created_at=born)])
    assert metrics.TASK_VISIBLE_DELAY._sum.get() > before


def test_the_workaround_never_hands_one_task_to_two_pickers(wms: FakeWms):
    """Обход нарушения контракта не имеет права нарушить главное правило склада.

    Задание, отданное сборщику в обход, обязано остаться за ним и после
    следующего опроса: иначе двое пойдут за одной вещью.
    """
    claimed = task_projection("a", assignee=SCREEN_ASSIGNEE, state="picking")
    wms.on("/tasks/pull", lambda params: pull_result(
        claimed, leased_until="2026-09-10T10:15:00+00:00"))
    wms.on("/tasks/a", lambda params: claimed)
    projection = Projection()
    poller = Poller(wms.client(), projection, interval_seconds=60, limit=50)
    run(poller.poll_once())
    assert poller.claim_ignored is True

    first = run(poller.adopt_screen_claimed(assignee="picker-1", limit=5))
    assert [task.task_id for task in first] == ["a"]

    # Следующий опрос приносит то же задание с прежним assignee сервиса.
    run(poller.poll_once())
    assert projection.get("a").assignee == "picker-1", "хозяин задания не должен теряться"

    second = run(poller.adopt_screen_claimed(assignee="picker-2", limit=5))
    assert second == [], "второму сборщику то же задание не достаётся"
