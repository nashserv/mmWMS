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
        run(hub.register(AgentSession(station_id="11111111-1111-4111-8111-111111111111", station_name="Станция 1",
                                      websocket=socket)))
    return PrintService(wms.client(), hub, store, Projection()), hub, socket


def test_print_sends_raw_bytes_to_the_station_agent(wms: FakeWms, store: FakeStore):
    payload = b"^XA^FDlabel^FS^XZ"
    wms.on("/labels/a/print", lambda params: dict(
        label_result(payload), task_id="a", station_id="11111111-1111-4111-8111-111111111111",
        printer_transport="agent"))
    printing, _hub, socket = build(wms, store)

    result = run(printing.print_label(task_id="a", station_id="11111111-1111-4111-8111-111111111111", actor_id="Иванов"))
    assert result["ok"] is True
    assert result["agent_write_ms"] == 3.5
    assert socket.sent[0]["type"] == "print"
    assert base64.b64decode(socket.sent[0]["payload_b64"]) == payload, (
        "агент обязан получить те же байты, что выдал wms — без растеризации")


def test_print_never_touches_wildberries(wms: FakeWms, store: FakeStore):
    """Инвариант 9: стикер лежит локально, запрос к WB в момент упаковки запрещён."""
    wms.on("/labels/a/print", lambda params: dict(label_result(), task_id="a",
                                                  station_id="11111111-1111-4111-8111-111111111111"))
    printing, _hub, _socket = build(wms, store)
    run(printing.print_label(task_id="a", station_id="11111111-1111-4111-8111-111111111111"))
    assert [route for route, _ in wms.calls] == ["/labels/a/print"], (
        "на пути печати ровно один вызов — за локальным стикером")


def test_print_is_refused_when_the_checksum_does_not_match(wms: FakeWms, store: FakeStore):
    wms.on("/labels/a/print", lambda params: dict(
        label_result(), task_id="a", station_id="11111111-1111-4111-8111-111111111111", checksum="0" * 64))
    printing, _hub, socket = build(wms, store)
    with pytest.raises(PrintRefused):
        run(printing.print_label(task_id="a", station_id="11111111-1111-4111-8111-111111111111"))
    assert not socket.sent, "испорченный стикер до принтера не доходит"


def test_print_without_an_agent_says_so_instead_of_pretending(wms: FakeWms,
                                                              store: FakeStore):
    wms.on("/labels/a/print", lambda params: dict(label_result(), task_id="a",
                                                  station_id="11111111-1111-4111-8111-111111111111"))
    printing, _hub, _socket = build(wms, store, connected=False)
    with pytest.raises(PrintRefused) as failure:
        run(printing.print_label(task_id="a", station_id="11111111-1111-4111-8111-111111111111"))
    assert "агента печати" in str(failure.value)
    # Отказ случается ДО вызова в wms: стикер не запрашивается, печати нет, и
    # записывать нечего. Раньше стикер запрашивался, в wms проставлялась
    # печать и менялось состояние задания — а печатать его оказывалось некуда,
    # и следующая попытка выглядела перепечаткой.
    assert not store.prints, (
        "печать записана там, где её не было: стикер запрошен вслепую")
    assert not wms.calls, "wms позвали, хотя печатать некуда"


def test_a_failing_printer_is_reported_not_swallowed(wms: FakeWms, store: FakeStore):
    wms.on("/labels/a/print", lambda params: dict(label_result(), task_id="a",
                                                  station_id="11111111-1111-4111-8111-111111111111"))
    printing, _hub, _socket = build(wms, store, ok=False)
    with pytest.raises(PrintRefused):
        run(printing.print_label(task_id="a", station_id="11111111-1111-4111-8111-111111111111"))


def test_reprint_without_a_reason_is_refused_before_any_call(wms: FakeWms,
                                                             store: FakeStore):
    printing, _hub, _socket = build(wms, store)
    with pytest.raises(PrintRefused):
        run(printing.print_label(task_id="a", station_id="11111111-1111-4111-8111-111111111111", reprint=True, reason=""))
    assert not wms.calls


def test_print_records_both_halves_of_the_budget(wms: FakeWms, store: FakeStore):
    wms.on("/labels/a/print", lambda params: dict(label_result(), task_id="a",
                                                  station_id="11111111-1111-4111-8111-111111111111"))
    printing, _hub, _socket = build(wms, store)
    result = run(printing.print_label(task_id="a", station_id="11111111-1111-4111-8111-111111111111",
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
        task_id="a", station_id="11111111-1111-4111-8111-111111111111"))
    printing, _hub, socket = build(wms, store)
    result = run(printing.print_label(task_id="a", station_id="11111111-1111-4111-8111-111111111111"))
    assert result["label_format"] == "png"
    assert socket.sent[0]["format"] == "png"
    assert base64.b64decode(socket.sent[0]["payload_b64"]) == png


def test_agent_write_telemetry_is_kept_for_the_full_run(wms: FakeWms, store: FakeStore):
    """Шаг 10 прогона читает это число по PRINT_AGENT_STATS_URL."""
    hub = AgentHub()
    hub.note_write(task_id="a", station_id="11111111-1111-4111-8111-111111111111", write_ms=12.4, label_format="zplv")
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


# ------------------------------------------------- идемпотентность и исход

def test_a_double_click_prints_once(wms: FakeWms, store: FakeStore):
    """Двойной клик по кнопке — одна этикетка, а не две.

    Ключ печати содержал свежий `uid()`, и каждое нажатие было новой печатью.
    Человек, нажавший дважды, получал две этикетки на одну вещь и наклеивал
    вторую на следующую — то есть отправлял чужой заказ.
    """
    wms.on("/labels/a/print", lambda params: dict(
        label_result(), task_id="a", station_id="11111111-1111-4111-8111-111111111111"))
    printing, _hub, socket = build(wms, store)

    first = run(printing.print_label(task_id="a", station_id="11111111-1111-4111-8111-111111111111"))
    second = run(printing.print_label(task_id="a", station_id="11111111-1111-4111-8111-111111111111"))

    assert first["idempotency_key"] == second["idempotency_key"], (
        f"ключи печати разные ({first['idempotency_key']} и "
        f"{second['idempotency_key']}): для wms это две разные печати, и "
        f"человек получит вторую наклейку на ту же вещь")
    assert first["idempotency_key"].startswith("print-a-")
    assert len(store.prints) == 1, "в журнале две печати вместо одной"


def test_a_reprint_always_gets_its_own_key(wms: FakeWms, store: FakeStore):
    """Перепечатка — намеренное повторение, и считается отдельно.

    Парная проверка: стабильный ключ не должен запретить перепечатать
    зажёванную этикетку.
    """
    wms.on("/labels/a/print", lambda params: dict(
        label_result(), task_id="a", station_id="11111111-1111-4111-8111-111111111111"))
    printing, _hub, _socket = build(wms, store)

    first = run(printing.print_label(task_id="a", station_id="11111111-1111-4111-8111-111111111111"))
    again = run(printing.print_label(task_id="a", station_id="11111111-1111-4111-8111-111111111111",
                                     reprint=True, reason="ленту зажевало"))
    third = run(printing.print_label(task_id="a", station_id="11111111-1111-4111-8111-111111111111",
                                     reprint=True, reason="ленту зажевало снова"))

    assert again["idempotency_key"].startswith("reprint-a-")
    assert again["idempotency_key"] != first["idempotency_key"]
    assert again["idempotency_key"] != third["idempotency_key"], (
        "две перепечатки получили один ключ — вторая не состоится")


def test_a_silent_agent_gives_unknown_not_failed(wms: FakeWms, store: FakeStore):
    """Агент не ответил — исход неизвестен, а не «не напечатано».

    Байты ушли в сокет, и принтер мог напечатать. Сказать человеку «не
    напечаталось» значит получить вторую наклейку на ту же вещь.
    """
    from app.printing import PrintUnknown

    wms.on("/labels/a/print", lambda params: dict(
        label_result(), task_id="a", station_id="11111111-1111-4111-8111-111111111111"))

    class SilentSocket(FakeAgentSocket):
        async def send_json(self, message: dict) -> None:
            self.sent.append(message)          # подтверждения не будет

    hub = AgentHub()
    socket = SilentSocket(hub)
    run(hub.register(AgentSession(station_id="11111111-1111-4111-8111-111111111111", station_name="Станция 1",
                                  websocket=socket)))
    printing = PrintService(wms.client(), hub, store, Projection())

    import app.agent_hub as agent_hub
    previous = agent_hub.ACK_TIMEOUT_SECONDS
    agent_hub.ACK_TIMEOUT_SECONDS = 0.05
    try:
        with pytest.raises(PrintUnknown) as failure:
            run(printing.print_label(task_id="a", station_id="11111111-1111-4111-8111-111111111111"))
    finally:
        agent_hub.ACK_TIMEOUT_SECONDS = previous

    assert "могла напечататься" in str(failure.value)
    job = run(store.print_job(next(iter(store.prints))))
    assert job["outcome"] == "unknown", (
        f"исход записан как {job['outcome']}: «неизвестно» и «не напечатано» — "
        f"разные ответы человеку")
    assert hub.get("11111111-1111-4111-8111-111111111111") is None, (
        "молчащая сессия агента осталась в реестре: следующая печать уйдёт "
        "в тот же немой сокет")


def test_an_acknowledgement_from_another_station_is_refused():
    """Чужое подтверждение не закрывает нашу печать.

    Раньше `resolve` принимал ack от любой станции: агент соседнего стола мог
    закрыть чужое задание, и «напечатано» значило «кто-то сказал, что
    напечатал».
    """
    class QuietSocket(FakeAgentSocket):
        """Сам не подтверждает: подтверждения в этом тесте шлём вручную."""

        async def send_json(self, message: dict) -> None:
            self.sent.append(message)

    hub = AgentHub()
    ours, theirs = QuietSocket(hub), QuietSocket(hub)
    run(hub.register(AgentSession(station_id="11111111-1111-4111-8111-111111111111", station_name="Наша",
                                  websocket=ours)))
    run(hub.register(AgentSession(station_id="22222222-2222-4222-8222-222222222222", station_name="Соседняя",
                                  websocket=theirs)))

    async def scenario():
        sending = asyncio.create_task(hub.send_print(
            station_id="11111111-1111-4111-8111-111111111111", job_id="job-1", task_id="a",
            payload=b"^XA^XZ", label_format="zplv",
            content_type="application/x-zpl"))
        await asyncio.sleep(0)
        foreign = hub.resolve("job-1", {"ok": True, "write_ms": 1.0},
                              station_id="22222222-2222-4222-8222-222222222222")
        mine = hub.resolve("job-1", {"ok": True, "write_ms": 2.0},
                           station_id="11111111-1111-4111-8111-111111111111")
        return foreign, mine, await sending

    foreign, mine, ack = run(scenario())

    assert foreign is False, "чужая станция закрыла нашу печать"
    assert mine is True
    assert ack["write_ms"] == 2.0, "в ответе оказалось чужое подтверждение"
