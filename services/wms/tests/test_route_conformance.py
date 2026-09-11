"""Каждый маршрут заглушки обязан отвечать формой из openapi.yaml.

Заявка 4 потока B: заглушка отвечала не по контракту на восьми маршрутах.
Заглушка, отвечающая не той формой, хуже отсутствующей — потоки B и C
выучивают её форму, а расхождение всплывает в день переключения на настоящий
сервис. Так и вышло: рабочее место научилось понимать обе формы и считать
расхождения счётчиком `mmx_workstation_contract_fallbacks_total`.

Проверяется контракт из репозитория, а не список ожиданий в коде теста: список
ожиданий разъедется с контрактом молча, и никто этого не заметит. Ровно так же
поток A проверял свои 35 маршрутов.

Тест ходит по маршрутам заглушки и валидирует `result` каждого ответа схемой,
на которую ссылается сам openapi.yaml. Маршрут, для которого здесь нет вызова,
считается непроверенным и называется поимённо — «не вызвали» не должно
читаться как «в порядке».
"""
from __future__ import annotations

import hashlib
import os
import pathlib
import uuid
from dataclasses import dataclass
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

import dbfixtures
from app.api import create_app, publisher, state

jsonschema = pytest.importorskip("jsonschema", reason="нужен jsonschema для проверки контракта")
from jsonschema import Draft202012Validator  # noqa: E402
from referencing import Registry, Resource  # noqa: E402
from referencing.jsonschema import DRAFT202012  # noqa: E402

CONTRACT = pathlib.Path(__file__).resolve().parents[1] / "contracts" / "openapi.yaml"
CONTRACT_URI = "urn:mmx:wms:contracts:openapi"
BASE = "/api/mmx/wms/v1"


@pytest.fixture(scope="module")
def contract() -> dict:
    with CONTRACT.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


@pytest.fixture(scope="module")
def registry(contract: dict) -> Registry:
    return Registry().with_resource(
        CONTRACT_URI, Resource.from_contents(contract, default_specification=DRAFT202012))


@pytest.fixture(params=["mock", "real"])
def target(request: pytest.FixtureRequest) -> Any:
    """Заглушка и настоящий сервис проверяются одним и тем же обходом.

    Дрейф настоящего сервиса дороже дрейфа заглушки: заглушку в день
    переключения выключат, а расхождение настоящего сервиса с контрактом
    поедет в бой. Поэтому проверяются оба, одной формой и одним списком
    маршрутов.
    """
    if request.param == "mock":
        state.reset()
        publisher.clear()
        with TestClient(create_app()) as running:
            yield Target(running, "заглушка", mock=True)
        return

    url = dbfixtures.require_database()
    previous = os.environ.get("WMS_MOCK")
    os.environ["WMS_MOCK"] = "false"
    os.environ["DATABASE_URL"] = url
    from app import postgres
    from app.api import create_app as build

    postgres.reset_pool()
    try:
        with TestClient(build()) as running:
            yield Target(running, "настоящий сервис", mock=False)
    finally:
        postgres.reset_pool()
        if previous is None:
            os.environ.pop("WMS_MOCK", None)
        else:
            os.environ["WMS_MOCK"] = previous


@pytest.fixture()
def client() -> Any:
    state.reset()
    publisher.clear()
    return TestClient(create_app())


def result_ref(contract: dict, template: str) -> str | None:
    """На какую схему ссылается контракт для `result` этого маршрута."""
    spec = (contract.get("paths") or {}).get(template)
    if not spec:
        return None
    schema = (spec.get("post", {}).get("responses", {}).get("200", {})
              .get("content", {}).get("application/json", {}).get("schema", {}))
    for part in schema.get("allOf", []):
        ref = (part.get("properties", {}).get("result") or {}).get("$ref")
        if ref:
            return ref
    return None


def check(contract: dict, registry: Registry, template: str, result: Any) -> str | None:
    """Ошибка расхождения с контрактом или None."""
    ref = result_ref(contract, template)
    if ref is None:
        return f"{template}: в контракте нет схемы ответа"
    validator = Draft202012Validator(
        {"$ref": f"{CONTRACT_URI}{ref}"}, registry=registry)
    errors = sorted(validator.iter_errors(result), key=lambda error: list(error.path))
    if not errors:
        return None
    details = "; ".join(
        f"{'/'.join(str(part) for part in error.path) or '<корень>'}: {error.message}"
        for error in errors[:4])
    return f"{template}: {details}"


@dataclass
class Target:
    """Что именно проверяем и как его зовут в сообщении об ошибке."""
    client: TestClient
    name: str
    mock: bool


class Walk:
    """Обход сервиса: шаблон маршрута запоминается вместе с ответом."""

    def __init__(self, target: Target) -> None:
        self._client = target.client
        self.target = target
        self.seen: list[tuple[str, Any]] = []

    def post(self, template: str, path: str | None = None,
             params: dict | None = None) -> dict:
        response = self._client.post(
            f"{BASE}{path or template}", headers=dbfixtures.service_headers(),
            json={"jsonrpc": "2.0", "method": "call", "params": params or {}, "id": 1})
        assert response.status_code == 200, f"{template}: HTTP {response.status_code} — {response.text[:300]}"
        body = response.json()
        assert "result" in body, f"{template}: в ответе нет result — {body}"
        self.seen.append((template, body["result"]))
        return body["result"]


def _put_label(task_id: str) -> None:
    """Кладёт заданию стикер, как это делает воркер после резерва.

    Сам воркер здесь не гоняется намеренно: он глобальный — тянет стикеры для
    ВСЕХ заданий, которым их не хватает, — и, вызванный отсюда, разобрал бы
    работу соседних тестов. Их проверки после этого краснели бы «получено 0
    стикеров из 5», и красное указывало бы не туда.

    Пайплайн стикеров проверяют тесты потока A
    (`test_labels_and_stock_push.py`); здесь проверяется форма ответа, и для
    неё достаточно, чтобы стикер лежал.
    """
    from app.postgres import ConnectionPool

    zpl = b"^XA^FO40,40^A0N,30,30^FDconformance^FS^XZ"
    pool = ConnectionPool(dbfixtures.require_database(), max_size=2)
    try:
        with pool.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO wb_label (id, task_id, format, payload, checksum, version) "
                "VALUES (gen_random_uuid(), %s, 'zplv', %s, %s, 1) "
                "ON CONFLICT (task_id) DO NOTHING",
                (task_id, zpl, hashlib.sha256(zpl).hexdigest()))
            connection.commit()
    finally:
        pool.close()


def walk_every_route(walk: Walk) -> None:
    """Вызывает все маршруты приложения B хотя бы раз.

    Заглушка отвечает фикстурами, у настоящего сервиса своего продавца надо
    завести: база стенда не пустая, поэтому идентификаторы уникальны на запуск
    (`dbfixtures.unique`) и не сталкиваются ни с сидом, ни с прошлым прогоном.
    """
    # Метка обхода. Настоящая база живёт между тестами, и постоянный
    # `wb_order_id` во втором обходе попал бы в идемпотентность (инвариант 5):
    # `/reservations` вернул бы задание ПЕРВОГО обхода, с чужим штрихкодом, и
    # контрольный скан честно бы его отверг. Ошибка выглядела бы как поломка
    # упаковки, а сломан был бы тест.
    tag = uuid.uuid4().hex[:8]
    order = iter(range(7101 + (uuid.uuid4().int % 90000) * 10, 10**9))

    if walk.target.mock:
        seller, barcode = "seller-a", "2000000000011"
        second_barcode = "2000000000042"
        second_seller = "seller-b"
        account = "wb-acc-1"
    else:
        seller = dbfixtures.unique("conf-seller")
        second_seller = seller
        barcode = str(4600000000000 + uuid.uuid4().int % 900000)
        second_barcode = str(4600000000000 + uuid.uuid4().int % 900000)
        walk.post("/sellers", params={
            "seller_external_id": seller, "name": "Проверка контракта",
            "inn": "0000000000"})
        for code in (barcode, second_barcode):
            walk.post("/catalog/products/ensure", params={
                "seller_external_id": seller, "barcode": code,
                "name": f"Проверка контракта {code[-4:]}"})
        # Резерв требует связки «продавец ↔ кабинет»: без кабинета сервис
        # честно отвечает SELLER_MAPPING_MISSING (инвариант 6).
        account = dbfixtures.unique("conf-wb")
        # Резерв требует связки «продавец ↔ кабинет»: без кабинета сервис
        # честно отвечает SELLER_MAPPING_MISSING (инвариант 6).
        #
        # Режим `shadow` намеренно: в нём запись в WB запрещена (раздел 11), и
        # глобальный воркер стикеров этот кабинет не тронет. В `live` он пошёл
        # бы тянуть стикеры для заданий обхода, которых у симулятора нет, и
        # соседние тесты краснели бы «получено 0 стикеров из 5» — красное
        # указывало бы не туда. Свой стикер обход кладёт сам, `_put_label`.
        walk.post("/wb/accounts", params={
            "op": "upsert",
            "owner_external_id": seller,
            "seller_external_id": seller,
            "external_id": account,
            "display_name": "Проверка контракта",
            "mode": "shadow", "status": "ACTIVE",
            "secret_ref": f"vault://mmx/stand/{account}"})

    walk.post("/health")
    walk.post("/sellers", params={"seller_external_id": seller})
    walk.post("/catalog/products", params={"seller_external_id": seller})
    walk.post("/catalog/products/ensure", params={
        "seller_external_id": seller, "barcode": barcode, "name": "Проверка контракта"})
    walk.post("/catalog/stocks", params={"seller_external_id": seller})
    walk.post("/catalog/stocks/bulk", params={"seller_external_id": seller})
    walk.post("/catalog/wb-cards", params={"seller_external_id": seller})

    walk.post("/warehouse/documents", params={
        "seller_external_id": seller, "reference": f"conf-open-{tag}", "doc_type": "opening",
        "warehouse_code": "RUM",
        "lines": [{"barcode": barcode, "quantity": 5,
                   "cell_address": "FR-01-01", "state": "good"}]})
    walk.post("/warehouse/stock", params={"seller_external_id": seller})
    walk.post("/warehouse/movements", params={"seller_external_id": seller, "barcode": barcode})

    if walk.target.mock:
        task_id = walk.post("/reservations", params={
            "seller_external_id": seller, "barcode": barcode, "quantity": 1,
            "wb_order_id": next(order), "idempotency_key": f"conf-{tag}-1",
            "correlation_id": f"conf-corr-{tag}-1"})["task_id"]
    else:
        task_id = walk.post("/reservations", params={
            "seller_external_id": seller, "barcode": barcode, "quantity": 1,
            "wb_order_id": next(order), "idempotency_key": f"conf-{tag}-1",
            "correlation_id": f"conf-corr-{tag}-1"})["task_id"]
        _put_label(task_id)

    # Чтение не занимает — исполнителя не шлём вовсе (контракт требует его
    # только при claim: true).
    walk.post("/tasks/pull", params={"limit": 5, "claim": False})
    walk.post("/tasks/{taskId}", path=f"/tasks/{task_id}")
    walk.post("/tasks/{taskId}/scan", path=f"/tasks/{task_id}/scan", params={"barcode": barcode})
    walk.post("/tasks/{taskId}/pack", path=f"/tasks/{task_id}/pack", params={
        "idempotency_key": f"conf-pack-{tag}", "control_scan_barcode": barcode})
    walk.post("/tasks/{taskId}/label", path=f"/tasks/{task_id}/label")
    walk.post("/labels/{task_id}/print", path=f"/labels/{task_id}/print", params={
        "station_id": "11111111-1111-4111-8111-111111111111",
        "idempotency_key": f"conf-print-{tag}"})

    walk.post("/receipts", params={
        "seller_external_id": seller, "warehouse_code": "RUM", "reference": f"conf-rcpt-{tag}",
        "lines": [{"barcode": barcode, "expected_qty": 2, "actual_qty": 2,
                   "box_barcode": f"CONF-BOX-{tag}-1", "cell_address": "FR-01-02",
                   "comment": "проверка контракта"}]})
    walk.post("/receipts/screen", params={"seller_external_id": seller})
    walk.post("/putaway/screen", params={"seller_external_id": seller})
    walk.post("/discrepancies", params={"owner_external_id": seller})
    walk.post("/inventory/sheet", params={"seller_external_id": seller})
    walk.post("/inventory/count", params={
        "seller_external_id": seller, "warehouse_code": "RUM", "reference": f"conf-cnt-{tag}",
        "scope": "partial",
        "lines": [{"barcode": barcode, "cell_address": "FR-01-01", "fact_qty": 5}]})

    walk.post("/boxes", params={
        "barcode": f"CONF-BOX-{tag}-2", "seller_external_id": seller, "product_barcode": barcode,
        "cell_address": "FR-01-03", "quantity": 3, "comment": "проверка контракта"})
    walk.post("/boxes/list", params={"seller_external_id": seller})
    walk.post("/boxes/remove", params={"barcode": f"CONF-BOX-{tag}-2", "reason": "проверка контракта"})
    walk.post("/storage/lookup", params={"seller_external_id": seller, "barcode": barcode})
    walk.post("/storage/count", params={"seller_external_id": seller, "barcode": barcode})

    walk.post("/shipments", params={"seller_external_id": seller, "action": "open",
                                    "idempotency_key": f"conf-ship-{tag}"})
    walk.post("/shipments/picked", params={"seller_external_id": seller})

    spare = walk.post("/reservations", params={
        "seller_external_id": seller, "barcode": barcode, "quantity": 1,
        "wb_order_id": next(order), "correlation_id": f"conf-corr-{tag}-2"})["task_id"]
    walk.post("/tasks/{taskId}/return-to-shelf", path=f"/tasks/{spare}/return-to-shelf",
              params={"reason": "проверка контракта: товар вернулся на полку"})

    doomed = walk.post("/reservations", params={
        "seller_external_id": seller, "barcode": barcode, "quantity": 1,
        "wb_order_id": next(order), "correlation_id": f"conf-corr-{tag}-3"})["task_id"]
    walk.post("/tasks/{taskId}/cancel", path=f"/tasks/{doomed}/cancel", params={
        "cancellation_event_id": f"conf-cancel-{tag}", "handed_over": False})

    returned = walk.post("/reservations", params={
        "seller_external_id": second_seller, "barcode": second_barcode, "quantity": 1,
        "wb_order_id": next(order), "correlation_id": f"conf-corr-{tag}-4"})["task_id"]
    return_id = walk.post("/tasks/{taskId}/return", path=f"/tasks/{returned}/return", params={
        "return_event_id": f"conf-ret-{tag}", "seller_external_id": second_seller,
        "reason": "покупатель вернул"})["return_id"]
    walk.post("/returns/{returnId}/receive", path=f"/returns/{return_id}/receive")
    walk.post("/returns/{returnId}/decision", path=f"/returns/{return_id}/decision",
              params={"decision": "resellable"})
    walk.post("/returns/receipt", params={"seller_external_id": second_seller})

    walk.post("/wb/accounts", params={"seller_external_id": seller})
    walk.post("/wb/accounts/{id}/verify", path=f"/wb/accounts/{account}/verify")


def test_every_route_answers_in_the_contract_shape(
        target: Target, contract: dict, registry: Registry) -> None:
    """Ни один маршрут не отвечает формой, которой нет в контракте."""
    walk = Walk(target)
    walk_every_route(walk)
    assert walk.seen, "не вызвано ни одного маршрута — проверять нечего"

    problems = [message for template, result in walk.seen
                if (message := check(contract, registry, template, result))]
    assert not problems, (
        f"{target.name} отвечает не по контракту:\n  " + "\n  ".join(problems))


def test_the_walk_covers_every_route_of_the_contract(
        target: Target, contract: dict) -> None:
    """Непроверенный маршрут называется поимённо.

    Иначе «не вызвали» читается как «в порядке» — та же подмена, что и
    пропущенный тест вместо красного.
    """
    walk = Walk(target)
    walk_every_route(walk)
    missed = sorted(set(contract["paths"]) - {template for template, _ in walk.seen})
    assert not missed, f"маршруты контракта не проверены обходом: {missed}"


def test_the_stub_respects_claim_false(client: TestClient) -> None:
    """Заявка 2 потока B: `claim: false` — только посмотреть, не занимать.

    Экран обновляется чаще, чем человек берёт работу. Заглушка занимала
    задания при каждом обновлении: за минуту опроса раз в секунду очередь
    разошлась бы по несуществующей сессии, а симптом выглядел бы как «заданий
    нет» — неотличимо от «новые заказы не падают в приложение».
    """
    for order in (7201, 7202, 7203, 7204):
        client.post(f"{BASE}/reservations", headers=dbfixtures.service_headers(), json={
            "jsonrpc": "2.0", "method": "call", "id": 1,
            "params": {"seller_external_id": "seller-a", "barcode": "2000000000011",
                       "quantity": 1, "wb_order_id": order,
                       "correlation_id": f"peek-{order}"}})

    def pull(assignee: str | None, claim: bool) -> list[str]:
        params: dict[str, Any] = {"limit": 2, "claim": claim}
        if assignee:
            params["assignee"] = assignee
        body = client.post(f"{BASE}/tasks/pull", headers=dbfixtures.service_headers(), json={
            "jsonrpc": "2.0", "method": "call", "id": 1,
            "params": params}).json()["result"]
        return [item["task"]["task_id"] for item in body["tasks"]]

    first = pull(None, claim=False)
    second = pull(None, claim=False)
    assert first, "чтение без занятия не вернуло ничего"
    assert first == second, (
        f"чтение заняло задания: peek-1 увидел {first}, peek-2 уже другие {second}")

    taken = pull("bbbbbbbb-0000-4000-8000-000000000001", claim=True)
    assert taken == first, "занятие обязано отдавать те же задания, что и чтение"
    after = pull(None, claim=False)
    assert set(after).isdisjoint(taken), (
        f"занятые задания продолжают показываться свободными: {after} против {taken}")


def test_the_stub_respects_the_states_filter(client: TestClient) -> None:
    """Заявка 2 потока B: без фильтра задания в работе не видны вовсе."""
    order = walk_state_setup(client)
    in_work = client.post(f"{BASE}/tasks/pull", headers=dbfixtures.service_headers(), json={
        "jsonrpc": "2.0", "method": "call", "id": 1,
        "params": {"limit": 50, "claim": False,
                   "states": ["picking"]}}).json()["result"]["tasks"]
    ids = [item["task"]["task_id"] for item in in_work]
    assert order in ids, (
        f"задание в состоянии picking не видно через states=[picking]: {ids}")


def walk_state_setup(client: TestClient) -> str:
    """Заводит задание и переводит его в `picking`."""
    task_id = client.post(f"{BASE}/reservations", headers=dbfixtures.service_headers(), json={
        "jsonrpc": "2.0", "method": "call", "id": 1,
        "params": {"seller_external_id": "seller-a", "barcode": "2000000000011",
                   "quantity": 1, "wb_order_id": 7301,
                   "correlation_id": "states-7301"}}).json()["result"]["task_id"]
    client.post(f"{BASE}/tasks/pull", headers=dbfixtures.service_headers(), json={
        "jsonrpc": "2.0", "method": "call", "id": 1,
        "params": {"assignee": "bbbbbbbb-0000-4000-8000-000000000009",
                   "limit": 50, "claim": True}})
    return task_id
