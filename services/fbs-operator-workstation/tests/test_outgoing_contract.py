"""Каждый запрос к `wms` проверяется по его же контракту.

Рабочее место и `wms` пишут разные люди, и расхождение между ними видно не
сразу: отправленное лишнее поле сервис молча игнорирует, отсутствующее
обязательное — отвергает уже в бою. Здесь запросы, которые рабочее место
действительно отправляет в своих тестах, валидируются по `openapi.yaml`.

Проверка идёт по ФАКТИЧЕСКИМ вызовам, а не по списку: список устаревает молча,
а факт — нет.
"""
from __future__ import annotations

import pathlib
import re
from typing import Any

import pytest
import yaml

jsonschema = pytest.importorskip("jsonschema", reason="нужен jsonschema")
from jsonschema import Draft202012Validator  # noqa: E402
from referencing import Registry, Resource  # noqa: E402
from referencing.jsonschema import DRAFT202012  # noqa: E402

CONTRACT = (pathlib.Path(__file__).resolve().parents[3]
            / "services" / "wms" / "contracts" / "openapi.yaml")
CONTRACT_URI = "urn:mmx:wms:contracts:openapi"


@pytest.fixture(scope="module")
def contract() -> dict:
    if not CONTRACT.is_file():
        pytest.skip(f"контракт wms не найден по {CONTRACT}")
    with CONTRACT.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


@pytest.fixture(scope="module")
def registry(contract: dict) -> Registry:
    return Registry().with_resource(
        CONTRACT_URI, Resource.from_contents(contract, default_specification=DRAFT202012))


def _params_pointer(contract: dict, path: str) -> str | None:
    """JSON-указатель на схему `params` маршрута.

    Возвращается именно указатель, а не вырезанная схема: внутри неё ссылки
    вида `#/components/...` абсолютны относительно ВСЕГО документа, и
    вырезанный кусок их не разрешает.
    """
    entry = (contract.get("paths") or {}).get(path)
    if not entry:
        return None
    body = ((entry.get("post") or {}).get("requestBody") or {})
    schema = (((body.get("content") or {}).get("application/json") or {}).get("schema") or {})
    base = f"/paths/{path.replace('~', '~0').replace('/', '~1')}" \
           f"/post/requestBody/content/application~1json/schema"
    branches = schema.get("allOf")
    if branches:
        for index, branch in enumerate(branches):
            if (branch.get("properties") or {}).get("params"):
                return f"{base}/allOf/{index}/properties/params"
        return None
    if (schema.get("properties") or {}).get("params"):
        return f"{base}/properties/params"
    return None


# Имя параметра в пути у разных маршрутов разное: `/labels/{task_id}/print`
# назван так в приложении B мастера, остальные — `{taskId}`. Расхождение
# сохранено намеренно (комментарий в контракте), поэтому здесь перебираются
# оба написания, а не выбирается одно.
_PLACEHOLDERS = ("{taskId}", "{task_id}", "{returnId}", "{id}")


def _contract_paths(route: str, known: set[str]) -> str | None:
    """Путь клиента → путь контракта: `/tasks/abc/scan` → `/tasks/{taskId}/scan`.

    Совпадение ищется по контракту, а не угадывается: список путей и есть
    источник истины о том, как они названы.
    """
    if route in known:
        return route
    parts = route.strip("/").split("/")
    for index in range(len(parts)):
        for placeholder in _PLACEHOLDERS:
            candidate = "/" + "/".join(
                placeholder if position == index else part
                for position, part in enumerate(parts))
            if candidate in known:
                return candidate
    return None


def _collect_calls() -> list[tuple[str, dict[str, Any]]]:
    """Все вызовы к wms, которые делают тесты рабочего места.

    Гоняем чужие тесты внутри этого и смотрим, что они отправили: список
    маршрутов вручную устаревает молча, а факт — нет.
    """
    import asyncio

    from conftest import FakeStore, FakeWms, label_result, pull_result, task_projection

    from app.picking import PickingService
    from app.printing import PrintService
    from app.projection import Projection
    from app.puller import Poller
    from app.agent_hub import AgentHub, AgentSession
    from app.receiving import ReceivingService

    wms = FakeWms()
    wms.on("/tasks/pull", lambda params: pull_result(task_projection("a")))
    wms.on("/tasks/a", lambda params: task_projection("a"))
    wms.on("/tasks/a/scan", lambda params: {"task_id": "a", "status": "picked",
                                            "owner_external_id": "seller-1",
                                            "state": "picked", "version": 2,
                                            "duplicate": False,
                                            "barcode": "4600000000011",
                                            "scan_result": "ok"})
    wms.on("/tasks/a/pack", lambda params: {"task_id": "a", "state": "packed",
                                            "owner_external_id": "seller-1",
                                            "version": 3, "duplicate": False})
    wms.on("/tasks/a/cancel", lambda params: {"task_id": "a", "state": "cancelled",
                                              "owner_external_id": "seller-1",
                                              "version": 4, "duplicate": False,
                                              "cancel_reason": "брак: порвана упаковка"})
    wms.on("/tasks/a/return-to-shelf", lambda params: {"task_id": "a", "state": "reserved",
                                                      "owner_external_id": "seller-1",
                                                      "version": 5, "duplicate": False})
    wms.on("/labels/a/print", lambda params: dict(
        label_result(), task_id="a", station_id="st-1", order_id=123456))
    wms.on("/storage/lookup", lambda params: {"seller_external_id": "seller-1",
                                              "barcode": "4600000000011",
                                              "placements": []})
    wms.on("/receiving/receive", lambda params: {"receipt_id": "r-1",
                                                 "reference": "ТН-1",
                                                 "owner_external_id": "seller-1",
                                                 "state": "accepted",
                                                 "lines_accepted": 1,
                                                 "discrepancies": [],
                                                 "duplicate": False,
                                                 "owner_created": False})

    client = wms.client()
    projection, store, hub = Projection(), FakeStore(), AgentHub()

    class Socket:
        async def send_json(self, message: dict) -> None:
            if message.get("type") == "print":
                hub.resolve(message["job_id"], {"ok": True, "write_ms": 2.0},
                            station_id="st-1")

        async def close(self) -> None:
            return None

    async def scenario() -> None:
        await hub.register(AgentSession(station_id="st-1", station_name="Станция 1",
                                        websocket=Socket()))
        poller = Poller(client, projection, interval_seconds=60, limit=50)
        picking = PickingService(client, projection, store, poller)
        printing = PrintService(client, hub, store, projection)
        receiving = ReceivingService(client)

        await poller.poll_once()
        await picking.start_session(actor_id="picker-1", station_id="st-1",
                                    limit=10, lease_seconds=900)
        await picking.scan_at_rack(task_id="a", barcode="4600000000011",
                                   actor_id="picker-1", station_id="st-1")
        await picking.pack(task_id="a", control_barcode="4600000000011",
                           actor_id="picker-1", station_id="st-1")
        await printing.print_label(task_id="a", station_id="st-1")
        await picking.return_to_shelf(task_id="a", cell_address="A-01-01",
                                      actor_id="picker-1", reason="коробка не открывается")
        await picking.cancel(task_id="a", reason_code="operator_damaged",
                             comment="порвана упаковка", actor_id="picker-1")
        await receiving.submit_receipt(
            seller_external_id="seller-1", reference="ТН-1",
            lines=[{"barcode": "4600000000011", "expected_qty": 1, "actual_qty": 1,
                    "cell_address": "A-01-01", "comment": "приёмка"}],
            actor_id="picker-1")
        await client.aclose()

    asyncio.run(scenario())
    return wms.calls


def test_every_request_the_workstation_sends_fits_the_contract(
        contract: dict, registry: Registry) -> None:
    """Ни одного лишнего поля и ни одного пропущенного обязательного.

    Лишнее поле `wms` молча игнорирует — и рабочее место годами думает, что
    отправляет то, что нужно. Пропущенное обязательное отвергается уже в бою.
    """
    calls = _collect_calls()
    assert calls, "рабочее место не сделало ни одного вызова — проверять нечего"
    known = set(contract.get("paths") or {})

    checked, skipped = 0, []
    problems: list[str] = []
    for route, params in calls:
        path = _contract_paths(route, known)
        pointer = _params_pointer(contract, path) if path else None
        if pointer is None:
            skipped.append(f"{route} → {path}")
            continue
        checked += 1
        validator = Draft202012Validator({"$ref": f"{CONTRACT_URI}#{pointer}"},
                                         registry=registry)
        for error in sorted(validator.iter_errors(params), key=lambda item: item.path):
            location = "/".join(str(part) for part in error.path) or "(корень)"
            problems.append(f"{route} ({path}), поле {location}: {error.message}")

    assert not skipped, (
        f"маршруты, которых нет в контракте wms: {skipped}. "
        f"Либо рабочее место зовёт то, чего нет, либо путь собран неверно")
    assert not problems, (
        "запросы рабочего места не сходятся с контрактом wms:\n  "
        + "\n  ".join(problems))
    assert checked >= 8, f"проверено всего {checked} вызовов — сценарий не покрывает путь"
