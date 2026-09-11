"""Выдача этикетки: из локального хранилища, без единого вызова в Wildberries.

Инвариант 9: стикер лежит локально до того, как человек нажал печать. Запрос к
WB в момент упаковки запрещён — именно он даёт сегодняшние 2–10 секунд ожидания
у принтера (раздел 3.3).

Целевой бюджет (раздел 6.6):

    SELECT zpl FROM wb_label WHERE task_id = $1       1–3 мс   ← отсюда
    push агенту по открытому соединению               5–20 мс  ← поток B
    RAW-печать в устройство                           5–20 мс  ← поток B
    принтер двигает головку                          50–150 мс

Здесь только первая строка: сама печать — на стороне агента потока B.
"""
from __future__ import annotations

import base64
import uuid
from typing import Any

from . import repositories as repo
from .domain import PRINT_ADVANCES_FROM, check_transition
from .metrics import LABEL_PRINT_DURATION
from .postgres import ConnectionPool, single, transaction
from .service import WmsService

# Что отдать агенту как тип содержимого. ZPL печатается принтером нативно,
# без растеризации драйвером, — ради этого формат и выбран.
CONTENT_TYPES = {
    "zplv": "application/x-zpl",
    "zplh": "application/x-zpl",
    "svg": "image/svg+xml",
    "png": "image/png",
}


class LabelOperations:
    def __init__(self, pool: ConnectionPool, service: WmsService) -> None:
        self._pool = pool
        self._service = service

    def read(self, task_id: str, _params: dict[str, Any]) -> dict[str, Any]:
        """Тело этикетки по заданию (приложение C).

        `order_id` обязан совпасть с заданием: напечатанный чужой стикер
        отправит вещь другому покупателю, и это не исправляется никак.
        """
        with self._pool.connection() as connection:
            with single(connection) as cursor:
                task = repo.task_view(cursor, _uuid(task_id))
                if task is None:
                    raise ValueError(f"задание {task_id} не найдено")
                label = repo.label_of(cursor, task["id"])
        if label is None:
            raise ValueError(
                "стикер ещё не получен от Wildberries; он тянется заранее, "
                "фоново, сразу после резерва (раздел 6.6)")
        if label["invalidated_at"] is not None:
            raise ValueError("стикер помечен недействительным: задание отменено")

        payload = bytes(label["payload"])
        return {
            "id": str(label["id"]),
            "order_id": int(task["wb_order_id"]),
            "version": int(label["version"]),
            "payload": base64.b64encode(payload).decode("ascii"),
            "content_type": CONTENT_TYPES.get(label["format"], "application/octet-stream"),
            "checksum": label["checksum"],
            "format": label["format"],
            "sticker": {"barcode": task.get("barcode")},
        }

    def print(self, task_id: str, params: dict[str, Any]) -> dict[str, Any]:
        """Готовит готовые байты для агента печати.

        Никакой растеризации: агент пишет их в устройство как есть
        (`WritePrinter` с типом RAW). Растеризация на стороне агента и есть те
        1–3 секунды, ради которых переписан весь путь этикетки.
        """
        station_id = _uuid_or_none(params.get("station_id"))
        if station_id is None:
            raise ValueError("station_id обязателен: принтер на каждом рабочем месте свой")
        if not str(params.get("idempotency_key") or "").strip():
            raise ValueError("idempotency_key обязателен")
        reprint = bool(params.get("reprint"))
        reason = str(params.get("reason") or "").strip()
        if reprint and not reason:
            raise ValueError(
                "при повторной печати reason обязателен: доля перепечаток — "
                "метрика качества этикетки и принтера, и её нечем разбирать без причины")

        with LABEL_PRINT_DURATION.time():
            with self._pool.connection() as connection:
                with transaction(connection) as cursor:
                    task = repo.task_view(cursor, _uuid(task_id), for_update=True)
                    if task is None:
                        raise ValueError(f"задание {task_id} не найдено")
                    label = repo.label_of(cursor, task["id"])
                    if label is None or label["invalidated_at"] is not None:
                        raise ValueError(
                            "действующего стикера нет: он тянется заранее, "
                            "а у отменённого задания помечается недействительным")
                    station = repo.station(cursor, station_id)
                    check_transition("print", task["state"])
                    # Печать из состояний подбора законна — стикер лежит
                    # локально с момента резерва (инвариант 9), — но состояние
                    # не меняет: печать не подбор.
                    if task["state"] in PRINT_ADVANCES_FROM:
                        repo.set_task_state(cursor, task["id"], "labeled")
                    first_print = repo.record_print(cursor, label["id"])
                    if first_print:
                        # Событие «этикетка наклеена» уходит один раз. Раньше
                        # оно уходило при каждой печати: пять перепечаток из-за
                        # зажёванной ленты давали пять наклеек в отчёте
                        # потребителя. Наклейка одна, печатей сколько угодно.
                        self._service.emit_for_aggregate(
                            cursor, aggregate_id=task["id"],
                            event_type="wms.label.attached.v1",
                            payload={"task_id": str(task["id"]),
                                     "owner_id": str(task["owner_id"]),
                                     "label_id": str(label["id"]),
                                     "format": label["format"],
                                     "version": int(label["version"])},
                            correlation_id=str(params["idempotency_key"]))

        payload = bytes(label["payload"])
        return {
            "task_id": str(task["id"]),
            "owner_external_id": task.get("seller_external_id"),
            # Номер заказа WB: рабочее место сверяет его перед печатью.
            # Напечатанный чужой стикер отправит вещь другому покупателю.
            "order_id": int(task["wb_order_id"]) if task.get("wb_order_id") else None,
            "format": label["format"],
            "content_type": CONTENT_TYPES.get(label["format"], "application/octet-stream"),
            "payload": base64.b64encode(payload).decode("ascii"),
            "checksum": label["checksum"],
            "version": int(label["version"]),
            "station_id": str(station_id),
            "printer_transport": (station or {}).get("transport", "agent"),
            "reprint": reprint,
            "duplicate": not first_print,
        }


def _uuid(value: Any) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError):
        raise ValueError(f"{value!r} не похоже на идентификатор") from None


def _uuid_or_none(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value)) if value else None
    except (ValueError, AttributeError):
        return None
