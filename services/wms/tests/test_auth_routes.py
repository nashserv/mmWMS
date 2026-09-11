"""Маршруты `wms` спрашивают, кто пришёл. Раздел 12.

До этих тестов все 38 маршрутов были открыты, хотя `openapi.yaml` объявляет
`bearerAuth`. Любой, у кого есть сетевой доступ к стенду, мог завести кабинет
Wildberries, снять резерв или отгрузить чужой товар.

Отдельно проверяется `/wb/accounts`: за ним стоят токены WB, и право их менять
не то же самое, что право собирать заказы (раздел 6.7).
"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from dbfixtures import require_database, service_headers, unique

BASE = "/api/mmx/wms/v1"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    url = require_database()
    monkeypatch.setenv("WMS_MOCK", "false")
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("SERVICE_TOKEN", "test-service-token-not-a-secret")
    from app import postgres
    from app.api import create_app

    postgres.reset_pool()
    with TestClient(create_app()) as running:
        yield running
    postgres.reset_pool()


def call(client: TestClient, path: str, headers: dict | None = None,
         params: dict | None = None):
    return client.post(f"{BASE}{path}", headers=headers or {},
                       json={"jsonrpc": "2.0", "method": "call",
                             "params": params or {}, "id": 1})


def test_a_route_without_a_token_is_refused(client: TestClient) -> None:
    """Без токена 401. Раньше — 200 и заведённый продавец."""
    response = call(client, "/sellers", params={"seller_external_id": unique("nobody")})
    assert response.status_code == 401
    assert "Bearer" in response.json()["error"]["message"]


def test_health_answers_without_a_token(client: TestClient) -> None:
    """По `/health` смотрят, жив ли сервис. Требовать для этого identity значит
    связать готовность склада с готовностью соседа."""
    assert call(client, "/health").status_code == 200


def test_a_service_token_is_accepted(client: TestClient) -> None:
    """Рабочее место, каталог и возвраты зовут `wms` не от имени человека."""
    response = call(client, "/sellers", headers=service_headers(),
                    params={"seller_external_id": unique("svc"), "name": "Проверка"})
    assert response.status_code == 200


def test_a_wrong_service_token_is_refused(client: TestClient) -> None:
    """Сравнение секретов постоянного времени — и всё же отказ."""
    response = call(client, "/sellers", headers={"Authorization": "Bearer wrong-service-token"},
                    params={"seller_external_id": unique("bad")})
    assert response.status_code == 401


def test_wb_accounts_need_their_own_right(client: TestClient, monkeypatch) -> None:
    """За `/wb/accounts` стоят токены Wildberries.

    Человек со сборочной ролью туда не ходит: право менять кабинеты — это
    право отправлять команды в кабинет клиента (раздел 6.7).
    """
    from app import auth

    # Человек с ролью picker: подпись подлинная, роли не те.
    monkeypatch.setattr(auth, "_decode", lambda token: {"sub": str(uuid.uuid4())})
    monkeypatch.setattr(auth, "_introspect", lambda token: {
        "user_id": str(uuid.uuid4()), "roles": ["picker"],
        "scopes": [{"role": "picker", "kind": "global"}]})

    response = call(client, "/wb/accounts", headers={"Authorization": "Bearer signature-ok"},
                    params={"op": "upsert", "external_id": unique("wb")})
    assert response.status_code == 403, "сборщик не должен управлять кабинетами WB"


def test_wb_accounts_accept_the_right_role(client: TestClient, monkeypatch) -> None:
    from app import auth

    monkeypatch.setattr(auth, "_decode", lambda token: {"sub": str(uuid.uuid4())})
    monkeypatch.setattr(auth, "_introspect", lambda token: {
        "user_id": str(uuid.uuid4()), "roles": ["wb_accounts_admin"],
        "scopes": [{"role": "wb_accounts_admin", "kind": "global"}]})

    response = call(client, "/wb/accounts", headers={"Authorization": "Bearer signature-ok"},
                    params={})
    assert response.status_code == 200
