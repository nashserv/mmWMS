"""Минимальный клиент WebSocket на стандартной библиотеке.

Агент печати живёт не на сервере, а на пяти складских ПК под Windows. Каждая
внешняя зависимость там — это установка, которую кто-то должен сделать руками и
повторить после переустановки машины. `pywin32` неизбежен: без него нет
RAW-печати. Всё остальное здесь — стандартная библиотека.

Реализовано ровно то, что нужно агенту: рукопожатие, текстовые кадры, сборка
фрагментов, ответ на ping и корректное закрытие. Расширений, сжатия и
бинарных кадров нет — они не используются.
"""
from __future__ import annotations

import base64
import json
import os
import socket
import ssl
import struct
from typing import Any
from urllib.parse import urlparse

# Максимальный кадр. Стикер в ZPL — 1–3 КБ, PNG-фолбэк по контракту до 10 МБ;
# берём с запасом, но не «сколько пришлют»: кадр на гигабайт положит агент.
MAX_FRAME_BYTES = 16 * 1024 * 1024

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONTINUATION = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA


class WebSocketError(RuntimeError):
    pass


class WebSocketClosed(WebSocketError):
    pass


class WebSocket:
    """Одно соединение. Не потокобезопасен — агент работает в один поток."""

    def __init__(self, url: str, *, timeout: float = 30.0) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in ("ws", "wss"):
            raise WebSocketError(f"схема {parsed.scheme!r} не поддерживается")
        self._host = parsed.hostname or "127.0.0.1"
        self._port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        self._path = parsed.path or "/"
        if parsed.query:
            self._path += "?" + parsed.query
        self._secure = parsed.scheme == "wss"
        self._timeout = timeout
        self._buffer = b""
        self._sock: socket.socket | None = None

    # ------------------------------------------------------------ соединение

    def connect(self) -> None:
        sock = socket.create_connection((self._host, self._port), timeout=self._timeout)
        # Nagle выключен: этикетка — маленький кадр, и склеивать его не с чем.
        # Сорок миллисекунд задержки Nagle съели бы почти весь бюджет печати.
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        if self._secure:
            context = ssl.create_default_context()
            sock = context.wrap_socket(sock, server_hostname=self._host)
        self._sock = sock
        self._handshake()

    def _handshake(self) -> None:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {self._path} HTTP/1.1\r\n"
            f"Host: {self._host}:{self._port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self._send_all(request.encode("ascii"))

        header = b""
        while b"\r\n\r\n" not in header:
            chunk = self._recv(4096)
            header += chunk
            if len(header) > 65536:
                raise WebSocketError("сервер прислал слишком длинный ответ на рукопожатие")
        head, _, rest = header.partition(b"\r\n\r\n")
        status = head.split(b"\r\n", 1)[0].decode("latin-1")
        if "101" not in status:
            raise WebSocketError(f"рукопожатие отклонено: {status}")
        import hashlib
        expected = base64.b64encode(
            hashlib.sha1((key + _GUID).encode("ascii")).digest()).decode("ascii")
        lowered = head.decode("latin-1").lower()
        if f"sec-websocket-accept: {expected.lower()}" not in lowered:
            raise WebSocketError("сервер не подтвердил ключ рукопожатия")
        self._buffer = rest

    def close(self) -> None:
        sock = self._sock
        if sock is None:
            return
        try:
            self._send_frame(OP_CLOSE, struct.pack("!H", 1000))
        except OSError:
            pass
        try:
            sock.close()
        finally:
            self._sock = None

    # ------------------------------------------------------------ ввод-вывод

    def _send_all(self, data: bytes) -> None:
        if self._sock is None:
            raise WebSocketClosed("соединение закрыто")
        self._sock.sendall(data)

    def _recv(self, size: int) -> bytes:
        if self._sock is None:
            raise WebSocketClosed("соединение закрыто")
        chunk = self._sock.recv(size)
        if not chunk:
            raise WebSocketClosed("сервер закрыл соединение")
        return chunk

    def _read_exactly(self, size: int) -> bytes:
        while len(self._buffer) < size:
            self._buffer += self._recv(max(4096, size - len(self._buffer)))
        data, self._buffer = self._buffer[:size], self._buffer[size:]
        return data

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        header = bytearray([0x80 | opcode])
        length = len(payload)
        # Клиент обязан маскировать кадры — иначе сервер закроет соединение.
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header += struct.pack("!H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", length)
        mask = os.urandom(4)
        header += mask
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self._send_all(bytes(header) + masked)

    def send_json(self, message: dict[str, Any]) -> None:
        self._send_frame(OP_TEXT, json.dumps(message, ensure_ascii=False).encode("utf-8"))

    def receive_json(self, timeout: float | None = None) -> dict[str, Any] | None:
        """Одно сообщение. None — истёк таймаут ожидания, а не разрыв."""
        if self._sock is None:
            raise WebSocketClosed("соединение закрыто")
        self._sock.settimeout(timeout if timeout is not None else self._timeout)
        try:
            payload = self._receive_message()
        except (socket.timeout, TimeoutError):
            return None
        if payload is None:
            return None
        try:
            message = json.loads(payload.decode("utf-8"))
        except ValueError as error:
            raise WebSocketError("сервер прислал не JSON") from error
        return message if isinstance(message, dict) else {"value": message}

    def _receive_message(self) -> bytes | None:
        chunks: list[bytes] = []
        while True:
            first, second = self._read_exactly(2)
            final = bool(first & 0x80)
            opcode = first & 0x0F
            masked = bool(second & 0x80)
            length = second & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._read_exactly(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read_exactly(8))[0]
            if length > MAX_FRAME_BYTES:
                raise WebSocketError(f"кадр {length} байт больше допустимого")
            mask = self._read_exactly(4) if masked else b""
            payload = self._read_exactly(length) if length else b""
            if masked:
                payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))

            if opcode == OP_CLOSE:
                raise WebSocketClosed("сервер закрыл соединение")
            if opcode == OP_PING:
                # Ответить обязательно: без pong сервер посчитает агент мёртвым
                # и оборвёт соединение посреди смены.
                self._send_frame(OP_PONG, payload)
                continue
            if opcode == OP_PONG:
                continue
            if opcode in (OP_TEXT, OP_BINARY, OP_CONTINUATION):
                chunks.append(payload)
                if final:
                    return b"".join(chunks)
                continue
            raise WebSocketError(f"неизвестный опкод {opcode}")

    def ping(self) -> None:
        self._send_frame(OP_PING, b"")
