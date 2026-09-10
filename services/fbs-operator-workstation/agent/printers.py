"""Как байты попадают в принтер.

Ключевое здесь — RAW. На Windows печать через обычный принт-диалог означает
графический драйвер, растеризацию и спулер: от одной до трёх секунд, которые
никаким локальным стикером не убрать. Поэтому путь один:
`OpenPrinter` → `StartDocPrinter` с типом данных `"RAW"` → `WritePrinter`.

Остальные бэкенды нужны не «на всякий случай», а по делу: стенд поднят на
Linux и принтера у него нет, но измерить путь и прогнать шаг 10 полного
прогона надо.
"""
from __future__ import annotations

import os
import socket
import time
from pathlib import Path
from typing import Protocol


class Printer(Protocol):
    def write(self, payload: bytes) -> float:
        """Записать байты. Возвращает время записи в миллисекундах."""

    def describe(self) -> str:
        ...


class WindowsRawPrinter:
    """RAW-печать мимо графического драйвера. Боевой путь на складе."""

    def __init__(self, printer_name: str) -> None:
        self.printer_name = printer_name
        import win32print  # noqa: F401 — проверяем наличие сразу, а не при первой печати
        self._win32print = win32print

    def write(self, payload: bytes) -> float:
        started = time.perf_counter()
        handle = self._win32print.OpenPrinter(self.printer_name)
        try:
            # Тип данных RAW: принтер получает наши байты как есть. Любой
            # другой тип означает драйвер и растеризацию — те самые секунды.
            self._win32print.StartDocPrinter(handle, 1, ("mmx-label", None, "RAW"))
            try:
                self._win32print.StartPagePrinter(handle)
                self._win32print.WritePrinter(handle, payload)
                self._win32print.EndPagePrinter(handle)
            finally:
                self._win32print.EndDocPrinter(handle)
        finally:
            self._win32print.ClosePrinter(handle)
        return (time.perf_counter() - started) * 1000.0

    def describe(self) -> str:
        return f"windows-raw:{self.printer_name}"


class DevicePrinter:
    """Прямая запись в устройство — `/dev/usb/lp0` и подобные."""

    def __init__(self, device_path: str) -> None:
        self.device_path = device_path

    def write(self, payload: bytes) -> float:
        started = time.perf_counter()
        # Открываем на каждую этикетку: держать дескриптор устройства смену
        # целиком — верный способ потерять принтер после первого сбоя USB.
        handle = os.open(self.device_path, os.O_WRONLY)
        try:
            written = 0
            while written < len(payload):
                written += os.write(handle, payload[written:])
        finally:
            os.close(handle)
        return (time.perf_counter() - started) * 1000.0

    def describe(self) -> str:
        return f"device:{self.device_path}"


class TcpPrinter:
    """Сетевой принтер на 9100. Сегодня не используется, но путь предусмотрен."""

    def __init__(self, host: str, port: int = 9100, timeout: float = 3.0) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout

    def write(self, payload: bytes) -> float:
        started = time.perf_counter()
        with socket.create_connection((self.host, self.port), timeout=self.timeout) as sock:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.sendall(payload)
        return (time.perf_counter() - started) * 1000.0

    def describe(self) -> str:
        return f"tcp:{self.host}:{self.port}"


class SinkPrinter:
    """Файл вместо принтера — для стенда, где принтера нет.

    Не заглушка «ничего не делаем»: байты действительно пишутся и
    сбрасываются на диск, поэтому измеренное время осмысленно, а содержимое
    можно посмотреть глазами и убедиться, что уехал именно ZPL.
    """

    def __init__(self, directory: str) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.written = 0

    def write(self, payload: bytes) -> float:
        started = time.perf_counter()
        path = self.directory / f"label-{int(time.time() * 1000)}-{self.written}.bin"
        with open(path, "wb") as sink:
            sink.write(payload)
            sink.flush()
            os.fsync(sink.fileno())
        self.written += 1
        return (time.perf_counter() - started) * 1000.0

    def describe(self) -> str:
        return f"sink:{self.directory}"


def build_printer(backend: str, target: str) -> Printer:
    """Собрать бэкенд по имени.

    `auto` выбирает Windows там, где он есть, и путь устройства там, где его
    видно. Молча свалиться в файл нельзя: «печатает, но никуда» — худший из
    возможных исходов.
    """
    backend = (backend or "auto").strip().lower()
    if backend == "windows":
        return WindowsRawPrinter(target)
    if backend == "device":
        return DevicePrinter(target)
    if backend == "tcp":
        host, _, port = target.partition(":")
        return TcpPrinter(host, int(port or 9100))
    if backend == "sink":
        return SinkPrinter(target or "./labels")
    if backend != "auto":
        raise ValueError(f"неизвестный бэкенд печати {backend!r}")

    if os.name == "nt":
        return WindowsRawPrinter(target)
    if target and Path(target).exists():
        return DevicePrinter(target)
    if ":" in target:
        host, _, port = target.partition(":")
        return TcpPrinter(host, int(port or 9100))
    raise ValueError(
        f"не понял, куда печатать: {target!r}. Укажите PRINT_AGENT_BACKEND "
        "(windows|device|tcp|sink) явно — молча писать в файл вместо принтера нельзя")
