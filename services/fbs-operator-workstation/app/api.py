"""HTTP-слой рабочего места.

Наружу смотрят три вещи:

* **экраны** — подбор, приёмка, размещение, инвентаризация, отгрузка и экран
  начальника склада. Ванильный JS, как и остальной фронт платформы;
* **собственный API** под эти экраны (`/api/workstation/v1/*`). Он не имеет
  ничего общего с контрактом wms и меняется свободно: контракт wms заморожен
  потоком 0, а это внутренний интерфейс между экраном и сервисом;
* **сокет агентов печати** — постоянное соединение до каждой станции.

`/healthz` отвечает «процесс жив», `/readyz` — «работа делается». Разводить их
принципиально: в боевом контуре четыре воркера стояли Up с зелёным
healthcheck и нулём обработанных сообщений (раздел 3.5 мастера).
"""
from __future__ import annotations

import hmac

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import APIRouter, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import config, metrics, screens
from .agent_hub import AgentBusy, AgentHub, AgentSession
from .inbox import InboxConsumer, NullConsumer
from .picking import PickingRefused, PickingService
from .printing import PrintRefused, PrintService, PrintUnknown
from .projection import Projection
from .puller import Poller
from .receiving import ReceivingRefused, ReceivingService
from .store import Store
from .supervisor import SupervisorService
from .wms_client import WmsClient, WmsRejected, WmsUnavailable

logger = logging.getLogger("workstation")

API = "/api/workstation/v1"


class AppState:
    """Всё, что живёт дольше одного запроса."""

    def __init__(self) -> None:
        self.client: WmsClient | None = None
        self.store: Store | None = None
        self.projection = Projection()
        self.hub = AgentHub()
        self.poller: Poller | None = None
        self.inbox: Any = NullConsumer()
        self.picking: PickingService | None = None
        self.printing: PrintService | None = None
        self.receiving: ReceivingService | None = None
        self.supervisor: SupervisorService | None = None
        self.started_at = time.time()

    def ready(self) -> bool:
        """Готовность — это «работа делается», а не «порт открыт».

        Неготовым сервис считается, когда опрос не проходит: экран без опроса
        показывает вчерашний день, и именно это раньше выглядело как «всё
        зелёное».
        """
        snapshot = self.projection.snapshot()
        if not snapshot.get("last_poll_ok"):
            return False
        last = snapshot.get("last_poll_at")
        if last is None:
            return False
        # Пропущенные подряд опросы — тоже неготовность. Порог с запасом в
        # пять интервалов: одиночная сетевая заминка алертом быть не должна.
        return (time.time() - float(last)) < max(30.0, config.poll_interval_seconds() * 5)


state = AppState()
router = APIRouter(prefix=API)


# ------------------------------------------------------------------ входные формы

class TasksQuery(BaseModel):
    assignee: str | None = None
    owner_external_id: str | None = None
    screen_status: str | None = None


class SessionStart(BaseModel):
    actor_id: str = Field(min_length=1, max_length=128)
    station_id: str | None = None
    limit: int = Field(default=20, ge=1, le=200)
    owner_external_ids: list[str] | None = None


class BarcodeLookup(BaseModel):
    picklist_barcode: str = Field(min_length=1, max_length=64)


class ScanCommand(BaseModel):
    task_id: str = Field(min_length=1)
    barcode: str = Field(min_length=1, max_length=64)
    actor_id: str = Field(min_length=1, max_length=128)
    session_id: str | None = None
    station_id: str | None = None


class PackCommand(BaseModel):
    task_id: str = Field(min_length=1)
    control_barcode: str = Field(min_length=1, max_length=64)
    actor_id: str = Field(min_length=1, max_length=128)
    station_id: str | None = None
    box_barcode: str | None = None
    session_id: str | None = None


class PrintCommand(BaseModel):
    task_id: str = Field(min_length=1)
    station_id: str = Field(min_length=1)
    actor_id: str | None = None
    reprint: bool = False
    reason: str | None = None
    copies: int = Field(default=1, ge=1, le=10)
    # Ключ идемпотентности печати. Экран присылает свой и блокирует кнопку до
    # ответа: двойной клик по «печать» давал две этикетки на одну вещь, и
    # вторая наклеивалась на следующую.
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)


class CancelCommand(BaseModel):
    task_id: str = Field(min_length=1)
    # Причина обязательна и здесь, и в схеме базы (инвариант 11).
    reason_code: str = Field(min_length=1, max_length=64)
    comment: str = Field(min_length=1, max_length=512)
    actor_id: str = Field(min_length=1, max_length=128)
    handed_over: bool = False


class ShelfCommand(BaseModel):
    task_id: str = Field(min_length=1)
    cell_address: str = Field(min_length=1, max_length=64)
    actor_id: str = Field(min_length=1, max_length=128)
    box_barcode: str | None = None
    # Причина обязательна: возврат без причины — та самая тихая запись,
    # из-за которой у всех 2645 отмен боевого контура причина пуста.
    # `wms` отвергнет такой возврат, и отказ дойдёт до сборщика уже после
    # того, как он отошёл от стойки.
    reason: str = Field(min_length=1, max_length=512)


class ScreenQuery(BaseModel):
    owner_external_id: str | None = None
    reference: str | None = None
    limit: int = Field(default=50, ge=1, le=200)


class ReceiptSubmit(BaseModel):
    seller_external_id: str = Field(min_length=1, max_length=128)
    reference: str = Field(min_length=1, max_length=128)
    lines: list[dict[str, Any]]
    warehouse_code: str = "RUM"
    seller_name: str | None = None
    seller_inn: str | None = None
    actor_id: str | None = None


class BoxSubmit(BaseModel):
    barcode: str = Field(min_length=1, max_length=64)
    seller_external_id: str = Field(min_length=1, max_length=128)
    comment: str = Field(min_length=1, max_length=512)
    product_barcode: str | None = None
    cell_address: str | None = None
    quantity: int = Field(default=0, ge=0)
    counted: bool = False
    sequence: int | None = None
    total_boxes: int | None = None
    actor_id: str | None = None


class SheetQuery(BaseModel):
    seller_external_id: str = Field(min_length=1, max_length=128)
    scope: str = "partial"
    cell_addresses: list[str] | None = None
    barcodes: list[str] | None = None


class CountSubmit(BaseModel):
    seller_external_id: str = Field(min_length=1, max_length=128)
    reference: str = Field(min_length=1, max_length=128)
    scope: str = "partial"
    lines: list[dict[str, Any]]
    warehouse_code: str = "RUM"
    actor_id: str | None = None


class ShipmentCommand(BaseModel):
    seller_external_id: str = Field(min_length=1, max_length=128)
    action: str = Field(min_length=1, max_length=32)
    wb_supply_id: str | None = None
    task_ids: list[str] | None = None
    handed_over_by: str | None = None


class ProbeCommand(BaseModel):
    station_id: str = Field(min_length=1)


class ProbeConfirm(BaseModel):
    station_id: str = Field(min_length=1)
    # Что реально вылезло из принтера. Определить это может только человек,
    # стоящий рядом: агент печатает оба теста и молчит о результате.
    confirmed_format: str = Field(pattern="^(zplv|zplh|tspl|png)$")
    note: str | None = None


# ------------------------------------------------------------------ задания

@router.post("/tasks")
async def tasks(query: TasksQuery) -> dict[str, Any]:
    """Очередь для экрана — из проекции, а не из wms.

    Экран обновляется чаще, чем меняется склад; ходить в wms на каждое
    обновление незачем — опросчик уже сходил.
    """
    rows = state.projection.for_screen(
        assignee=query.assignee, owner_external_id=query.owner_external_id,
        screen_status=query.screen_status)
    return {
        "tasks": [task.as_dict() for task in rows],
        "pickable": len(state.projection.pickable()),
        "full_path": state.supervisor.full_path() if state.supervisor else {},
    }


@router.post("/status")
async def status() -> dict[str, Any]:
    """Состояние самого рабочего места — для экрана и для человека."""
    store_available = bool(state.store and state.store.available)
    return {
        "ready": state.ready(),
        "uptime_seconds": round(time.time() - state.started_at, 1),
        "poller": state.poller.status() if state.poller else {},
        "bus": state.inbox.status(),
        "projection": state.projection.snapshot(),
        "agents": state.hub.connected(),
        "store": {"available": store_available,
                  "last_error": state.store.last_error if state.store else None},
        "wms_base_url": state.client.base_url if state.client else None,
    }


@router.post("/poll")
async def poll_now() -> dict[str, Any]:
    """Опросить немедленно. Нужна экрану после команды и кнопке «обновить»."""
    if state.poller is None:
        return {"ok": False, "error": "опросчик не запущен"}
    try:
        batch = await state.poller.poll_once()
    except WmsUnavailable as error:
        return {"ok": False, "error": str(error)}
    return {"ok": True, "tasks": len(batch.tasks), "available_total": batch.available_total}


# ------------------------------------------------------------------ подбор

@router.post("/sessions")
async def session_start(command: SessionStart) -> JSONResponse:
    try:
        result = await state.picking.start_session(
            actor_id=command.actor_id, station_id=command.station_id,
            limit=command.limit, lease_seconds=config.lease_seconds(),
            owner_external_ids=command.owner_external_ids)
    except PickingRefused as error:
        return _refused(error)
    return _ok(result)


@router.post("/sessions/by-barcode")
async def session_by_barcode(lookup: BarcodeLookup) -> JSONResponse:
    try:
        return _ok(await state.picking.session_by_barcode(lookup.picklist_barcode))
    except PickingRefused as error:
        return _refused(error, status_code=404)


# Объявлен ПОСЛЕ «by-barcode»: маршруты сопоставляются сверху вниз, и шаблон с
# параметром съел бы «by-barcode» как идентификатор сессии.
@router.post("/sessions/{session_id}")
async def session_view(session_id: str) -> JSONResponse:
    try:
        return _ok(await state.picking.session_view(session_id))
    except PickingRefused as error:
        return _refused(error, status_code=404)


@router.post("/sessions/{session_id}/finish")
async def session_finish(session_id: str) -> JSONResponse:
    return _ok(await state.picking.finish_session(session_id))


@router.post("/scan")
async def scan(command: ScanCommand) -> JSONResponse:
    try:
        result = await state.picking.scan_at_rack(
            task_id=command.task_id, barcode=command.barcode, actor_id=command.actor_id,
            session_id=command.session_id, station_id=command.station_id)
    except PickingRefused as error:
        return _refused(error)
    return _ok(result)


@router.post("/pack")
async def pack(command: PackCommand) -> JSONResponse:
    """Упаковка с контрольным сканом.

    Отказ приезжает статусом 409, а не 200 с полем: экран обязан показать
    красное и остановиться, а не «показать результат».
    """
    try:
        result = await state.picking.pack(
            task_id=command.task_id, control_barcode=command.control_barcode,
            actor_id=command.actor_id, station_id=command.station_id,
            box_barcode=command.box_barcode, session_id=command.session_id)
    except PickingRefused as error:
        return _refused(error)
    return _ok(result, status_code=200 if result.get("accepted") else 409)


@router.post("/cancel")
async def cancel(command: CancelCommand) -> JSONResponse:
    try:
        result = await state.picking.cancel(
            task_id=command.task_id, reason_code=command.reason_code,
            comment=command.comment, actor_id=command.actor_id,
            handed_over=command.handed_over)
    except PickingRefused as error:
        return _refused(error)
    return _ok(result)


@router.post("/return-to-shelf")
async def return_to_shelf(command: ShelfCommand) -> JSONResponse:
    try:
        result = await state.picking.return_to_shelf(
            task_id=command.task_id, cell_address=command.cell_address,
            actor_id=command.actor_id, box_barcode=command.box_barcode,
            reason=command.reason)
    except PickingRefused as error:
        return _refused(error)
    return _ok(result)


# ------------------------------------------------------------------ печать

@router.post("/print")
async def print_label(command: PrintCommand) -> JSONResponse:
    try:
        result = await state.printing.print_label(
            task_id=command.task_id, station_id=command.station_id,
            actor_id=command.actor_id, reprint=command.reprint,
            reason=command.reason, copies=command.copies,
            idempotency_key=command.idempotency_key)
    except PrintUnknown as error:
        # 202, а не 409: «неизвестно» — не отказ. Экран обязан сказать
        # человеку «посмотрите на принтер», а не «нажмите ещё раз».
        return JSONResponse({"ok": False, "status": "unknown", "error": str(error)},
                            status_code=202)
    except PrintRefused as error:
        return _refused(error)
    return _ok(result)


@router.post("/print/probe")
async def print_probe(command: ProbeCommand) -> JSONResponse:
    """Проверить на живом принтере, понимает ли он ZPL.

    Вопрос 2 раздела 13 мастера закрывается этим вызовом, а не значением по
    умолчанию в конфиге.
    """
    try:
        return _ok(await state.printing.probe_printer(command.station_id))
    except AgentBusy as error:
        return _refused(error)


@router.post("/print/probe/confirm")
async def print_probe_confirm(command: ProbeConfirm) -> JSONResponse:
    """Записать, что человек увидел на выходе из принтера.

    Это и есть закрытие вопроса 2 раздела 13 мастера для конкретной станции:
    не «по документации XP-420B умеет ZPL», а «на этой машине вышла вот такая
    этикетка, дата такая-то».
    """
    if state.store is None:
        return _refused(RuntimeError("база рабочего места недоступна"), status_code=503)
    await state.store.record_probe(
        station_id=command.station_id, confirmed_format=command.confirmed_format,
        note=command.note or "подтверждено человеком у принтера")
    agent = state.hub.get(command.station_id)
    if agent is not None:
        agent.confirmed_format = command.confirmed_format
        # Агент обязан узнать сразу: от формата зависит, перекладывать ли
        # растр перед печатью следующей этикетки.
        try:
            await agent.websocket.send_json({"type": "format",
                                             "confirmed_format": command.confirmed_format})
        except Exception:  # noqa: BLE001 — агент отвалился, запись всё равно сделана
            pass
    return _ok({"ok": True, "station_id": command.station_id,
                "confirmed_format": command.confirmed_format})


@router.post("/printers")
async def printers() -> dict[str, Any]:
    rows = await state.store.printers() if state.store else []
    connected = {agent["station_id"]: agent for agent in state.hub.connected()}
    for row in rows:
        row["station_id"] = str(row.get("station_id"))
        row["online"] = row["station_id"] in connected
    return {"printers": rows, "connected": list(connected.values())}


# ------------------------------------------------------------------ приёмка и размещение

@router.post("/receiving/screen")
async def receiving_screen(query: ScreenQuery) -> JSONResponse:
    try:
        return _ok(await state.receiving.receipts_screen(
            owner_external_id=query.owner_external_id, reference=query.reference,
            limit=query.limit))
    except ReceivingRefused as error:
        return _refused(error, status_code=503)


@router.post("/receiving/submit")
async def receiving_submit(command: ReceiptSubmit) -> JSONResponse:
    try:
        return _ok(await state.receiving.submit_receipt(
            seller_external_id=command.seller_external_id, reference=command.reference,
            lines=command.lines, warehouse_code=command.warehouse_code,
            seller_name=command.seller_name, seller_inn=command.seller_inn,
            actor_id=command.actor_id))
    except ReceivingRefused as error:
        return _refused(error)


@router.post("/putaway/screen")
async def putaway_screen(query: ScreenQuery) -> JSONResponse:
    try:
        return _ok(await state.receiving.putaway_screen(
            owner_external_id=query.owner_external_id, reference=query.reference,
            limit=query.limit))
    except ReceivingRefused as error:
        return _refused(error, status_code=503)


@router.post("/putaway/box")
async def putaway_box(command: BoxSubmit) -> JSONResponse:
    try:
        return _ok(await state.receiving.place_in_box(
            barcode=command.barcode, seller_external_id=command.seller_external_id,
            comment=command.comment, product_barcode=command.product_barcode,
            cell_address=command.cell_address, quantity=command.quantity,
            counted=command.counted, sequence=command.sequence,
            total_boxes=command.total_boxes, actor_id=command.actor_id))
    except ReceivingRefused as error:
        return _refused(error)


@router.post("/inventory/sheet")
async def inventory_sheet(query: SheetQuery) -> JSONResponse:
    try:
        return _ok(await state.receiving.inventory_sheet(
            seller_external_id=query.seller_external_id, scope=query.scope,
            cell_addresses=query.cell_addresses, barcodes=query.barcodes))
    except ReceivingRefused as error:
        return _refused(error, status_code=503)


@router.post("/inventory/count")
async def inventory_count(command: CountSubmit) -> JSONResponse:
    try:
        return _ok(await state.receiving.submit_count(
            seller_external_id=command.seller_external_id, reference=command.reference,
            scope=command.scope, lines=command.lines,
            warehouse_code=command.warehouse_code, actor_id=command.actor_id))
    except ReceivingRefused as error:
        return _refused(error)


# ------------------------------------------------------------------ отгрузка

@router.post("/shipping/picked")
async def shipping_picked(query: ScreenQuery) -> JSONResponse:
    try:
        rows = await state.receiving.picked_tasks(
            seller_external_id=query.owner_external_id, limit=query.limit)
    except ReceivingRefused as error:
        return _refused(error, status_code=503)
    return _ok({"tasks": rows})


@router.post("/shipping/action")
async def shipping_action(command: ShipmentCommand) -> JSONResponse:
    """Действия над поставкой, включая подтверждение передачи человеком."""
    try:
        result = await state.receiving.shipment(
            seller_external_id=command.seller_external_id, action=command.action,
            wb_supply_id=command.wb_supply_id, task_ids=command.task_ids,
            handed_over_by=command.handed_over_by)
    except ReceivingRefused as error:
        return _refused(error)
    return _ok(result)


# ------------------------------------------------------------------ начальник склада

@router.post("/supervisor/screen")
async def supervisor_screen() -> dict[str, Any]:
    result = await state.supervisor.screen()
    result["printing"]["last_write"] = state.hub.last_write or None
    return result


# ------------------------------------------------------------------ агенты печати

@router.websocket("/agent")
async def agent_socket(websocket: WebSocket) -> None:
    """Постоянное соединение с агентом станции.

    Push, а не опрос: агент, спрашивающий раз в секунду, — это и есть секунда
    задержки, которую потом не убрать ничем (раздел «Печать» файла 03).
    """
    await websocket.accept()
    station_id = ""
    try:
        hello = await asyncio.wait_for(websocket.receive_json(), timeout=10.0)
    except (asyncio.TimeoutError, WebSocketDisconnect, ValueError):
        await websocket.close(code=1002)
        return

    # Агент предъявляет общий секрет. Без него в сокет приходит кто угодно и
    # объявляет себя станцией: печать чужих стикеров и подтверждение чужих
    # заданий. Вне локальных сред секрет обязателен.
    expected = (os.getenv("WORKSTATION_AGENT_TOKEN") or "").strip()
    presented = str(hello.get("token") or "").strip()
    local = (os.getenv("APP_ENV") or "").strip().lower() in {"test", "local", "development"}
    if expected:
        if not hmac.compare_digest(expected, presented):
            await websocket.send_json({"type": "error", "error": "агент не предъявил токен"})
            await websocket.close(code=1008)
            return
    elif not local:
        await websocket.send_json(
            {"type": "error", "error": "WORKSTATION_AGENT_TOKEN не задан"})
        await websocket.close(code=1011)
        return

    station_id = str(hello.get("station_id") or "").strip()
    station_name = str(hello.get("station_name") or station_id or "станция")
    if not station_id:
        await websocket.send_json({"type": "error", "error": "агент не назвал station_id"})
        await websocket.close(code=1002)
        return

    # `transport` и `printer_name` агент НЕ задаёт: транспорт станции — это
    # запись в базе (`station.transport`), и агент, объявивший себя `tcp`,
    # увёл бы печать на сетевой адрес, которого никто не проверял.
    session = AgentSession(
        station_id=station_id, station_name=station_name, websocket=websocket,
        printer_name=None,
        transport="agent",
        capabilities=hello.get("capabilities") if isinstance(hello.get("capabilities"), dict) else {},
        confirmed_format=_text(hello.get("confirmed_format")))
    await state.hub.register(session)
    if state.store is not None:
        await state.store.upsert_printer(
            station_id=station_id, station_name=station_name,
            printer_name=session.printer_name, transport=session.transport)
        if session.confirmed_format:
            await state.store.record_probe(
                station_id=station_id, confirmed_format=session.confirmed_format,
                note="сообщено агентом при подключении")

    await websocket.send_json({"type": "welcome", "station_id": station_id})
    try:
        while True:
            message = await websocket.receive_json()
            await _handle_agent_message(session, message)
    except (WebSocketDisconnect, ValueError, RuntimeError):
        pass
    finally:
        await state.hub.unregister(station_id, websocket)


async def _handle_agent_message(session: AgentSession, message: dict[str, Any]) -> None:
    kind = str(message.get("type") or "")
    if kind == "result":
        job_id = str(message.get("job_id") or "")
        ok = bool(message.get("ok", True))
        write_ms = message.get("write_ms")
        accepted = state.hub.resolve(
            job_id, {"ok": ok, "write_ms": write_ms, "error": message.get("error")},
            station_id=session.station_id)
        if not accepted:
            # Не молчим: подтверждение от не той станции — это либо ошибка
            # настройки агента, либо чужой агент в сети. И то и другое надо
            # видеть, а не списывать на «печать не подтвердилась».
            log.warning("станция %s подтвердила чужое задание печати %s",
                        session.station_id, job_id)
            metrics.PRINTS.labels(outcome="foreign_ack", label_format="unknown").inc()
            return
        if ok and write_ms is not None:
            state.hub.note_write(
                task_id=str(message.get("task_id") or ""), station_id=session.station_id,
                write_ms=float(write_ms), label_format=str(message.get("format") or ""))
        metrics.worker_beat("print_agent", units=1)
    elif kind == "probe_result":
        # Результат живого теста принтера. Формат этикетки закрывается этим
        # ответом, а не значением по умолчанию (раздел 13, вопрос 2).
        confirmed = _text(message.get("confirmed_format"))
        session.confirmed_format = confirmed
        if state.store is not None and confirmed:
            await state.store.record_probe(
                station_id=session.station_id, confirmed_format=confirmed,
                note=_text(message.get("note")))
        logger.info("станция %s: принтер понял %s", session.station_name, confirmed)
    elif kind == "heartbeat":
        session.last_seen_at = time.time()
        metrics.worker_beat("print_agent", units=0)


# ------------------------------------------------------------------ вспомогательное

def _ok(payload: Any, status_code: int = 200) -> JSONResponse:
    """Ответ с приведением типов базы к JSON.

    Postgres отдаёт uuid и timestamptz объектами, а `JSONResponse` сериализует
    голым json.dumps и падает на них пятисоткой. Экран при этом видит не
    ошибку данных, а «сервис сломался».
    """
    return JSONResponse(jsonable_encoder(payload), status_code=status_code)


def _refused(error: Exception, status_code: int = 409) -> JSONResponse:
    """Отказ словами. Экран показывает их человеку как есть."""
    return JSONResponse({"ok": False, "error": str(error)}, status_code=status_code)


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


# ------------------------------------------------------------------ сборка приложения

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    state.client = WmsClient(config.wms_base_url(), token=config.wms_service_token(),
                             timeout=config.wms_timeout_seconds())
    state.store = Store(config.database_url())
    state.poller = Poller(state.client, state.projection,
                          interval_seconds=config.poll_interval_seconds(),
                          limit=config.poll_limit(), store=state.store)
    state.picking = PickingService(state.client, state.projection, state.store, state.poller)
    state.printing = PrintService(state.client, state.hub, state.store, state.projection)
    state.receiving = ReceivingService(state.client)
    state.supervisor = SupervisorService(state.projection, state.store, state.receiving)

    await state.store.ping()
    # Задания открытых сессий возвращаются на экран ДО первого опроса.
    # Рабочее место перезапустили посреди смены: у пяти сборщиков на руках по
    # обходу, а экран показывал бы им «работы нет».
    await state.poller.restore_open_sessions()
    state.poller.start()

    bus_url = config.rabbitmq_url()
    if bus_url:
        state.inbox = InboxConsumer(bus_url, config.events_exchange(),
                                    on_event=state.poller.nudge)
        state.inbox.start(asyncio.get_running_loop())
    else:
        state.inbox = NullConsumer()
        state.inbox.start()

    logger.info("рабочее место поднято: wms=%s, опрос каждые %.1f с",
                state.client.base_url, config.poll_interval_seconds())
    try:
        yield
    finally:
        state.inbox.stop()
        if state.poller is not None:
            await state.poller.stop()
        if state.client is not None:
            await state.client.aclose()
        if state.store is not None:
            await state.store.close()


def create_app() -> FastAPI:
    # Падаем на старте, а не на первом запросе: сервис без объявленного
    # окружения не должен считаться поднявшимся.
    environment = config.app_environment()

    # Логи со временем и именем источника. Разбор происшествия по логам без
    # отметки времени невозможен, а именно логов не хватило, когда четыре
    # воркера боевого контура молчали при зелёном healthcheck (раздел 3.5).
    logging.basicConfig(
        level=logging.getLevelName(os.getenv("LOG_LEVEL", "INFO").upper()),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True)
    # Опрос идёт раз в секунду, и httpx на уровне INFO пишет строку на каждый
    # вызов — 86 тысяч строк в сутки, в которых тонет всё остальное. Молчащий
    # воркер ловится метрикой (инвариант 14), а не чтением такого лога.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    app = FastAPI(
        title="MM-Express — рабочее место сборщика и приёмщика",
        version="1.0.0",
        description="Задания берутся опросом /tasks/pull, а не из шины (раздел 6.1).",
        lifespan=lifespan,
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=config.trusted_hosts(environment))
    app.include_router(router)

    @app.middleware("http")
    async def observe(request: Request, call_next: Any) -> Any:
        started = time.perf_counter()
        response = await call_next(request)
        metrics.observe_http(response.status_code, time.perf_counter() - started)
        return response

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        """Процесс жив. Ничего больше этот ответ не значит — и в этом суть."""
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        """Работа делается: опрос проходит, база отвечает."""
        ready = state.ready()
        body = {
            "status": "ready" if ready else "degraded",
            "polling": state.projection.snapshot(),
            "store_available": bool(state.store and state.store.available),
            "bus_connected": bool(getattr(state.inbox, "connected", False)),
            # Шина не влияет на готовность: склад от неё не зависит (6.1).
            "note": "готовность определяется опросом, а не шиной",
        }
        return JSONResponse(body, status_code=200 if ready else 503)

    @app.get("/metrics")
    async def metrics_endpoint() -> PlainTextResponse:
        payload, content_type = metrics.prometheus_payload(state)
        return PlainTextResponse(payload.decode("utf-8"), media_type=content_type)

    @app.get("/agent/stats")
    async def agent_stats() -> dict[str, Any]:
        """Телеметрия записи в устройство — `PRINT_AGENT_STATS_URL` шага 10 прогона.

        Это единственное число, которое не может измерить никто снаружи:
        `POST /labels/{task_id}/print` меряется по HTTP, а запись RAW-байтов в
        USB-принтер — только на станции.
        """
        last = dict(state.hub.last_write) if state.hub.last_write else {}
        last.setdefault("task_id", None)
        last.setdefault("last_write_ms", None)
        last["agents_connected"] = state.hub.count()
        return last

    # --- экраны -------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def screen_picking() -> HTMLResponse:
        return HTMLResponse(screens.page("picking"))

    @app.get("/receiving", response_class=HTMLResponse)
    async def screen_receiving() -> HTMLResponse:
        return HTMLResponse(screens.page("receiving"))

    @app.get("/putaway", response_class=HTMLResponse)
    async def screen_putaway() -> HTMLResponse:
        return HTMLResponse(screens.page("putaway"))

    @app.get("/inventory", response_class=HTMLResponse)
    async def screen_inventory() -> HTMLResponse:
        return HTMLResponse(screens.page("inventory"))

    @app.get("/shipping", response_class=HTMLResponse)
    async def screen_shipping() -> HTMLResponse:
        return HTMLResponse(screens.page("shipping"))

    @app.get("/supervisor", response_class=HTMLResponse)
    async def screen_supervisor() -> HTMLResponse:
        return HTMLResponse(screens.page("supervisor"))

    @app.get("/picklist/{session_id}", response_class=HTMLResponse)
    async def picklist(session_id: str) -> HTMLResponse:
        """Бумажный лист подбора со штрихкодом.

        Печатается браузером станции. Штрихкод настоящий (Code 128): лист,
        найденный через час на складе, возвращается к своей сессии сканером.
        """
        try:
            view = await state.picking.session_view(session_id)
        except PickingRefused as error:
            return HTMLResponse(screens.error_page(str(error)), status_code=404)
        return HTMLResponse(screens.picklist_page(view))

    return app


app = create_app()
