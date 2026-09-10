"""Агент печати станции.

Живёт на складском ПК рядом с USB-принтером. Держит постоянное соединение с
рабочим местом и печатает то, что ему присылают — без опроса, без спулера, без
графического драйвера.

Запуск:

    python print_agent.py --url ws://workstation:8080/api/workstation/v1/agent \\
                          --station-id 0a00...000a --station-name "Станция 1" \\
                          --printer "XP-420B"

На стенде, где принтера нет:

    python print_agent.py --url ws://127.0.0.1:8080/api/workstation/v1/agent \\
                          --station-id ... --backend sink --printer ./labels

Телеметрия записи в устройство отдаётся по `--stats-port` (по умолчанию 8091):
это `PRINT_AGENT_STATS_URL` шага 10 полного прогона. Никто, кроме агента, это
время измерить не может.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

# Агент запускается и как модуль пакета, и как одиночный файл, скопированный на
# складской ПК. Второе — основной способ, поэтому импорт умеет оба варианта.
try:
    from .printers import Printer, build_printer
    from .raster import TSPL_PROBE, ZPL_PROBE, png_to_tspl, png_to_zpl
    from .ws import WebSocket, WebSocketClosed, WebSocketError
except ImportError:  # pragma: no cover — путь одиночного файла
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from printers import Printer, build_printer  # type: ignore
    from raster import TSPL_PROBE, ZPL_PROBE, png_to_tspl, png_to_zpl  # type: ignore
    from ws import WebSocket, WebSocketClosed, WebSocketError  # type: ignore

import base64

logger = logging.getLogger("print-agent")

HEARTBEAT_SECONDS = 20.0
RECONNECT_SECONDS = 3.0

ZPL_FORMATS = {"zplv", "zplh", "zpl"}


class Stats:
    """Последняя запись в устройство. Читается шагом 10 полного прогона."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.data: dict[str, Any] = {"task_id": None, "last_write_ms": None,
                                     "writes": 0, "failures": 0}

    def note(self, **fields: Any) -> None:
        with self._lock:
            self.data.update(fields)

    def bump(self, key: str) -> None:
        with self._lock:
            self.data[key] = int(self.data.get(key) or 0) + 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self.data)


class PrintAgent:
    def __init__(self, *, url: str, station_id: str, station_name: str,
                 printer: Printer, confirmed_format: str | None = None,
                 stats: Stats | None = None) -> None:
        self.url = url
        self.station_id = station_id
        self.station_name = station_name
        self.printer = printer
        # Что принтер реально понимает. Пока не проверено на живом принтере —
        # None, и агент печатает то, что прислали, ничего не додумывая
        # (раздел 13, вопрос 2 мастера).
        self.confirmed_format = confirmed_format
        self.stats = stats or Stats()
        self._socket: WebSocket | None = None
        self._stopping = False

    # ------------------------------------------------------------ цикл

    def run_forever(self) -> None:
        while not self._stopping:
            try:
                self._session()
            except WebSocketClosed as error:
                logger.warning("соединение закрыто: %s", error)
            except (WebSocketError, OSError) as error:
                logger.warning("нет связи с рабочим местом (%s), повтор через %.0f с",
                               error, RECONNECT_SECONDS)
            if not self._stopping:
                time.sleep(RECONNECT_SECONDS)

    def stop(self) -> None:
        self._stopping = True
        if self._socket is not None:
            self._socket.close()

    def _session(self) -> None:
        socket_client = WebSocket(self.url)
        socket_client.connect()
        self._socket = socket_client
        socket_client.send_json({
            "type": "hello",
            "station_id": self.station_id,
            "station_name": self.station_name,
            "printer_name": self.printer.describe(),
            "transport": "agent",
            "confirmed_format": self.confirmed_format,
            "capabilities": {"raw": True, "png_to_zpl": True, "png_to_tspl": True},
        })
        logger.info("подключён к %s как станция %s (%s)",
                    self.url, self.station_name, self.printer.describe())

        last_beat = time.monotonic()
        while not self._stopping:
            message = socket_client.receive_json(timeout=1.0)
            now = time.monotonic()
            if message is not None:
                self._handle(socket_client, message)
            if now - last_beat >= HEARTBEAT_SECONDS:
                # Сердцебиение нужно не соединению, а наблюдаемости: молчащий
                # агент обязан отличаться от работающего (инвариант 14).
                socket_client.send_json({"type": "heartbeat",
                                         "station_id": self.station_id})
                last_beat = now

    # ------------------------------------------------------------ сообщения

    def _handle(self, socket_client: WebSocket, message: dict[str, Any]) -> None:
        kind = str(message.get("type") or "")
        if kind == "print":
            self._print(socket_client, message)
        elif kind == "probe":
            self._probe(socket_client)
        elif kind == "format":
            # Человек подтвердил у принтера, что именно вышло. С этого момента
            # агент знает, перекладывать ли растр перед печатью.
            self.confirmed_format = str(message.get("confirmed_format") or "") or None
            logger.info("формат станции подтверждён: %s", self.confirmed_format)
        elif kind in ("welcome", "pong"):
            return
        else:
            logger.debug("неизвестное сообщение %s", kind)

    def _print(self, socket_client: WebSocket, message: dict[str, Any]) -> None:
        job_id = str(message.get("job_id") or "")
        task_id = str(message.get("task_id") or "")
        label_format = str(message.get("format") or "").lower()
        copies = max(1, min(10, int(message.get("copies") or 1)))
        try:
            payload = base64.b64decode(str(message.get("payload_b64") or ""), validate=True)
        except Exception as error:  # noqa: BLE001
            self._fail(socket_client, job_id, task_id, f"тело этикетки не декодируется: {error}")
            return
        if not payload:
            self._fail(socket_client, job_id, task_id, "пустая этикетка")
            return

        try:
            prepared = self._prepare(payload, label_format)
        except Exception as error:  # noqa: BLE001
            self._fail(socket_client, job_id, task_id, str(error))
            return

        try:
            total_ms = 0.0
            for _ in range(copies):
                total_ms += self.printer.write(prepared)
        except Exception as error:  # noqa: BLE001 — принтер отвалился, агент нет
            self._fail(socket_client, job_id, task_id, f"принтер не принял байты: {error}")
            return

        self.stats.note(task_id=task_id, last_write_ms=round(total_ms, 3),
                        label_format=label_format, station_id=self.station_id,
                        at=time.time(), ok=True)
        self.stats.bump("writes")
        socket_client.send_json({
            "type": "result", "job_id": job_id, "task_id": task_id, "ok": True,
            "write_ms": round(total_ms, 3), "format": label_format,
            "bytes": len(prepared),
        })
        logger.info("напечатано задание %s за %.1f мс (%s, %d байт)",
                    task_id, total_ms, label_format, len(prepared))

    def _prepare(self, payload: bytes, label_format: str) -> bytes:
        """Привести этикетку к тому, что понимает этот принтер.

        Оба пути заложены заранее: пришёл ZPL и принтер понимает ZPL — печатаем
        как есть; пришёл PNG — растр перекладывается в команду того языка,
        который принтер подтвердил.
        """
        confirmed = (self.confirmed_format or "").lower()

        if label_format in ZPL_FORMATS:
            if confirmed == "tspl":
                # Единственный тупик: ZPL нельзя перерисовать, его нужно
                # исполнить. Формат меняется на стороне потока A.
                raise RuntimeError(
                    "принтер станции понимает только TSPL, а стикер пришёл в ZPL. "
                    "Нужен WB_STICKER_FORMAT=png на стороне wms (раздел 13, вопрос 2)")
            return payload

        if label_format == "png":
            if confirmed == "tspl":
                return png_to_tspl(payload)
            # ZPL подтверждён или ещё не проверен: ^GFA понимают и настоящие
            # ZPL-принтеры, и эмуляция.
            return png_to_zpl(payload)

        if label_format == "svg":
            raise RuntimeError(
                "стикер пришёл в SVG: агент его не рисует. Формат должен быть "
                "zplv или png (WB_STICKER_FORMAT на стороне wms)")

        # Формат неизвестен — печатаем как есть. Принтер либо поймёт, либо нет,
        # но додумывать за wms агент не имеет права.
        return payload

    def _probe(self, socket_client: WebSocket) -> None:
        """Тест на живом принтере: печатаем обе этикетки, ZPL и TSPL.

        Какая из них вышла из принтера, знает только человек, стоящий рядом.
        Поэтому агент не делает вид, что определил формат сам, — он печатает
        оба теста и говорит, что ответ за человеком.
        """
        results: dict[str, Any] = {}
        for name, probe in (("zpl", ZPL_PROBE), ("tspl", TSPL_PROBE)):
            try:
                results[name] = round(self.printer.write(probe), 3)
            except Exception as error:  # noqa: BLE001
                results[name] = f"ошибка: {error}"
        socket_client.send_json({
            "type": "probe_result",
            "station_id": self.station_id,
            "confirmed_format": None,
            "note": ("напечатаны два теста: ZPL и TSPL. Какая этикетка вышла — "
                     f"подтверждает человек. Времена записи: {json.dumps(results, ensure_ascii=False)}"),
        })
        logger.info("тест принтера отправлен: %s", results)

    def _fail(self, socket_client: WebSocket, job_id: str, task_id: str, error: str) -> None:
        self.stats.bump("failures")
        self.stats.note(task_id=task_id, ok=False, error=error, at=time.time())
        socket_client.send_json({"type": "result", "job_id": job_id, "task_id": task_id,
                                 "ok": False, "error": error})
        logger.error("печать не удалась: %s", error)


def start_stats_server(stats: Stats, port: int) -> HTTPServer:
    """HTTP с одним числом — временем последней записи в устройство."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 — имя задано базовым классом
            body = json.dumps(stats.snapshot(), ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("content-type", "application/json; charset=utf-8")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            return

    server = HTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, name="agent-stats", daemon=True).start()
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Агент печати рабочего места MM-Express")
    parser.add_argument("--url", default=os.getenv("WORKSTATION_WS_URL",
                                                   "ws://127.0.0.1:8080/api/workstation/v1/agent"))
    parser.add_argument("--station-id", default=os.getenv("STATION_ID", ""))
    parser.add_argument("--station-name", default=os.getenv("STATION_NAME", "станция"))
    parser.add_argument("--printer", default=os.getenv("PRINTER", ""),
                        help="имя принтера Windows, путь устройства, host:port или каталог")
    parser.add_argument("--backend", default=os.getenv("PRINT_AGENT_BACKEND", "auto"),
                        choices=["auto", "windows", "device", "tcp", "sink"])
    parser.add_argument("--confirmed-format", default=os.getenv("PRINTER_CONFIRMED_FORMAT") or None,
                        choices=[None, "zplv", "zplh", "tspl", "png"],
                        help="что принтер реально понял на живом тесте")
    parser.add_argument("--stats-port", type=int, default=int(os.getenv("STATS_PORT", "8091")))
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if not args.station_id:
        parser.error("--station-id обязателен: без него неизвестно, чей это принтер")

    printer = build_printer(args.backend, args.printer)
    stats = Stats()
    start_stats_server(stats, args.stats_port)
    agent = PrintAgent(url=args.url, station_id=args.station_id,
                       station_name=args.station_name, printer=printer,
                       confirmed_format=args.confirmed_format, stats=stats)
    logger.info("агент печати запущен: %s, телеметрия на :%d",
                printer.describe(), args.stats_port)
    try:
        agent.run_forever()
    except KeyboardInterrupt:
        agent.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
