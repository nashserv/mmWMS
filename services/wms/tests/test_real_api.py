"""Настоящие маршруты против настоящей базы — по контракту, а не по реализации.

Заглушка потока 0 отвечала фикстурами и своего реестра продавцов в Postgres не
имела: `/reservations` на реально существующего `stand-seller-001` отвечал
`SELLER_MAPPING_MISSING` (reference/stand-seed/README.md). Здесь проверяется
обратное — что сервис читает те самые таблицы.
"""
from __future__ import annotations

import os
import pathlib
import re
import uuid

import pytest
import yaml
from fastapi.testclient import TestClient

from dbfixtures import require_database, unique

BASE = "/api/mmx/wms/v1"


@pytest.fixture(scope="module")
def client() -> TestClient:
    url = require_database()
    os.environ["WMS_MOCK"] = "false"
    os.environ["DATABASE_URL"] = url
    # Импорт после переменных окружения: пул поднимается на сборке приложения.
    from app import postgres
    from app.api import create_app

    postgres.reset_pool()
    with TestClient(create_app()) as running:
        yield running
    postgres.reset_pool()
    os.environ["WMS_MOCK"] = "true"


def call(client: TestClient, path: str, params: dict | None = None) -> dict:
    response = client.post(f"{BASE}{path}",
                           json={"jsonrpc": "2.0", "method": "call",
                                 "params": params or {}, "id": 7})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["jsonrpc"] == "2.0" and body["id"] == 7
    assert "error" not in body, body["error"]
    return body["result"]


@pytest.fixture()
def seller(client: TestClient) -> dict:
    name = unique("seller")
    barcode = f"46{uuid.uuid4().int % 10**11:011d}"
    call(client, "/sellers", {"seller_external_id": name, "name": "Тест маршрутов",
                              "inn": "0000000000", "allow_ledger_short": True})
    call(client, "/catalog/products/ensure", {
        "seller_external_id": name, "barcode": barcode, "seller_sku": "API-1",
        "name": "Товар маршрутов"})
    call(client, "/wb/accounts", {
        "op": "upsert", "external_id": f"{name}-wb", "seller_external_id": name,
        "display_name": "Кабинет маршрутов", "secret_ref": f"vault://mmx/test/{name}",
        "mode": "shadow", "status": "ACTIVE"})
    return {"seller": name, "barcode": barcode, "account": f"{name}-wb",
            "cell": unique("cell").upper()}


def test_seeded_seller_is_visible_to_reservation(client: TestClient) -> None:
    """Сид стенда перестаёт быть мёртвым грузом.

    Заглушка держала свой in-memory реестр и посеянных продавцов не видела.
    Настоящий сервис обязан отвечать по `owner`, а не по фикстуре.
    """
    # Списка владельцев контракт не отдаёт: `/sellers` — это один владелец
    # (SellerResult). Проверяем на посеянном: он обязан находиться в таблице
    # `owner`, а не в фикстуре.
    seeded = call(client, "/sellers", {"seller_external_id": "stand-seller-001"})
    assert seeded["owner_external_id"] == "stand-seller-001"
    assert seeded["created"] is False, "посеянный продавец завёлся заново — сид мёртв"
    assert seeded["owner_id"], "маршрут не читает таблицу owner"


def test_opening_stock_lands_in_the_ledger(client: TestClient, seller: dict) -> None:
    """Шаг 1 прогона: начальный остаток оставляет след с doc_type='opening'."""
    reference = unique("opening")
    result = call(client, "/warehouse/documents", {
        "seller_external_id": seller["seller"], "warehouse_code": "RUM",
        "reference": reference, "doc_type": "opening",
        "comment": "начальный остаток от владельца компании",
        "lines": [{"barcode": seller["barcode"], "quantity": 10,
                   "cell_address": seller["cell"], "state": "good"}]})

    assert result["state"] == "applied"
    assert result["moves"] == 1 and result["duplicate"] is False

    placements = call(client, "/warehouse/stock", {"seller_external_id": seller["seller"]})
    good = [row for row in placements["rows"]
            if row["barcode"] == seller["barcode"] and row["state"] == "good"]
    assert good and good[0]["quantity"] == 10
    assert good[0]["cell_address"] == seller["cell"], "ячейка не заведена по адресу строки"


def test_reservation_answers_by_contract(client: TestClient, seller: dict) -> None:
    """Приложение C: клиент считает задание принятым только при status == 'reserved'."""
    call(client, "/warehouse/documents", {
        "seller_external_id": seller["seller"], "reference": unique("open"),
        "doc_type": "opening", "warehouse_code": "RUM",
        "lines": [{"barcode": seller["barcode"], "quantity": 4,
                   "cell_address": seller["cell"], "state": "good"}]})

    result = call(client, "/reservations", {
        "idempotency_key": unique("idem"), "seller_external_id": seller["seller"],
        "wb_account_external_id": seller["account"],
        "wb_order_id": uuid.uuid4().int % 10**12, "sku": seller["barcode"],
        "barcode": seller["barcode"], "quantity": 2, "correlation_id": unique("corr")})

    assert result["status"] == "reserved"
    assert result["owner_external_id"] == seller["seller"]
    # Поля без значения не едут: отсутствующий ключ и null значат для клиента
    # одно и то же, а контракт объявляет типы строго.
    assert result.get("error_code") is None
    assert result["ledger_short"] is False
    # additionalProperties: false — лишних полей в ответе нет.
    assert set(result) <= {"status", "task_id", "owner_external_id", "error_code",
                           "reservation_id", "ledger_short"}


def test_published_stock_is_always_lowered(client: TestClient, seller: dict) -> None:
    """Инвариант 7: в WB публикуется good − buffer, и никогда больше."""
    call(client, "/warehouse/documents", {
        "seller_external_id": seller["seller"], "reference": unique("open"),
        "doc_type": "opening", "warehouse_code": "RUM",
        "lines": [{"barcode": seller["barcode"], "quantity": 10,
                   "cell_address": seller["cell"], "state": "good"}]})
    call(client, "/reservations", {
        "idempotency_key": unique("idem"), "seller_external_id": seller["seller"],
        "wb_account_external_id": seller["account"],
        "wb_order_id": uuid.uuid4().int % 10**12, "sku": seller["barcode"],
        "barcode": seller["barcode"], "quantity": 3, "correlation_id": unique("corr")})

    published = call(client, "/catalog/stocks/bulk",
                     {"seller_external_id": seller["seller"]})
    rows = {row["barcode"]: row["available"] for row in published["stocks"]}
    # good = 10 − 3 = 7 (движение good → reserved), buffer = 0. Резерв в формуле
    # не участвует: он уже вычтен движением, и вычесть его второй раз значит
    # опубликовать 4 при семи физически свободных (мастер 1.3, раздел 6.4).
    assert rows[seller["barcode"]] == 7, "публикуемый остаток разошёлся с инвариантом 7"
    assert all(row["available"] >= 0 for row in published["stocks"])


def test_missing_sku_is_a_protocol_error_not_a_silent_rejection(
        client: TestClient, seller: dict) -> None:
    """Пустой `sku` клиент не может разобрать по контракту — это ошибка протокола."""
    response = client.post(f"{BASE}/reservations", json={
        "jsonrpc": "2.0", "method": "call", "id": 9,
        "params": {"idempotency_key": unique("idem"),
                   "seller_external_id": seller["seller"], "wb_order_id": 1, "quantity": 1}})
    body = response.json()
    assert "error" in body and body["error"]["code"] == -32602


def test_wb_account_never_returns_a_token(client: TestClient, seller: dict) -> None:
    """Инвариант 15: в ответе есть ссылка на секрет и нет ни одного его значения."""
    result = call(client, "/wb/accounts", {"owner_external_id": seller["seller"]})
    accounts = [row for row in result["accounts"] if row["external_id"] == seller["account"]]
    assert accounts, "кабинет не найден"
    account = accounts[0]
    assert account["secret_ref"].startswith("vault://")
    assert not any(key in account for key in ("token", "secret", "access_token", "api_key"))


def test_a_live_token_is_refused_as_a_secret_ref(client: TestClient, seller: dict) -> None:
    """Раздел 12: живой токен на стенде отправил бы команды в кабинет клиента."""
    response = client.post(f"{BASE}/wb/accounts", json={
        "jsonrpc": "2.0", "method": "call", "id": 3,
        "params": {"op": "upsert", "external_id": unique("wb"),
                   "seller_external_id": seller["seller"], "display_name": "нельзя",
                   "secret_ref": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload.signature"}})
    body = response.json()
    assert "error" in body, "живой JWT принят как ссылка на секрет"
    assert "ссылка на секрет" in body["error"]["message"]


def test_every_contract_route_is_served(client: TestClient, seller: dict) -> None:
    """Ни один маршрут контракта не отвечает «этого ещё нет».

    Проверяется контракт из репозитория, а не его список в тесте: копия
    однажды разойдётся молча. Маршрут может отказать по существу — не найдено
    задание, не хватает параметра, — но не имеет права ответить -32601:
    заглушка под настоящим именем это то, как заглушки доезжают до прода.
    """
    contract = yaml.safe_load(
        (pathlib.Path(__file__).resolve().parents[1] / "contracts" / "openapi.yaml")
        .read_text(encoding="utf-8"))
    # Пути в контракте относительны базового адреса сервера — приставку
    # добавляем сами, как это делает любой сгенерированный клиент.
    paths = [BASE + path for path in contract["paths"]]
    assert len(paths) >= 29, f"в контракте {len(paths)} маршрутов — приложение B обещает 29+"

    unimplemented, missing_route = [], []
    for path in paths:
        # Подставляем что угодно похожее на идентификатор: маршрут обязан
        # ответить по существу, а не «такого пути нет».
        concrete = re.sub(r"\{[^}]+\}", str(uuid.uuid4()), path)
        response = client.post(concrete, json={
            "jsonrpc": "2.0", "method": "call", "id": 1,
            "params": {"seller_external_id": seller["seller"]}})
        if response.status_code == 404:
            missing_route.append(path)
            continue
        error = response.json().get("error") or {}
        if error.get("code") == -32601:
            unimplemented.append(path)

    assert not missing_route, f"маршрутов нет вовсе: {missing_route}"
    assert not unimplemented, f"маршруты отвечают «ещё не реализовано»: {unimplemented}"
