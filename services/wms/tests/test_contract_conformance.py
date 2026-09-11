"""Mock обязан соответствовать контракту, а не своему представлению о нём.

Смысл заглушки в том, что потоки B и C пишут против неё консьюмеры уже
сейчас. Если mock шлёт события не той формы, что описана в asyncapi.yaml, оба
потока напишут неправильных клиентов и узнают об этом через недели — на
интеграции. Поэтому каждое событие, которое mock способен выпустить,
проверяется схемой из самого контракта.

Проверяется контракт, лежащий в репозитории, а не его копия в тесте: копия
однажды разойдётся молча.
"""
from __future__ import annotations

import pathlib

import pytest
import yaml
from fastapi.testclient import TestClient

from app import fixtures
from app.api import create_app, publisher, state

jsonschema = pytest.importorskip("jsonschema", reason="нужен jsonschema для проверки контракта")
from jsonschema import Draft202012Validator  # noqa: E402
from referencing import Registry, Resource  # noqa: E402
from referencing.jsonschema import DRAFT202012  # noqa: E402

CONTRACT = pathlib.Path(__file__).resolve().parents[1] / "contracts" / "asyncapi.yaml"
CONTRACT_URI = "urn:mmx:wms:contracts:asyncapi"
BASE = "/api/mmx/wms/v1"


@pytest.fixture(scope="module")
def contract() -> dict:
    with CONTRACT.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


@pytest.fixture(scope="module")
def registry(contract: dict) -> Registry:
    # Контракт регистрируется под явным URI, и ссылки на его части даются
    # абсолютно. Относительный "#/..." резолвился бы внутри самой ссылки,
    # а не внутри документа.
    return Registry().with_resource(
        CONTRACT_URI, Resource.from_contents(contract, default_specification=DRAFT202012))


@pytest.fixture()
def client() -> TestClient:
    state.reset()
    publisher.clear()
    return TestClient(create_app())


def call(client: TestClient, path: str, params: dict | None = None) -> dict:
    response = client.post(
        f"{BASE}{path}",
        json={"jsonrpc": "2.0", "method": "call", "params": params or {}, "id": 1})
    assert response.status_code == 200, response.text
    return response.json()["result"]


def message_name_for(contract: dict, event_type: str) -> str:
    for name, message in (contract.get("components", {}).get("messages") or {}).items():
        if message.get("name") == event_type:
            return name
    raise AssertionError(
        f"события {event_type!r} нет в каталоге контракта — либо mock выдумал "
        f"событие, либо контракт неполон")


def validate_event(contract: dict, registry: Registry, event: dict) -> None:
    name = message_name_for(contract, event["type"])
    validator = Draft202012Validator(
        {"$ref": f"{CONTRACT_URI}#/components/messages/{name}/payload"}, registry=registry)
    errors = sorted(validator.iter_errors(event), key=lambda error: list(error.path))
    if errors:
        details = "; ".join(
            f"{'/'.join(str(part) for part in error.path) or '<корень>'}: {error.message}"
            for error in errors)
        raise AssertionError(f"событие {event['type']} не проходит свою схему: {details}")


def exercise_everything(client: TestClient) -> None:
    """Прогоняет mock по всем путям, которые выпускают события."""
    ok = call(client, "/reservations", {
        "seller_external_id": "seller-a", "barcode": "2000000000011", "quantity": 1,
        "wb_order_id": 4101, "idempotency_key": "idem-4101", "correlation_id": "corr-4101"})
    task_id = ok["task_id"]

    # Отказ маппинга — тоже событие (wms.reservation.failed.v1).
    call(client, "/reservations", {
        "seller_external_id": "seller-a", "barcode": "0000000000000", "quantity": 1,
        "wb_order_id": 4102, "correlation_id": "corr-4102"})

    # Клапан «собрать без остатка» (раздел 6.5).
    call(client, "/reservations", {
        "seller_external_id": "seller-a", "barcode": fixtures.ZERO_STOCK_BARCODE,
        "quantity": 3, "wb_order_id": 4103, "correlation_id": "corr-4103"})

    call(client, f"/tasks/{task_id}/scan", {"barcode": "2000000000011"})
    call(client, f"/tasks/{task_id}/pack")
    call(client, f"/labels/{task_id}/print")

    other = call(client, "/reservations", {
        "seller_external_id": "seller-a", "barcode": "2000000000028", "quantity": 1,
        "wb_order_id": 4104, "correlation_id": "corr-4104"})["task_id"]
    call(client, f"/tasks/{other}/return-to-shelf")

    third = call(client, "/reservations", {
        "seller_external_id": "seller-a", "barcode": "2000000000011", "quantity": 1,
        "wb_order_id": 4105, "correlation_id": "corr-4105"})["task_id"]
    call(client, f"/tasks/{third}/cancel", {"reason": "клиент отменил"})

    fourth = call(client, "/reservations", {
        "seller_external_id": "seller-b", "barcode": "2000000000042", "quantity": 1,
        "wb_order_id": 4106, "correlation_id": "corr-4106"})["task_id"]
    return_id = call(client, f"/tasks/{fourth}/return", {
        "return_event_id": "evt-return-1", "seller_external_id": "seller-b",
        "reason": "покупатель вернул"})["return_id"]
    call(client, f"/returns/{return_id}/receive")
    call(client, f"/returns/{return_id}/decision", {"decision": "resellable"})

    fifth = call(client, "/reservations", {
        "seller_external_id": "seller-b", "barcode": "2000000000042", "quantity": 1,
        "wb_order_id": 4107, "correlation_id": "corr-4107"})["task_id"]
    sixth_return = call(client, f"/tasks/{fifth}/return", {
        "return_event_id": "evt-return-2", "seller_external_id": "seller-b",
        "reason": "брак"})["return_id"]
    call(client, f"/returns/{sixth_return}/receive")
    call(client, f"/returns/{sixth_return}/decision", {"decision": "defective"})


def test_every_emitted_event_matches_its_contract_schema(
        client: TestClient, contract: dict, registry: Registry) -> None:
    exercise_everything(client)
    published = publisher.published
    assert published, "mock не выпустил ни одного события — проверять нечего"
    for event in published:
        validate_event(contract, registry, event)


def test_shortfall_carries_exactly_the_five_contract_fields(
        client: TestClient, contract: dict, registry: Registry) -> None:
    """Пункт 3 файла 01 задаёт этому payload ровно пять полей.

    Схема стоит с additionalProperties: false, поэтому лишнее поле — не
    «немного больше данных», а нарушение контракта.
    """
    call(client, "/reservations", {
        "seller_external_id": "seller-a", "barcode": fixtures.ZERO_STOCK_BARCODE,
        "quantity": 4, "wb_order_id": 4201, "correlation_id": "corr-4201"})

    shortfall = [e for e in publisher.published if e["type"] == "wms.stock.shortfall.v1"]
    assert len(shortfall) == 1
    assert set(shortfall[0]["payload"]) == {
        "owner_id", "sku_id", "cell_id", "qty_short", "task_id"}
    validate_event(contract, registry, shortfall[0])


def test_identifiers_are_real_uuids(client: TestClient) -> None:
    """Контракт требует формат uuid.

    Читаемая заглушка вида "owner-seller-a" учит консьюмеры неправильному
    формату, и поток B ловит это уже на настоящем сервисе.
    """
    import uuid

    call(client, "/reservations", {
        "seller_external_id": "seller-a", "barcode": "2000000000011", "quantity": 1,
        "wb_order_id": 4301, "correlation_id": "corr-4301"})

    checked = 0
    for event in publisher.published:
        for field in ("owner_id", "sku_id", "task_id", "cell_id"):
            value = event["payload"].get(field)
            if value is None:
                continue
            uuid.UUID(str(value))  # бросит ValueError, если не uuid
            checked += 1
    assert checked, "в событиях не оказалось ни одного идентификатора"


# ------------------------------------------- каталог событий и список эмиссии

def test_the_catalogue_and_the_emission_list_agree() -> None:
    """Каждое событие, которое `wms` вправе издать, описано в каталоге.

    Список эмиссии — код, каталог — контракт. Они расходятся молча: событие,
    которого нет в каталоге, потребитель не разберёт, а канал без эмиссии
    выглядит рабочим и не даёт ничего. Так `wms.stock.released.v1` год лежал
    в каталоге, и издавать его было некому.
    """
    from app.domain import INVENTORY_EVENT_TYPES, WB_EVENT_TYPES, WMS_EVENT_TYPES

    with CONTRACT.open(encoding="utf-8") as handle:
        document = yaml.safe_load(handle)
    channels = {str(name) for name in (document.get("channels") or {})}
    assert channels, "в каталоге нет ни одного канала"

    emitted = WMS_EVENT_TYPES | INVENTORY_EVENT_TYPES | WB_EVENT_TYPES
    missing = sorted(emitted - channels)
    assert not missing, (
        f"wms вправе издать события, которых нет в каталоге: {missing}. "
        f"Потребитель их не разберёт")

    # Обратное направление — только для каналов, которые `wms` ОБЪЯВЛЯЕТ
    # издаваемыми (`action: send`). Каталог описывает всё, что ходит по
    # `mmx.events`: и унаследованные имена боевого контура, и четыре команды,
    # которые `wms` принимает, а не издаёт.
    sent: set[str] = set()
    for operation in (document.get("operations") or {}).values():
        if operation.get("action") != "send":
            continue
        reference = ((operation.get("channel") or {}).get("$ref") or "")
        if reference.startswith("#/channels/"):
            sent.add(reference.removeprefix("#/channels/"))
    orphaned = sorted(sent - emitted)
    assert not orphaned, (
        f"каналы wms без эмиссии: {orphaned}. Канал, который никто не издаёт, "
        f"выглядит рабочим и не даёт ничего")
