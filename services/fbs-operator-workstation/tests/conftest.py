"""Общий сетап тестов рабочего места.

Тесты пишутся против контракта, а не против чужого кода (правило 9.5.3), и
поэтому здесь стоит не заглушка потока 0, а свой маленький wms, отвечающий
ровно по `services/wms/contracts/openapi.yaml`. Расхождение mock с контрактом
проверяется отдельными тестами явно — как расхождение, а не как норма.
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid as uuidlib
from datetime import datetime, timezone
from typing import Any, Callable

import httpx
import pytest

os.environ.setdefault("APP_ENV", "test")

from app.domain import Task  # noqa: E402
from app.wms_client import WmsClient  # noqa: E402


class FakeWms:
    """Сервис по контракту. Ответы задаются тестом, вызовы записываются."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.handlers: dict[str, Callable[[dict[str, Any]], Any]] = {}
        self.fail_with: Exception | None = None
        self.status_code = 200

    def on(self, route: str, handler: Callable[[dict[str, Any]], Any]) -> None:
        self.handlers[route] = handler

    def on_error(self, route: str, *, code: int, message: str) -> None:
        """Отказ конвертом JSON-RPC — так их и отдаёт настоящий wms."""
        def handler(_params: dict[str, Any]) -> httpx.Response:
            return httpx.Response(200, json={
                "jsonrpc": "2.0", "id": 1,
                "error": {"code": code, "message": message}})
        self.handlers[route] = handler

    def transport(self) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            if self.fail_with is not None:
                raise self.fail_with
            route = request.url.path.replace("/api/mmx/wms/v1", "", 1)
            payload = json.loads(request.read() or b"{}")
            params = payload.get("params") or {}
            self.calls.append((route, params))
            handler = self.handlers.get(route)
            if handler is None:
                return httpx.Response(self.status_code,
                                      json={"jsonrpc": "2.0", "id": 1, "result": {}})
            result = handler(params)
            if isinstance(result, httpx.Response):
                return result
            return httpx.Response(self.status_code,
                                  json={"jsonrpc": "2.0", "id": 1, "result": result})
        return httpx.MockTransport(handle)

    def client(self) -> WmsClient:
        return WmsClient("http://wms.test",
                         client=httpx.AsyncClient(base_url="http://wms.test/api/mmx/wms/v1",
                                                  transport=self.transport()))

    def route_calls(self, route: str) -> list[dict[str, Any]]:
        return [params for called, params in self.calls if called == route]


class FakeStore:
    """База рабочего места в памяти — ровно те методы, что зовёт код."""

    def __init__(self) -> None:
        self.available = True
        self.last_error: str | None = None
        self.sessions: dict[str, dict[str, Any]] = {}
        self.lines: dict[str, list[dict[str, Any]]] = {}
        self.scans: list[dict[str, Any]] = []
        self.prints: dict[str, dict[str, Any]] = {}
        self.tasks: dict[str, dict[str, Any]] = {}
        self.printers_by_id: dict[str, dict[str, Any]] = {}

    async def ping(self) -> bool:
        return self.available

    async def close(self) -> None:
        return None

    async def open_session(self, *, actor_id: str, station_id: str | None,
                           picklist_barcode: str) -> str:
        from app.domain import uid
        session_id = uid()
        # uuid и datetime объектами, как их отдаёт psycopg: голый json.dumps на
        # них падает, и это ловится здесь, а не оператором на складе.
        self.sessions[session_id] = {"id": uuidlib.UUID(session_id), "actor_id": actor_id,
                                     "station_id": station_id, "state": "picking",
                                     "picklist_barcode": picklist_barcode,
                                     "started_at": datetime(2026, 9, 10, 10, tzinfo=timezone.utc),
                                     "finished_at": None}
        self.lines[session_id] = []
        return session_id

    async def add_lines(self, session_id: str, tasks: Any) -> int:
        rows = self.lines.setdefault(session_id, [])
        for task in tasks:
            if any(row["task_id"] == task.task_id for row in rows):
                continue
            placement = task.placements[0] if task.placements else None
            rows.append({"task_id": task.task_id, "owner_external_id": task.owner_external_id,
                         "barcode": task.barcode, "name": task.name,
                         "quantity": max(1, task.quantity),
                         "cell_address": task.cell_address, "box_barcode": task.box_barcode,
                         "route_order": placement.route_order if placement else None,
                         "scanned_at": None, "scan_result": None})
        return len(rows)

    async def session_lines(self, session_id: str) -> list[dict[str, Any]]:
        return list(self.lines.get(session_id, []))

    async def session(self, session_id: str) -> dict[str, Any] | None:
        return self.sessions.get(str(session_id))

    async def session_by_barcode(self, picklist_barcode: str) -> dict[str, Any] | None:
        for session in self.sessions.values():
            if session["picklist_barcode"] == picklist_barcode:
                return session
        return None

    async def open_sessions(self) -> list[dict[str, Any]]:
        return [s for s in self.sessions.values() if s["state"] in ("open", "picking")]

    async def finish_session(self, session_id: str, *, state: str = "completed") -> None:
        if session_id in self.sessions:
            self.sessions[session_id]["state"] = state
            self.sessions[session_id]["finished_at"] = "2026-09-10T12:00:00+00:00"

    async def record_scan(self, *, task_id: str, stage: str, barcode: str, scan_result: str,
                          accepted: bool, actor_id: str | None, station_id: str | None = None,
                          session_id: str | None = None) -> None:
        self.scans.append({"task_id": task_id, "stage": stage, "barcode": barcode,
                           "scan_result": scan_result, "accepted": accepted,
                           "actor_id": actor_id, "session_id": session_id,
                           "scanned_at": datetime(2026, 9, 10, 11, tzinfo=timezone.utc)})
        for row in self.lines.get(str(session_id) if session_id else "", []):
            if row["task_id"] == task_id:
                row["scan_result"] = scan_result
                row["scanned_at"] = "2026-09-10T11:00:00+00:00"

    async def rejected_scans(self, limit: int = 50) -> list[dict[str, Any]]:
        return [scan for scan in self.scans if not scan["accepted"]][:limit]

    async def start_print(self, **fields: Any) -> str:
        self.prints.setdefault(fields["idempotency_key"], dict(fields))
        return fields["idempotency_key"]

    async def finish_print(self, *, idempotency_key: str, **fields: Any) -> None:
        self.prints.setdefault(idempotency_key, {}).update(fields)

    async def print_job(self, idempotency_key: str) -> dict[str, Any] | None:
        return self.prints.get(idempotency_key)

    async def last_print(self) -> dict[str, Any] | None:
        return next(iter(reversed(list(self.prints.values()))), None)

    async def print_stats(self) -> dict[str, Any]:
        return {"total": len(self.prints),
                "reprints": sum(1 for job in self.prints.values() if job.get("reprint")),
                "failed": sum(1 for job in self.prints.values()
                              if job.get("outcome") == "failed"),
                "worst_click_to_agent_ms": None, "worst_agent_write_ms": None}

    async def note_task(self, task: Task, **fields: Any) -> None:
        row = self.tasks.setdefault(task.task_id, {"task_id": task.task_id})
        row.update({key: value for key, value in fields.items() if value})

    async def record_cancel(self, *, task_id: str, owner_external_id: str, barcode: str,
                            reason: str, reason_code: str, actor_id: str | None) -> None:
        # Пустая причина не должна доходить до базы: в настоящей схеме её
        # отвергает constraint, здесь — это же условие, чтобы тест ловил
        # регрессию до миграции.
        assert reason and reason.strip(), "отмена без причины нарушает инвариант 11"
        assert reason_code and reason_code.strip()
        self.tasks[task_id] = {"task_id": task_id, "cancelled": True,
                               "cancel_reason": reason, "cancel_reason_code": reason_code,
                               "owner_external_id": owner_external_id, "barcode": barcode}

    async def cancellations_without_reason(self) -> int:
        return 0

    async def upsert_printer(self, *, station_id: str, station_name: str,
                             printer_name: str | None = None, transport: str = "agent") -> None:
        self.printers_by_id[station_id] = {"station_id": station_id,
                                           "station_name": station_name,
                                           "printer_name": printer_name,
                                           "transport": transport, "active": True,
                                           "confirmed_format": None, "probed_at": None}

    async def record_probe(self, *, station_id: str, confirmed_format: str,
                           note: str | None = None) -> None:
        row = self.printers_by_id.setdefault(station_id, {"station_id": station_id})
        row.update({"confirmed_format": confirmed_format,
                    "probed_at": datetime(2026, 9, 10, tzinfo=timezone.utc),
                    "probe_note": note})

    async def printers(self) -> list[dict[str, Any]]:
        return list(self.printers_by_id.values())

    async def printer(self, station_id: str) -> dict[str, Any] | None:
        return self.printers_by_id.get(station_id)


def task_projection(task_id: str = "task-1", **overrides: Any) -> dict[str, Any]:
    """Проекция задания по схеме TaskProjection контракта."""
    row: dict[str, Any] = {
        "task_id": task_id,
        "wb_order_id": 123456,
        "owner_external_id": "seller-1",
        "barcode": "4600000000011",
        "seller_sku": "ART-1",
        "name": "Платье",
        "quantity": 1,
        "deadline": "2026-09-11T12:00:00+00:00",
        "state": "reserved",
        "wb_status": "new",
        "label": {"ready": True, "format": "zplv", "version": 1},
        "assignee": None,
        "claim_expires_at": None,
        "created_at": "2026-09-10T09:00:00+00:00",
        "updated_at": "2026-09-10T09:00:00+00:00",
        "version": 1,
    }
    row.update(overrides)
    return row


def pull_result(*tasks: dict[str, Any], available_total: int | None = None,
                leased_until: str | None = None) -> dict[str, Any]:
    """Ответ /tasks/pull по схеме TasksPullResult."""
    return {
        "tasks": [{"task": task, "leased_until": leased_until,
                   "placements": [{"barcode": task["barcode"], "cell_address": "A-01-01",
                                   "box_barcode": "BOX-1", "state": "good",
                                   "quantity": 5, "route_order": 10}]}
                  for task in tasks],
        "served_at": "2026-09-10T10:00:00+00:00",
        "available_total": len(tasks) if available_total is None else available_total,
    }


def label_result(payload: bytes = b"^XA^FDtest^FS^XZ", *, order_id: Any = 123456,
                 base64_encoded: bool = True, **overrides: Any) -> dict[str, Any]:
    """Ответ этикетки по схеме LabelResult (payload в base64, как в контракте)."""
    import base64 as b64
    body = b64.b64encode(payload).decode("ascii") if base64_encoded else payload.decode()
    row = {
        "id": "label-1", "order_id": order_id, "version": 1,
        "payload": body,
        "content_type": "application/x-zpl", "format": "zplv",
        "checksum": hashlib.sha256(payload).hexdigest(),
    }
    row.update(overrides)
    return row


@pytest.fixture()
def wms() -> FakeWms:
    return FakeWms()


@pytest.fixture()
def store() -> FakeStore:
    return FakeStore()
