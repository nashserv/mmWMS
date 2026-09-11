"""Подбор, контрольный скан и отмена.

Контрольный скан — главный рубеж качества процесса (раздел 4 мастера):
единственное место, где проверяется, что в коробке лежит то, что должно.
Поэтому его отказ проверяется не «возвращает поле», а «упаковка остановлена».
"""
from __future__ import annotations

import asyncio

import pytest
from conftest import FakeStore, FakeWms, pull_result, task_projection

from app.picking import PickingRefused, PickingService
from app.projection import Projection
from app.puller import Poller


def run(coro):
    return asyncio.run(coro)


def build(wms: FakeWms, store: FakeStore):
    projection = Projection()
    client = wms.client()
    poller = Poller(client, projection, interval_seconds=60, limit=50)
    return PickingService(client, projection, store, poller), projection


def test_session_writes_a_picklist_with_addresses_and_a_barcode(wms: FakeWms,
                                                                store: FakeStore):
    wms.on("/tasks/pull", lambda params: pull_result(
        task_projection("a", assignee="Иванов", state="picking"),
        leased_until="2026-09-10T10:15:00+00:00"))
    picking, _ = build(wms, store)
    result = run(picking.start_session(actor_id="Иванов", station_id=None, limit=5,
                                       lease_seconds=900))
    assert result["picklist_barcode"].startswith("PL-")
    lines = run(store.session_lines(result["session_id"]))
    assert lines[0]["cell_address"] == "A-01-01"
    assert lines[0]["box_barcode"] == "BOX-1"


def test_empty_queue_does_not_create_a_paper_sheet(wms: FakeWms, store: FakeStore):
    """Пустой лист подбора — мусор на столе, а не сессия."""
    wms.on("/tasks/pull", lambda params: pull_result())
    picking, _ = build(wms, store)
    result = run(picking.start_session(actor_id="Иванов", station_id=None, limit=5,
                                       lease_seconds=900))
    assert result["session_id"] is None
    assert store.sessions == {}


def test_missing_addresses_are_filled_from_the_contract_not_left_blank(wms: FakeWms,
                                                                      store: FakeStore):
    """Строка листа без адреса отправляет сборщика искать вещь глазами."""
    bare = {"tasks": [{"task": task_projection("a", assignee="Иванов", state="picking"),
                       "leased_until": None}],
            "served_at": "2026-09-10T10:00:00+00:00", "available_total": 1}
    wms.on("/tasks/pull", lambda params: bare)
    wms.on("/storage/lookup", lambda params: {"placements": [
        {"barcode": "4600000000011", "cell_address": "B-02-07", "box_barcode": "BOX-9",
         "state": "good", "quantity": 3, "route_order": 42}]})
    picking, _ = build(wms, store)
    result = run(picking.start_session(actor_id="Иванов", station_id=None, limit=5,
                                       lease_seconds=900))
    lines = run(store.session_lines(result["session_id"]))
    assert lines[0]["cell_address"] == "B-02-07"


def test_control_scan_mismatch_stops_packing_and_never_calls_wms(wms: FakeWms,
                                                                 store: FakeStore):
    wms.on("/tasks/pull", lambda params: pull_result(
        task_projection("a", assignee="Иванов", state="picked")))
    picking, _ = build(wms, store)
    run(picking.start_session(actor_id="Иванов", station_id=None, limit=5, lease_seconds=900))

    result = run(picking.pack(task_id="a", control_barcode="9999999999999",
                              actor_id="Иванов"))
    assert result["accepted"] is False
    assert result["stop"] is True
    assert result["expected_barcode"] == "4600000000011"
    assert not wms.route_calls("/tasks/a/pack"), (
        "при несовпадении упаковка не должна доезжать до wms")
    rejected = run(store.rejected_scans())
    assert rejected and rejected[0]["stage"] == "control"


def test_control_scan_match_packs_and_records_the_scan(wms: FakeWms, store: FakeStore):
    wms.on("/tasks/pull", lambda params: pull_result(
        task_projection("a", assignee="Иванов", state="picked")))
    wms.on("/tasks/a/pack", lambda params: {"task_id": "a", "owner_external_id": "seller-1",
                                            "state": "packed", "version": 2})
    wms.on("/tasks/a", lambda params: task_projection("a", state="packed"))
    picking, _ = build(wms, store)
    run(picking.start_session(actor_id="Иванов", station_id=None, limit=5, lease_seconds=900))

    result = run(picking.pack(task_id="a", control_barcode="4600000000011",
                              actor_id="Иванов", box_barcode="TRBX-1"))
    assert result["accepted"] is True
    sent = wms.route_calls("/tasks/a/pack")[0]
    assert sent["control_scan_barcode"] == "4600000000011", (
        "контрольный штрихкод обязан уехать на сервер: экран можно обойти")
    assert sent["box_barcode"] == "TRBX-1"
    assert sent["idempotency_key"]


def test_packing_stops_when_the_service_answers_about_another_owner(wms: FakeWms,
                                                                   store: FakeStore):
    wms.on("/tasks/pull", lambda params: pull_result(
        task_projection("a", assignee="Иванов", state="picked")))
    wms.on("/tasks/a/pack", lambda params: {"task_id": "a", "owner_external_id": "seller-2",
                                            "state": "packed"})
    picking, _ = build(wms, store)
    run(picking.start_session(actor_id="Иванов", station_id=None, limit=5, lease_seconds=900))
    with pytest.raises(PickingRefused) as failure:
        run(picking.pack(task_id="a", control_barcode="4600000000011", actor_id="Иванов"))
    assert "seller-2" in str(failure.value)


def test_packing_without_a_control_scan_is_refused(wms: FakeWms, store: FakeStore):
    wms.on("/tasks/pull", lambda params: pull_result(
        task_projection("a", assignee="Иванов", state="picked")))
    picking, _ = build(wms, store)
    run(picking.start_session(actor_id="Иванов", station_id=None, limit=5, lease_seconds=900))
    with pytest.raises(PickingRefused):
        run(picking.pack(task_id="a", control_barcode="   ", actor_id="Иванов"))


def test_a_lost_scan_reply_is_recovered_by_rereading_the_task(wms: FakeWms,
                                                              store: FakeStore):
    """Правило приложения C: перечитать задание и принять, если оно уже picked."""
    import httpx

    wms.on("/tasks/pull", lambda params: pull_result(
        task_projection("a", assignee="Иванов", state="picking")))
    picking, _ = build(wms, store)
    run(picking.start_session(actor_id="Иванов", station_id=None, limit=5, lease_seconds=900))

    def lost(params):
        raise httpx.ReadTimeout("ответ потерялся")

    wms.on("/tasks/a/scan", lost)
    wms.on("/tasks/a", lambda params: task_projection("a", state="picked"))
    result = run(picking.scan_at_rack(task_id="a", barcode="4600000000011",
                                      actor_id="Иванов"))
    assert result["accepted"] is True


def test_cancel_requires_a_reason_from_the_closed_list(wms: FakeWms, store: FakeStore):
    wms.on("/tasks/pull", lambda params: pull_result(task_projection("a")))
    wms.on("/tasks/a/cancel", lambda params: {"status": "cancelled"})
    picking, projection = build(wms, store)
    run(picking._poller.poll_once())

    with pytest.raises(PickingRefused):
        run(picking.cancel(task_id="a", reason_code="потому что", comment="ну так",
                           actor_id="Иванов"))
    with pytest.raises(PickingRefused):
        run(picking.cancel(task_id="a", reason_code="operator_short", comment="  ",
                           actor_id="Иванов"))
    assert not wms.route_calls("/tasks/a/cancel"), "отмена без причины до wms не доходит"


def test_cancel_with_a_reason_records_both_code_and_words(wms: FakeWms, store: FakeStore):
    wms.on("/tasks/pull", lambda params: pull_result(task_projection("a")))
    wms.on("/tasks/a/cancel", lambda params: {"status": "cancelled"})
    picking, projection = build(wms, store)
    run(picking._poller.poll_once())

    result = run(picking.cancel(task_id="a", reason_code="operator_short",
                                comment="на полке A-01-01 пусто", actor_id="Иванов"))
    assert result["cancel_reason_code"] == "operator_short"
    assert "пусто" in result["cancel_reason"]
    assert store.tasks["a"]["cancelled"] is True
    assert projection.get("a") is None, "отменённое задание уходит с экрана"


def test_cancel_is_idempotent_by_a_stable_event_id(wms: FakeWms, store: FakeStore):
    """Повтор отмены не должен превращаться во вторую отмену."""
    wms.on("/tasks/pull", lambda params: pull_result(task_projection("a")))
    wms.on("/tasks/a/cancel", lambda params: {"status": "cancelled"})
    picking, _ = build(wms, store)
    run(picking._poller.poll_once())
    run(picking.cancel(task_id="a", reason_code="wb_cancelled", comment="отменил WB",
                       actor_id="Иванов"))
    first = wms.route_calls("/tasks/a/cancel")[0]["cancellation_event_id"]
    assert first == "ws-cancel-a"


def test_return_to_shelf_demands_an_address(wms: FakeWms, store: FakeStore):
    """Без адреса товар «возвращается» в никуда, и остаток снова начинает врать."""
    picking, _ = build(wms, store)
    with pytest.raises(PickingRefused):
        run(picking.return_to_shelf(task_id="a", cell_address="", actor_id="Иванов"))


# --------------------------------------------------- база рабочего места лежит

class BrokenStore(FakeStore):
    """База не отвечает. Экран обязан работать и так (пункт 14 прогона)."""

    def __init__(self) -> None:
        super().__init__()
        self.available = False
        self.last_error = "OperationalError: connection refused"

    async def ping(self) -> bool:
        return False

    async def open_session(self, **_kwargs):
        return None


def test_a_broken_store_does_not_take_tasks_nobody_can_pick(wms: FakeWms):
    """База лежит — задания НЕ занимаются.

    Раньше они занимались, а сессия не заводилась: лист подбора оказывался
    пустым, сборщик уходил ни с чем, а задания оставались за ним на срок
    лизинга — пятнадцать минут очередь была пуста для всех, включая его.
    """
    wms.on("/tasks/pull", lambda params: pull_result([task_projection("a")]))
    picking, _poller = build(wms, BrokenStore())

    with pytest.raises(PickingRefused) as refused:
        run(picking.start_session(actor_id="picker-1", station_id="11111111-1111-4111-8111-111111111111",
                                  limit=10, lease_seconds=900))

    assert "база рабочего места недоступна" in str(refused.value)
    assert not wms.calls, (
        "задания заняты при недоступной базе: они пропадут на срок лизинга")
