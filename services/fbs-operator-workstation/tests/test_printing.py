"""Путь этикетки.

Бюджет раздела 10: от нажатия до записи в устройство меньше 50 мс, до движения
головки — меньше 300 мс. Поэтому проверяется не только «напечаталось», а и то,
что на этом пути нет ни вызова в Wildberries, ни записи в базу до отправки
байтов.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib

import pytest
from conftest import FakeStore, FakeWms, label_result

from app.agent_hub import AgentHub, AgentSession
from app.printing import PrintRefused, PrintService
from app.projection import Projection


def run(coro):
    return asyncio.run(coro)


class FakeAgentSocket:
    """Сокет агента: запоминает отправленное и отвечает как настоящий агент."""

    def __init__(self, hub: AgentHub, *, write_ms: float = 3.5, ok: bool = True) -> None:
        self.hub = hub
        self.sent: list[dict] = []
        self.write_ms = write_ms
        self.ok = ok

    async def send_json(self, message: dict) -> None:
        self.sent.append(message)
        if message.get("type") == "print":
            self.hub.resolve(message["job_id"],
                             {"ok": self.ok, "write_ms": self.write_ms,
                              "error": None if self.ok else "принтер не отвечает"})

    async def close(self) -> None:
        return None


def build(wms: FakeWms, store: FakeStore, *, connected: bool = True, **agent_kwargs):
    hub = AgentHub()
    socket = FakeAgentSocket(hub, **agent_kwargs)
    if connected:
        run(hub.register(AgentSession(station_id="st-1", station_name="Станция 1",
                                      websocket=socket)))
    return PrintService(wms.client(), hub, store, Projection()), hub, socket


def test_print_sends_raw_bytes_to_the_station_agent(wms: FakeWms, store: FakeStore):
    payload = b"^XA^FDlabel^FS^XZ"
    wms.on("/labels/a/print", lambda params: dict(
        label_result(payload), task_id="a", station_id="st-1",
        printer_transport="agent"))
    printing, _hub, socket = build(wms, store)

    result = run(printing.print_label(task_id="a", station_id="st-1", actor_id="Иванов"))
    assert result["ok"] is True
    assert result["agent_write_ms"] == 3.5
    assert socket.sent[0]["type"] == "print"
    assert base64.b64decode(socket.sent[0]["payload_b64"]) == payload, (
        "агент обязан получить те же байты, что выдал wms — без растеризации")


def test_print_never_touches_wildberries(wms: FakeWms, store: FakeStore):
    """Инвариант 9: стикер лежит локально, запрос к WB в момент упаковки запрещён."""
    wms.on("/labels/a/print", lambda params: dict(label_result(), task_id="a",
                                                  station_id="st-1"))
    printing, _hub, _socket = build(wms, store)
    run(printing.print_label(task_id="a", station_id="st-1"))
    assert [route for route, _ in wms.calls] == ["/labels/a/print"], (
        "на пути печати ровно один вызов — за локальным стикером")


def test_print_is_refused_when_the_checksum_does_not_match(wms: FakeWms, store: FakeStore):
    wms.on("/labels/a/print", lambda params: dict(
        label_result(), task_id="a", station_id="st-1", checksum="0" * 64))
    printing, _hub, socket = build(wms, store)
    with pytest.raises(PrintRefused):
        run(printing.print_label(task_id="a", station_id="st-1"))
    assert not socket.sent, "испорченный стикер до принтера не доходит"


def test_print_without_an_agent_says_so_instead_of_pretending(wms: FakeWms,
                                                              store: FakeStore):
    wms.on("/labels/a/print", lambda params: dict(label_result(), task_id="a",
                                                  station_id="st-1"))
    printing, _hub, _socket = build(wms, store, connected=False)
    with pytest.raises(PrintRefused) as failure:
        run(printing.print_label(task_id="a", station_id="st-1"))
    assert "агента печати" in str(failure.value)
    job = run(store.print_job(next(iter(store.prints))))
    assert job["outcome"] == "failed"


def test_a_failing_printer_is_reported_not_swallowed(wms: FakeWms, store: FakeStore):
    wms.on("/labels/a/print", lambda params: dict(label_result(), task_id="a",
                                                  station_id="st-1"))
    printing, _hub, _socket = build(wms, store, ok=False)
    with pytest.raises(PrintRefused):
        run(printing.print_label(task_id="a", station_id="st-1"))


def test_reprint_without_a_reason_is_refused_before_any_call(wms: FakeWms,
                                                             store: FakeStore):
    printing, _hub, _socket = build(wms, store)
    with pytest.raises(PrintRefused):
        run(printing.print_label(task_id="a", station_id="st-1", reprint=True, reason=""))
    assert not wms.calls


def test_print_records_both_halves_of_the_budget(wms: FakeWms, store: FakeStore):
    wms.on("/labels/a/print", lambda params: dict(label_result(), task_id="a",
                                                  station_id="st-1"))
    printing, _hub, _socket = build(wms, store)
    result = run(printing.print_label(task_id="a", station_id="st-1",
                                      idempotency_key="print-a-1"))
    job = run(store.print_job("print-a-1"))
    assert job["outcome"] == "written"
    assert job["agent_write_ms"] == 3.5
    assert result["click_to_agent_ms"] is not None


def test_a_png_label_reaches_the_agent_untouched(wms: FakeWms, store: FakeStore):
    """Растр перекладывает агент, а не сервис: обработка на сервере — лишний хоп."""
    png = b"\x89PNG\r\n\x1a\n" + b"payload"
    wms.on("/labels/a/print", lambda params: dict(
        label_result(png, content_type="image/png", format="png"),
        task_id="a", station_id="st-1"))
    printing, _hub, socket = build(wms, store)
    result = run(printing.print_label(task_id="a", station_id="st-1"))
    assert result["label_format"] == "png"
    assert socket.sent[0]["format"] == "png"
    assert base64.b64decode(socket.sent[0]["payload_b64"]) == png


def test_agent_write_telemetry_is_kept_for_the_full_run(wms: FakeWms, store: FakeStore):
    """Шаг 10 прогона читает это число по PRINT_AGENT_STATS_URL."""
    hub = AgentHub()
    hub.note_write(task_id="a", station_id="st-1", write_ms=12.4, label_format="zplv")
    assert hub.last_write["last_write_ms"] == 12.4
    assert hub.last_write["task_id"] == "a"


def test_checksum_is_compared_in_constant_time():
    """Приложение C: сверять hmac.compare_digest, а не оператором ==."""
    import inspect

    from app import wms_client
    source = inspect.getsource(wms_client.decode_label_payload)
    assert "compare_digest" in source
    payload = b"^XA^XZ"
    body, _how = wms_client.decode_label_payload(
        base64.b64encode(payload).decode(), hashlib.sha256(payload).hexdigest())
    assert body == payload
