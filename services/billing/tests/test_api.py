"""HTTP-поверхность биллинга.

Проверяется не «ручка отвечает 200», а то, ради чего ручки заведены: клиент
видит, из чего складываются 45 ₽, а деньги не превращаются в float по дороге.
"""
from __future__ import annotations

import importlib
from typing import Any, Iterator

import pytest
from starlette.testclient import TestClient

from app.db import Database

from conftest import event


@pytest.fixture
def client(database: Database, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", database.url)
    from app import api as api_module

    importlib.reload(api_module)
    with TestClient(api_module.app) as handle:
        yield handle


def test_readyz_tells_the_truth_about_the_database(client: TestClient) -> None:
    """healthz — про процесс, readyz — про зависимости. Смешивать их значит
    перезапускать контейнер вместо того, чтобы чинить базу."""
    assert client.get("/healthz").json()["status"] == "ok"
    ready = client.get("/readyz")
    assert ready.status_code == 200 and ready.json()["database"] is True


def test_partner_tree_is_the_first_thing_the_admin_sees(
        client: TestClient, stand: dict[str, Any]) -> None:
    body = client.get("/api/billing/v1/partners").json()
    names = [row["name"] for row in body["partners"]]
    assert names == ["Зардал", "Менеджер кабинетов"]
    assert body["partners"][1]["depth"] == 1


def test_the_client_sees_45_and_where_they_go(
        client: TestClient, stand: dict[str, Any]) -> None:
    """Клиент должен видеть 45 и понимать, что 30 — MM-Express, 15 — партнёру,
    а не считать нас источником завышенной цены (файл 04)."""
    client.post("/api/billing/v1/events",
                json=event("wms.packing.completed.v1", {"seller_id": "seller-1"}))

    body = client.get("/api/billing/v1/accruals", params={"period": "2026-09"}).json()
    assert body["count"] == 1
    row = body["accruals"][0]
    assert (row["amount"], row["partner_amount"], row["net_amount"]) == ("45.00", "15.00", "30.00")
    assert row["partner_name"] == "Менеджер кабинетов"
    assert isinstance(row["amount"], str), "деньги ушли числом — 45.00 станет 45.000000000000004"


def test_an_unapproved_price_cannot_be_approved_anonymously(
        client: TestClient, stand: dict[str, Any]) -> None:
    response = client.post(f"/api/billing/v1/tariff-versions/{stand['version']}/approve",
                           json={"approved_by": ""})
    assert response.status_code == 400
    assert "подписи" in response.json()["error"]


def test_closing_a_period_requires_a_name(client: TestClient) -> None:
    """Закрытый период не пересчитывается — значит, известно, кто его закрыл."""
    assert client.post("/api/billing/v1/periods/2026-09/close", json={}).status_code == 400
    ok = client.post("/api/billing/v1/periods/2026-09/close", json={"closed_by": "бухгалтер"})
    assert ok.json()["period"]["state"] == "closed"


def test_unbilled_report_names_what_did_not_reach_the_bill(
        client: TestClient, stand: dict[str, Any]) -> None:
    """Отчёт о потерянной выручке, а не лог ошибок."""
    client.post("/api/billing/v1/events",
                json=event("wms.packing.completed.v1", {"нет_владельца": True}))
    reasons = client.get("/api/billing/v1/reports/unbilled").json()["reasons"]
    assert [row["reason"] for row in reasons] == ["SELLER_UNKNOWN"]
    assert reasons[0]["events"] == 1


def test_cabinets_that_arrived_by_event_are_listed_for_onboarding(
        client: TestClient, stand: dict[str, Any]) -> None:
    client.post("/api/billing/v1/events",
                json=event("wms.packing.completed.v1", {"seller_id": "seller-мимо-процесса"}))
    body = client.get("/api/billing/v1/cabinets", params={"needs_onboarding": True}).json()
    assert [row["seller_external_id"] for row in body["cabinets"]] == ["seller-мимо-процесса"]
