"""Агент печати.

Агент — единственное место, которое знает, что понимает конкретный принтер.
Вопрос 2 раздела 13 мастера («ZPL или PNG?») до сих пор открыт, поэтому оба
пути обязаны существовать и вести себя предсказуемо, а тупик — говорить о
себе словами, а не печатать мусор.
"""
from __future__ import annotations

import base64

import pytest

from agent.print_agent import PrintAgent, Stats
from agent.printers import SinkPrinter, build_printer


class RecordingPrinter:
    def __init__(self) -> None:
        self.written: list[bytes] = []

    def write(self, payload: bytes) -> float:
        self.written.append(payload)
        return 2.5

    def describe(self) -> str:
        return "recording"


class RecordingSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    def send_json(self, message: dict) -> None:
        self.sent.append(message)


def build(confirmed: str | None = None):
    printer = RecordingPrinter()
    agent = PrintAgent(url="ws://x/y", station_id="11111111-1111-4111-8111-111111111111", station_name="Станция 1",
                       printer=printer, confirmed_format=confirmed, stats=Stats())
    return agent, printer, RecordingSocket()


def png_fixture() -> bytes:
    import struct
    import zlib

    def chunk(kind, payload):
        data = kind + payload
        return struct.pack("!I", len(payload)) + data + struct.pack("!I", zlib.crc32(data))

    ihdr = struct.pack("!IIBBBBB", 8, 1, 8, 0, 0, 0, 0)
    raw = bytes([0]) + bytes([0] * 4 + [255] * 4)
    return (b"\x89PNG\r\n\x1a\x0a" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def test_zpl_is_written_untouched():
    """Пришёл ZPL, принтер понимает ZPL — печатаем как есть, без обработки."""
    agent, printer, socket = build(confirmed="zplv")
    payload = b"^XA^FDlabel^FS^XZ"
    agent._print(socket, {"job_id": "j1", "task_id": "t1", "format": "zplv",
                          "payload_b64": base64.b64encode(payload).decode()})
    assert printer.written == [payload]
    assert socket.sent[0]["ok"] is True and socket.sent[0]["write_ms"] == 2.5


def test_png_becomes_zpl_on_a_zpl_printer():
    agent, printer, socket = build(confirmed="zplv")
    agent._print(socket, {"job_id": "j1", "task_id": "t1", "format": "png",
                          "payload_b64": base64.b64encode(png_fixture()).decode()})
    assert printer.written[0].startswith(b"^XA^FO0,0^GFA,")


def test_png_becomes_tspl_on_a_tspl_printer():
    """Тот же PNG на принтере без ZPL печатается командой TSPL."""
    agent, printer, socket = build(confirmed="tspl")
    agent._print(socket, {"job_id": "j1", "task_id": "t1", "format": "png",
                          "payload_b64": base64.b64encode(png_fixture()).decode()})
    assert printer.written[0].startswith(b"SIZE 58 mm,40 mm")
    assert b"BITMAP" in printer.written[0]


def test_zpl_on_a_tspl_only_printer_is_the_one_dead_end_and_says_so():
    """ZPL нельзя перерисовать — его нужно исполнить. Формат меняет поток A."""
    agent, printer, socket = build(confirmed="tspl")
    agent._print(socket, {"job_id": "j1", "task_id": "t1", "format": "zplv",
                          "payload_b64": base64.b64encode(b"^XA^XZ").decode()})
    assert printer.written == [], "мусор на настоящую этикетку не печатается"
    assert socket.sent[0]["ok"] is False
    assert "WB_STICKER_FORMAT=png" in socket.sent[0]["error"]


def test_an_unproven_printer_gets_the_bytes_as_they_came():
    """Пока формат не подтверждён живым принтером, агент ничего не додумывает."""
    agent, printer, socket = build(confirmed=None)
    payload = b"^XA^FDlabel^FS^XZ"
    agent._print(socket, {"job_id": "j1", "task_id": "t1", "format": "zplv",
                          "payload_b64": base64.b64encode(payload).decode()})
    assert printer.written == [payload]


def test_svg_is_refused_with_an_explanation():
    agent, printer, socket = build()
    agent._print(socket, {"job_id": "j1", "task_id": "t1", "format": "svg",
                          "payload_b64": base64.b64encode(b"<svg/>").decode()})
    assert socket.sent[0]["ok"] is False and "SVG" in socket.sent[0]["error"]


def test_copies_are_written_once_each():
    agent, printer, socket = build(confirmed="zplv")
    agent._print(socket, {"job_id": "j1", "task_id": "t1", "format": "zplv", "copies": 3,
                          "payload_b64": base64.b64encode(b"^XA^XZ").decode()})
    assert len(printer.written) == 3
    assert socket.sent[0]["write_ms"] == pytest.approx(7.5)


def test_the_probe_prints_both_tests_and_leaves_the_verdict_to_a_human():
    """Какая этикетка вышла из принтера, знает только человек рядом с ним."""
    agent, printer, socket = build()
    agent._probe(socket)
    assert len(printer.written) == 2
    assert printer.written[0].startswith(b"^XA")
    assert printer.written[1].startswith(b"SIZE")
    assert socket.sent[0]["type"] == "probe_result"
    assert socket.sent[0]["confirmed_format"] is None, (
        "агент не имеет права решить это за человека")


def test_confirmed_format_from_the_service_is_applied():
    agent, _printer, socket = build()
    agent._handle(socket, {"type": "format", "confirmed_format": "tspl"})
    assert agent.confirmed_format == "tspl"


def test_a_broken_payload_fails_the_job_not_the_agent():
    agent, printer, socket = build()
    agent._print(socket, {"job_id": "j1", "task_id": "t1", "format": "zplv",
                          "payload_b64": "не base64!!!"})
    assert socket.sent[0]["ok"] is False
    assert printer.written == []


def test_stats_carry_the_number_the_full_run_reads():
    agent, _printer, socket = build(confirmed="zplv")
    agent._print(socket, {"job_id": "j1", "task_id": "t42", "format": "zplv",
                          "payload_b64": base64.b64encode(b"^XA^XZ").decode()})
    snapshot = agent.stats.snapshot()
    assert snapshot["task_id"] == "t42"
    assert snapshot["last_write_ms"] == 2.5
    assert snapshot["writes"] == 1


def test_auto_backend_refuses_to_quietly_print_into_a_file():
    """«Печатает, но никуда» — худший из возможных исходов."""
    with pytest.raises(ValueError):
        build_printer("auto", "")


def test_sink_backend_really_writes(tmp_path):
    printer = SinkPrinter(str(tmp_path))
    elapsed = printer.write(b"^XA^XZ")
    assert elapsed >= 0
    written = list(tmp_path.iterdir())
    assert len(written) == 1 and written[0].read_bytes() == b"^XA^XZ"


# ------------------------------------------- кадр, пришедший по частям

def test_a_timeout_in_the_middle_of_a_frame_does_not_desynchronise_the_stream():
    """Таймаут посреди кадра не должен ломать поток.

    Заголовок вычитывался сразу, и таймаут между заголовком и полезной
    нагрузкой оставлял поток рассинхронизированным: следующий вызов читал
    байты этикетки как заголовок кадра, и агент видел мусорные опкоды. Для
    сборщика это выглядит как оборвавшийся принтер посреди смены.
    """
    import json

    from agent.ws import WebSocket

    body = json.dumps({"type": "print", "job_id": "j-1"}).encode("utf-8")
    frame = bytes([0x81, len(body)]) + body

    class Trickle:
        """Сокет, отдающий кадр по кусочку и с таймаутом посередине."""

        def __init__(self, data: bytes) -> None:
            self.data = data
            self.position = 0
            self.timeouts = 0

        def recv(self, size: int) -> bytes:
            if self.position == 1:
                # Таймаут ровно после первого байта заголовка.
                self.timeouts += 1
                if self.timeouts == 1:
                    raise TimeoutError()
            chunk = self.data[self.position:self.position + 1]
            self.position += len(chunk)
            return chunk

        def settimeout(self, value) -> None:
            return None

        def close(self) -> None:
            return None

    client = WebSocket.__new__(WebSocket)
    client._sock = Trickle(frame)
    client._buffer = b""
    client._chunks = []
    client._timeout = 0.01
    client._recv = lambda size: client._sock.recv(size)

    assert client.receive_json(timeout=0.01) is None, "таймаут обязан вернуть None"
    # Второй вызов дочитывает тот же кадр, а не начинает разбор с середины.
    message = None
    for _ in range(len(frame) + 5):
        message = client.receive_json(timeout=0.01)
        if message is not None:
            break
    assert message == {"type": "print", "job_id": "j-1"}, (
        f"сообщение собрано неверно: {message}. Поток рассинхронизирован — "
        f"агент читает байты этикетки как заголовок кадра")


def test_a_fragmented_message_survives_a_timeout_between_frames():
    """Недособранное сообщение живёт между вызовами, а не теряется."""
    import json

    from agent.ws import WebSocket

    body = json.dumps({"type": "print", "job_id": "j-2"}).encode("utf-8")
    half = len(body) // 2
    # Два кадра: первый без FIN, второй — продолжение с FIN.
    frames = (bytes([0x01, half]) + body[:half]
              + bytes([0x80, len(body) - half]) + body[half:])

    class Halting:
        def __init__(self, data: bytes, stop_at: int) -> None:
            self.data = data
            self.position = 0
            self.stop_at = stop_at
            self.stopped = False

        def recv(self, size: int) -> bytes:
            if self.position >= self.stop_at and not self.stopped:
                self.stopped = True
                raise TimeoutError()
            # Отдаём не больше, чем до точки остановки: иначе один `recv`
            # приносит оба кадра сразу, и таймаута посреди сообщения не
            # случается вовсе.
            limit = self.stop_at if not self.stopped else len(self.data)
            chunk = self.data[self.position:min(self.position + size, limit)]
            self.position += len(chunk)
            return chunk

        def settimeout(self, value) -> None:
            return None

    client = WebSocket.__new__(WebSocket)
    client._sock = Halting(frames, stop_at=2 + half)
    client._buffer = b""
    client._chunks = []
    client._timeout = 0.01
    client._recv = lambda size: client._sock.recv(size)

    assert client.receive_json(timeout=0.01) is None
    assert client._chunks, "первая половина сообщения потеряна на таймауте"
    assert client.receive_json(timeout=0.01) == {"type": "print", "job_id": "j-2"}
