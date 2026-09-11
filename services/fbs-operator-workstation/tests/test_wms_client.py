"""Клиент контракта wms.

Проверяется то, из-за чего на складе отгружают чужую вещь: подмена владельца,
чужой стикер, испорченная этикетка. Каждая из этих проверок стоит в коде
потому, что приложение C мастера требует её от клиента, а не от сервера.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib

import httpx
import pytest
from conftest import FakeWms, label_result, pull_result, task_projection

from app.domain import ScanResult, TaskState
from app.wms_client import (BASE_PATH, LabelUnusable, WmsRejected, WmsUnavailable,
                            decode_label_payload)


def run(coro):
    return asyncio.run(coro)


def test_base_url_gets_contract_prefix_once():
    """Переключение с mock на поток A — смена URL, и она не должна ломать путь."""
    from app.wms_client import WmsClient
    assert WmsClient("http://wms:8080").base_url == "http://wms:8080" + BASE_PATH
    assert WmsClient("http://wms:8080/").base_url == "http://wms:8080" + BASE_PATH
    assert WmsClient(f"http://wms:8080{BASE_PATH}").base_url == "http://wms:8080" + BASE_PATH


def test_pull_reads_contract_shape(wms: FakeWms):
    wms.on("/tasks/pull", lambda params: pull_result(task_projection()))
    batch = run(wms.client().tasks_pull(assignee="picker", limit=10))
    assert len(batch.tasks) == 1
    task = batch.tasks[0]
    assert task.task_id == "task-1"
    assert task.state is TaskState.RESERVED
    assert task.cell_address == "A-01-01"
    assert task.box_barcode == "BOX-1"
    assert task.label_ready is True
    assert batch.served_at == "2026-09-10T10:00:00+00:00"
    assert batch.available_total == 1


def test_screen_poll_never_asks_to_claim(wms: FakeWms):
    """Экран обновляется чаще, чем человек берёт работу.

    Занимать задание на каждом обновлении — значит разложить всю очередь по
    пустым сессиям и оставить сборщиков без работы.
    """
    wms.on("/tasks/pull", lambda params: pull_result())
    run(wms.client().tasks_pull(assignee="screen", limit=10, claim=False))
    assert wms.route_calls("/tasks/pull")[0]["claim"] is False


def test_claiming_pull_is_never_retried(wms: FakeWms):
    """Повтор занятия занял бы вторую пачку заданий, а первая осталась бы висеть."""
    attempts = {"n": 0}

    def flaky(params):
        attempts["n"] += 1
        raise httpx.ConnectError("сеть моргнула")

    wms.on("/tasks/pull", flaky)
    with pytest.raises(WmsUnavailable):
        run(wms.client().tasks_pull(assignee="picker", limit=5, claim=True))
    assert attempts["n"] == 1, "занятие заданий повторять нельзя"


def test_read_pull_is_retried(wms: FakeWms):
    """Чтение экрана повторить можно и нужно: оно ничего не занимает."""
    attempts = {"n": 0}

    def flaky(params):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise httpx.ConnectError("сеть моргнула")
        return pull_result(task_projection())

    wms.on("/tasks/pull", flaky)
    batch = run(wms.client().tasks_pull(assignee="screen", limit=5, claim=False))
    assert len(batch.tasks) == 1 and attempts["n"] == 2


def test_scan_rejects_a_foreign_owner_even_when_status_is_picked(wms: FakeWms):
    """Инвариант 6: перепутанный владелец — это отгрузка чужой вещи."""
    wms.on("/tasks/1/scan", lambda params: {"status": "picked", "scan_result": "ok",
                                            "owner_external_id": "seller-2"})
    accepted, result, _ = run(wms.client().scan("1", "4600000000011",
                                                expected_owner="seller-1"))
    assert accepted is False
    assert result is ScanResult.WRONG_OWNER


def test_scan_accepts_only_picked(wms: FakeWms):
    wms.on("/tasks/1/scan", lambda params: {"status": "rejected",
                                            "scan_result": "wrong_barcode",
                                            "owner_external_id": "seller-1"})
    accepted, result, _ = run(wms.client().scan("1", "999", expected_owner="seller-1"))
    assert accepted is False and result is ScanResult.WRONG_BARCODE


def test_application_refusal_lives_in_result_not_in_error(wms: FakeWms):
    """Контракт: прикладной отказ приезжает в result с error_code."""
    wms.on("/tasks/1/pack", lambda params: {"error_code": "INSUFFICIENT_STOCK"})
    with pytest.raises(WmsRejected) as failure:
        run(wms.client().pack("1", idempotency_key="k1"))
    assert failure.value.code == "INSUFFICIENT_STOCK"


def test_jsonrpc_error_is_transport_failure(wms: FakeWms):
    def broken(params):
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1,
                                         "error": {"code": -32000, "message": "внутри всё плохо"}})

    wms.on("/tasks/1/pack", broken)
    with pytest.raises(WmsUnavailable):
        run(wms.client().pack("1", idempotency_key="k1"))


# --- этикетка -------------------------------------------------------------

def test_label_payload_is_read_as_base64_per_contract():
    payload = b"^XA^FDhello^FS^XZ"
    body, how = decode_label_payload(base64.b64encode(payload).decode(),
                                     hashlib.sha256(payload).hexdigest())
    assert body == payload and how == "base64"


def test_label_payload_falls_back_to_plain_text_when_checksum_says_so():
    """Заглушка потока 0 отдаёт ZPL текстом. Решает контрольная сумма, а не вид строки."""
    payload = "^XA^FDhello^FS^XZ"
    body, how = decode_label_payload(payload,
                                     hashlib.sha256(payload.encode()).hexdigest())
    assert body == payload.encode() and how == "literal"


def test_label_with_a_broken_checksum_is_refused():
    """Испорченный стикер хуже ненапечатанного: он уедет не тому покупателю."""
    with pytest.raises(LabelUnusable):
        decode_label_payload(base64.b64encode(b"^XA^XZ").decode(), "0" * 64)


def test_label_for_a_foreign_order_is_refused(wms: FakeWms):
    wms.on("/tasks/1/label", lambda params: label_result(order_id=999999))
    with pytest.raises(LabelUnusable) as failure:
        run(wms.client().label("1", expected_order_id=123456))
    assert "999999" in str(failure.value)


def test_label_format_comes_from_the_service_not_from_a_local_guess(wms: FakeWms):
    """Вопрос 2 раздела 13 открыт: печатаем то, что прислали, а не что ждали."""
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 16
    wms.on("/tasks/1/label",
           lambda params: label_result(png, content_type="image/png", format="png"))
    label = run(wms.client().label("1"))
    assert label.label_format == "png" and label.is_raster and not label.is_zpl


def test_reprint_always_carries_a_reason(wms: FakeWms):
    """Без причины счётчик перепечаток нечем разбирать."""
    wms.on("/labels/1/print", lambda params: dict(label_result(),
                                                  task_id="1", station_id="s1"))
    run(wms.client().print_label("1", station_id="s1", idempotency_key="k",
                                 reprint=True, reason=None))
    sent = wms.route_calls("/labels/1/print")[0]
    assert sent["reprint"] is True
    assert sent["reason"], "перепечатка без причины уходить на сервер не должна"


def test_cancel_uses_the_frozen_request_shape(wms: FakeWms):
    wms.on("/tasks/1/cancel", lambda params: {"status": "cancelled"})
    run(wms.client().cancel("1", cancellation_event_id="evt-1", handed_over=False,
                            reason="operator_short: пусто на полке"))
    sent = wms.route_calls("/tasks/1/cancel")[0]
    assert sent["cancellation_event_id"] == "evt-1"
    assert sent["handed_over"] is False


def test_storage_lookup_is_sorted_by_route_order(wms: FakeWms):
    wms.on("/storage/lookup", lambda params: {"placements": [
        {"barcode": "b", "cell_address": "C-03", "state": "good", "quantity": 1,
         "route_order": 30},
        {"barcode": "b", "cell_address": "A-01", "state": "good", "quantity": 1,
         "route_order": 10},
    ]})
    rows = run(wms.client().storage_lookup(seller_external_id="seller-1", barcode="b"))
    assert [row.cell_address for row in rows] == ["A-01", "C-03"]


# ------------------------------------------- отказ по существу и сбой сервиса

def test_invalid_params_is_a_refusal_not_an_outage(wms: FakeWms):
    """`-32602` — отказ, адресованный человеку, а не сбой сервиса.

    Раньше он превращался в `WmsUnavailable`: экран показывал «wms
    недоступен», и сборщик ждал починки сервиса, который работал. В отказе при
    этом словами написано, что не так.
    """
    wms.on_error("/tasks/a/scan", code=-32602,
                 message="команда 'scan' не выполняется из состояния 'packed'")
    client = wms.client()

    with pytest.raises(WmsRejected) as refused:
        run(client.call("/tasks/a/scan", {"barcode": "4600000000011"}))

    assert "не выполняется из состояния" in str(refused.value), (
        "текст отказа потерян — человеку нечего показать")


def test_a_real_outage_is_still_an_outage(wms: FakeWms):
    """Парная проверка: внутренний сбой остаётся сбоем сервиса."""
    wms.on_error("/tasks/a/scan", code=-32603,
                 message="внутренняя ошибка, request_id=0123456789ab")
    client = wms.client()

    with pytest.raises(WmsUnavailable):
        run(client.call("/tasks/a/scan", {"barcode": "4600000000011"}))


def test_a_task_wms_does_not_know_reads_as_absent(wms: FakeWms):
    """«Задания нет» — законный ответ, а не сбой.

    Задание могли отменить, пока мы про него спрашивали. Экран обязан убрать
    его, а не оставить ждать возвращения сервиса.
    """
    wms.on_error("/tasks/ghost", code=-32602, message="задание ghost не найдено")
    assert run(wms.client().task("ghost")) is None
