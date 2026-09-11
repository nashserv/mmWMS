"""Соединение с агентами печати станций.

Принтеры USB, по одному на рабочее место (решение владельца 14), поэтому агент
на ПК из цепочки не убирается. Но работает он иначе, чем раньше: держит
постоянное соединение и получает задание **пушем**.

Почему не опрос. Агент, спрашивающий «есть ли что печатать» раз в секунду, —
это и есть секунда задержки, которую потом нельзя убрать ничем: ни быстрым
принтером, ни локальным стикером. Весь бюджет раздела 10 (300 мс от нажатия до
движения головки) на таком опросе не выполним в принципе.

Очередь и арбитраж не нужны: пять станций, пять принтеров, этикетки физически
не смешиваются (раздел 4 мастера). Здесь только адресация «станция → её агент».
"""
from __future__ import annotations

import asyncio
import base64
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from . import metrics

logger = logging.getLogger("workstation.agent")

# Сколько ждать подтверждения записи от агента. Больше секунды ждать нечего:
# принтер этикеток либо ответил, либо с ним что-то не так, и человеку нужно
# сказать об этом, а не крутить спиннер.
ACK_TIMEOUT_SECONDS = 2.0


@dataclass(slots=True)
class AgentSession:
    """Живое соединение с агентом одной станции."""

    station_id: str
    station_name: str
    websocket: Any
    printer_name: str | None = None
    transport: str = "agent"
    capabilities: dict[str, Any] = field(default_factory=dict)
    confirmed_format: str | None = None
    connected_at: float = field(default_factory=time.time)
    last_seen_at: float = field(default_factory=time.time)
    prints: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "station_id": self.station_id,
            "station_name": self.station_name,
            "printer_name": self.printer_name,
            "transport": self.transport,
            "confirmed_format": self.confirmed_format,
            "capabilities": self.capabilities,
            "connected_seconds": round(time.time() - self.connected_at, 1),
            "prints": self.prints,
        }


class AgentBusy(RuntimeError):
    """Агент станции не подключён или оборвал соединение.

    Значит «не напечатано»: байты до устройства не дошли.
    """


class AgentAckTimeout(AgentBusy):
    """Байты ушли, а подтверждения нет.

    Наследник `AgentBusy`, чтобы прежние обработчики продолжали ловить его, но
    отдельный тип: «не напечатано» и «неизвестно, напечаталось ли» — разные
    ответы человеку. Первый значит «нажмите ещё раз», второй — «посмотрите на
    принтер, прежде чем нажимать».
    """


class AgentHub:
    """Реестр агентов и push-доставка заданий печати."""

    def __init__(self) -> None:
        self._agents: dict[str, AgentSession] = {}
        self._waiters: dict[str, asyncio.Future[dict[str, Any]]] = {}
        # Кому какое задание отправлено. Без этой памяти `resolve` принимает
        # результат от ЛЮБОГО подключённого агента: чужая станция закрывает
        # чужую печать, и «напечатано» означает «кто-то сказал, что напечатал».
        # На складе пять станций и пять принтеров (раздел 4) — перепутать их
        # значит наклеить стикер на чужую коробку.
        self._job_owner: dict[str, str] = {}
        self._lock = asyncio.Lock()
        # Последняя запись в устройство — её читает шаг 10 полного прогона
        # через PRINT_AGENT_STATS_URL. Без этого числа утверждение «до записи в
        # устройство меньше 50 мс» проверить нечем.
        self.last_write: dict[str, Any] = {}

    # ------------------------------------------------------------ реестр

    async def register(self, session: AgentSession) -> None:
        async with self._lock:
            previous = self._agents.get(session.station_id)
            self._agents[session.station_id] = session
        if previous is not None and previous.websocket is not session.websocket:
            # Переподключение той же станции: старое соединение закрываем сами,
            # иначе push уйдёт в мёртвый сокет и молча пропадёт.
            try:
                await previous.websocket.close()
            except Exception:  # noqa: BLE001
                pass
        metrics.AGENTS_CONNECTED.set(len(self._agents))
        logger.info("агент станции %s подключён (%s)", session.station_name, session.station_id)

    async def unregister(self, station_id: str, websocket: Any = None) -> None:
        async with self._lock:
            current = self._agents.get(station_id)
            if current is not None and (websocket is None or current.websocket is websocket):
                self._agents.pop(station_id, None)
        metrics.AGENTS_CONNECTED.set(len(self._agents))
        logger.info("агент станции %s отключён", station_id)

    def get(self, station_id: str) -> AgentSession | None:
        return self._agents.get(station_id)

    def connected(self) -> list[dict[str, Any]]:
        return [session.as_dict() for session in self._agents.values()]

    def count(self) -> int:
        return len(self._agents)

    # ------------------------------------------------------------ push

    async def send_print(self, *, station_id: str, job_id: str, label_format: str,
                         content_type: str, payload: bytes, copies: int = 1,
                         task_id: str = "", wait_ack: bool = True) -> dict[str, Any]:
        """Отдать этикетку агенту станции и дождаться подтверждения записи.

        Возвращает ответ агента: `{ok, write_ms, ...}`. Ждать подтверждения
        обязательно — иначе «напечатано» на экране означает всего лишь
        «отправлено в сокет», а сборщик узнает правду по пустому лотку.
        """
        session = self._agents.get(station_id)
        if session is None:
            raise AgentBusy(f"агент станции {station_id} не подключён")

        message = {
            "type": "print",
            "job_id": job_id,
            "task_id": task_id,
            "format": label_format,
            "content_type": content_type,
            "copies": int(copies),
            # По сокету байты едут в base64: WebSocket-текст не переживёт
            # произвольные байты ZPL, а бинарный кадр смешает управление
            # с данными.
            "payload_b64": base64.b64encode(payload).decode("ascii"),
        }

        future: asyncio.Future[dict[str, Any]] | None = None
        self._job_owner[job_id] = station_id
        if wait_ack:
            future = asyncio.get_running_loop().create_future()
            self._waiters[job_id] = future
        try:
            await session.websocket.send_json(message)
        except Exception as error:  # noqa: BLE001
            self._waiters.pop(job_id, None)
            self._job_owner.pop(job_id, None)
            await self.unregister(station_id, session.websocket)
            raise AgentBusy(f"агент станции {station_id} оборвал соединение: {error}") from error

        session.prints += 1
        session.last_seen_at = time.time()
        if future is None:
            return {"ok": True, "ack": False}
        try:
            return await asyncio.wait_for(future, timeout=ACK_TIMEOUT_SECONDS)
        except asyncio.TimeoutError as error:
            raise AgentAckTimeout(
                f"агент станции {station_id} не подтвердил запись за "
                f"{ACK_TIMEOUT_SECONDS:.0f} с") from error
        finally:
            self._waiters.pop(job_id, None)
            self._job_owner.pop(job_id, None)

    def resolve(self, job_id: str, payload: dict[str, Any], *,
                station_id: str | None = None) -> bool:
        """Подтверждение от агента: задание напечатано (или нет).

        Принимается только от той станции, которой это задание и отправляли.
        Раньше принималось от любой: агент чужой станции мог закрыть чужую
        печать, и «напечатано» значило «кто-то сказал, что напечатал».

        Возвращает False, если подтверждение пришло не от того — вызывающий
        обязан это заметить и записать, а не промолчать.
        """
        owner = self._job_owner.get(job_id)
        if owner is None:
            return False
        if station_id is not None and owner != station_id:
            return False
        future = self._waiters.get(job_id)
        if future is not None and not future.done():
            future.set_result(payload)
        return True

    async def send_probe(self, station_id: str) -> None:
        """Попросить агент проверить, что принтер вообще понимает.

        Вопрос 2 раздела 13 мастера открыт: XP-420B заявляет эмуляцию ZPL, но
        подтверждения на живом принтере нет. Проверку делает агент — он один
        стоит рядом с устройством.
        """
        session = self._agents.get(station_id)
        if session is None:
            raise AgentBusy(f"агент станции {station_id} не подключён")
        await session.websocket.send_json({"type": "probe"})

    def note_write(self, *, task_id: str, station_id: str, write_ms: float,
                   label_format: str, ok: bool = True) -> None:
        """Запомнить последнюю запись в устройство.

        Это то самое число, которое не может измерить никто, кроме агента:
        `POST /labels/{task_id}/print` меряется снаружи, а запись RAW-байтов в
        USB — только на станции.
        """
        self.last_write = {
            "task_id": task_id,
            "station_id": station_id,
            "last_write_ms": round(float(write_ms), 3),
            "label_format": label_format,
            "ok": bool(ok),
            "at": time.time(),
        }
        if ok:
            metrics.PRINT_AGENT_WRITE.observe(max(0.0, float(write_ms)) / 1000.0)


async def send_to_tcp_printer(host: str, port: int, payload: bytes, *,
                              timeout: float = 3.0) -> float:
    """Запасной путь — сетевой принтер на 9100.

    Сегодня принтеры USB (решение владельца 14), но контракт отдаёт
    `printer_transport`, и `tcp` в нём предусмотрен. Пусть путь существует и
    работает, а не обнаруживается в день, когда на склад приедет сетевой
    принтер. Возвращает время записи в миллисекундах.
    """
    started = time.perf_counter()
    reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout)
    try:
        writer.write(payload)
        await asyncio.wait_for(writer.drain(), timeout=timeout)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass
    del reader
    return (time.perf_counter() - started) * 1000.0
