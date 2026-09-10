"""Путь этикетки: нажатие → байты в устройстве.

Ради этого пути переписана вся работа со стикером. Раньше нажатие «печать»
запускало три последовательных вызова к Wildberries, растеризацию PNG и спулер
ОС — 2–10 секунд, пока человек стоит и ждёт. Теперь стикер уже лежит локально
(инвариант 9), и остаётся только достать его и отдать агенту.

Бюджет раздела 10:

    достать стикер из wms      1–20 мс
    push агенту по сокету      5–20 мс
    RAW-запись в устройство    5–20 мс
    головка принтера          50–150 мс
    ────────────────────────────────
    меньше 300 мс

Поэтому здесь нет ни одного действия, которое можно сделать после отправки:
запись в базу, метрики и журнал идут **после** push, а не до него.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from . import metrics
from .agent_hub import AgentBusy, AgentHub, send_to_tcp_printer
from .domain import uid
from .projection import Projection
from .store import Store
from .wms_client import Label, LabelUnusable, WmsClient, WmsRejected, WmsUnavailable

logger = logging.getLogger("workstation.printing")

# Форматы, которые печатаются как есть, без обработки на станции.
NATIVE_FORMATS = frozenset({"zplv", "zplh", "zpl"})


class PrintRefused(RuntimeError):
    """Печатать нельзя, и это ответ человеку, а не тихий отказ."""


class PrintService:
    def __init__(self, client: WmsClient, hub: AgentHub, store: Store,
                 projection: Projection) -> None:
        self._client = client
        self._hub = hub
        self._store = store
        self._projection = projection

    async def print_label(self, *, task_id: str, station_id: str,
                          actor_id: str | None = None, reprint: bool = False,
                          reason: str | None = None, copies: int = 1,
                          idempotency_key: str | None = None) -> dict[str, Any]:
        """Напечатать этикетку задания на принтере станции."""
        if reprint and not (reason or "").strip():
            # Без причины счётчик перепечаток бесполезен для разбора, а доля
            # перепечаток — метрика качества этикетки и принтера.
            raise PrintRefused("повторная печать требует причины")

        key = idempotency_key or f"print-{task_id}-{uid()}"
        task = self._projection.get(task_id)
        expected_order = task.wb_order_id if task else None

        started = time.perf_counter()
        try:
            label = await self._client.print_label(
                task_id, station_id=station_id, idempotency_key=key, copies=copies,
                reprint=reprint, reason=reason, actor_id=actor_id,
                expected_order_id=expected_order)
        except LabelUnusable as error:
            # Напечатанный чужой стикер отправит вещь другому покупателю —
            # это хуже ненапечатанного.
            metrics.PRINTS.labels(outcome="unusable", label_format="unknown").inc()
            await self._store.start_print(
                idempotency_key=key, task_id=task_id, station_id=station_id,
                label_format="unknown", checksum="", payload_bytes=0, copies=copies,
                reprint=reprint, reason=reason, actor_id=actor_id)
            await self._store.finish_print(idempotency_key=key, outcome="failed",
                                           error=str(error))
            raise PrintRefused(str(error)) from error
        except WmsRejected as error:
            metrics.PRINTS.labels(outcome="rejected", label_format="unknown").inc()
            raise PrintRefused(
                f"wms не отдал стикер: {error.code or 'без кода'}") from error
        except WmsUnavailable as error:
            metrics.PRINTS.labels(outcome="wms_unavailable", label_format="unknown").inc()
            raise PrintRefused(f"wms недоступен: {error}") from error

        write_ms, transport = await self._deliver(
            label, station_id=station_id, task_id=task_id, key=key,
            copies=copies, reprint=reprint, reason=reason, actor_id=actor_id, started=started)

        click_to_agent_ms = (time.perf_counter() - started) * 1000.0
        metrics.PRINT_CLICK_TO_AGENT.observe(click_to_agent_ms / 1000.0)
        metrics.PRINTS.labels(outcome="ok", label_format=label.label_format).inc()

        # Всё, что ниже, происходит уже после того, как байты ушли в принтер:
        # ни база, ни журнал не стоят в бюджете 50 мс.
        await self._store.start_print(
            idempotency_key=key, task_id=task_id, station_id=station_id,
            label_format=label.label_format, checksum=label.checksum,
            payload_bytes=len(label.body), copies=copies, reprint=reprint,
            reason=reason, actor_id=actor_id)
        await self._store.finish_print(
            idempotency_key=key, outcome="written" if write_ms is not None else "sent",
            click_to_agent_ms=round(click_to_agent_ms, 3),
            agent_write_ms=round(write_ms, 3) if write_ms is not None else None)
        if task is not None:
            await self._store.note_task(task, station_id=station_id, actor_id=actor_id,
                                        printed=True)

        return {
            "ok": True,
            "task_id": task_id,
            "station_id": station_id,
            "label_format": label.label_format,
            "content_type": label.content_type,
            "bytes": len(label.body),
            "reprint": reprint,
            "transport": transport,
            "click_to_agent_ms": round(click_to_agent_ms, 1),
            "agent_write_ms": round(write_ms, 1) if write_ms is not None else None,
            "idempotency_key": key,
        }

    async def _deliver(self, label: Label, *, station_id: str, task_id: str, key: str,
                       copies: int, reprint: bool, reason: str | None,
                       actor_id: str | None, started: float) -> tuple[float | None, str]:
        """Доставить байты на устройство. Возвращает (время записи, транспорт)."""
        session = self._hub.get(station_id)
        job_id = uid()

        if session is not None:
            try:
                ack = await self._hub.send_print(
                    station_id=station_id, job_id=job_id, label_format=label.label_format,
                    content_type=label.content_type, payload=label.body, copies=copies,
                    task_id=task_id)
            except AgentBusy as error:
                await self._fail(key, task_id, station_id, label, copies, reprint,
                                 reason, actor_id, str(error))
                raise PrintRefused(str(error)) from error
            if not ack.get("ok", True):
                message = str(ack.get("error") or "агент не смог напечатать")
                await self._fail(key, task_id, station_id, label, copies, reprint,
                                 reason, actor_id, message)
                raise PrintRefused(message)
            # Записать факт в hub здесь нельзя: то же самое сообщение агента
            # уже прошло через обработчик сокета. Две записи на одну печать
            # испортили бы гистограмму ровно той метрики, ради которой всё
            # это меряется.
            return _float_or_none(ack.get("write_ms")), "agent"

        # Агента нет — пробуем сетевой принтер, если станция так настроена.
        printer = await self._store.printer(station_id)
        if printer and str(printer.get("transport")) == "tcp" and printer.get("printer_name"):
            host, _, port = str(printer["printer_name"]).partition(":")
            write_ms = await send_to_tcp_printer(host, int(port or 9100), label.body)
            self._hub.note_write(task_id=task_id, station_id=station_id, write_ms=write_ms,
                                 label_format=label.label_format)
            return write_ms, "tcp"

        message = (f"на станции {station_id} нет подключённого агента печати. "
                   f"Стикер получен, но напечатать его некуда")
        await self._fail(key, task_id, station_id, label, copies, reprint, reason,
                         actor_id, message)
        raise PrintRefused(message)

    async def _fail(self, key: str, task_id: str, station_id: str, label: Label,
                    copies: int, reprint: bool, reason: str | None,
                    actor_id: str | None, error: str) -> None:
        metrics.PRINTS.labels(outcome="failed", label_format=label.label_format).inc()
        await self._store.start_print(
            idempotency_key=key, task_id=task_id, station_id=station_id,
            label_format=label.label_format, checksum=label.checksum,
            payload_bytes=len(label.body), copies=copies, reprint=reprint,
            reason=reason, actor_id=actor_id)
        await self._store.finish_print(idempotency_key=key, outcome="failed", error=error[:500])
        logger.warning("печать не удалась: %s", error)

    async def probe_printer(self, station_id: str) -> dict[str, Any]:
        """Проверить на живом принтере, что он понимает.

        Ответ приходит асинхронно от агента и записывается в
        `workstation_printer.confirmed_format`. Формат этикетки — открытый
        вопрос 2 раздела 13 мастера, и закрывается он живым принтером, а не
        значением по умолчанию в конфиге.
        """
        await self._hub.send_probe(station_id)
        return {"ok": True, "station_id": station_id,
                "note": "агент печатает тестовые этикетки ZPL и TSPL; "
                        "результат придёт отдельным сообщением"}


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
