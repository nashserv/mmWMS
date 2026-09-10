"""Роли и области. Механика токенов сюда не входит — она чужая (файл 04)."""
from __future__ import annotations

import importlib
import uuid
from typing import Iterator

import pytest
from starlette.testclient import TestClient

from app.db import Database
from app.roles import RoleDirectory

ZARDAL_BRANCH = "367f8fe7-e37d-59d1-b685-5a7ef0e04c07"


@pytest.fixture
def client(database: Database, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", database.url)
    monkeypatch.delenv("IDENTITY_UPSTREAM_URL", raising=False)
    from app import api as api_module

    importlib.reload(api_module)
    with TestClient(api_module.app) as handle:
        yield handle


def test_the_four_warehouse_roles_joined_the_four_that_existed(client: TestClient) -> None:
    """Файл 04: identity получает warehouse_head, picker, receiver, logist."""
    codes = {row["code"] for row in client.get("/api/identity/v1/roles").json()["roles"]}
    assert {"owner", "manager", "admin", "accountant"} <= codes, "существующие роли потеряны"
    assert {"warehouse_head", "picker", "receiver", "logist"} <= codes


def test_a_grant_carries_its_scope_and_its_author(database: Database) -> None:
    directory = RoleDirectory(database)
    user = str(uuid.uuid4())

    grant = directory.grant(user, "senior_manager", "админ Пётр",
                            scope_kind="partner_branch", scope_id=ZARDAL_BRANCH)

    assert grant["granted_by"] == "админ Пётр"
    principal = directory.principal(user)
    assert principal["roles"] == ["senior_manager"]
    assert principal["partner_branches"] == [ZARDAL_BRANCH]


def test_granting_the_same_role_twice_leaves_one_grant(database: Database) -> None:
    """Две записи об одном праве — это отзыв, который снимает половину."""
    directory = RoleDirectory(database)
    user = str(uuid.uuid4())
    first = directory.grant(user, "picker", "админ")
    second = directory.grant(user, "picker", "админ")
    assert first["id"] == second["id"]


def test_revoking_keeps_the_history(database: Database) -> None:
    """Отзыв — дата, а не удаление: кто и когда имел право, спросят при разборе."""
    directory = RoleDirectory(database)
    user = str(uuid.uuid4())
    grant = directory.grant(user, "logist", "админ")

    directory.revoke(str(grant["id"]), "админ Пётр")

    assert directory.principal(user)["roles"] == []
    with database.cursor() as cursor:
        cursor.execute("SELECT revoked_by FROM identity_role_grant WHERE id = %s",
                       (grant["id"],))
        assert cursor.fetchone()["revoked_by"] == "админ Пётр"
    # Роль можно выдать заново — частичный уникальный индекс считает только
    # действующие выдачи.
    assert directory.grant(user, "logist", "админ")["id"] != grant["id"]


def test_a_grant_without_an_author_is_refused(database: Database) -> None:
    with pytest.raises(ValueError, match="без автора"):
        RoleDirectory(database).grant(str(uuid.uuid4()), "picker", "  ")


def test_a_scoped_grant_needs_something_to_scope_to(database: Database) -> None:
    import psycopg

    with pytest.raises(psycopg.errors.CheckViolation):
        with database.transaction() as cursor:
            cursor.execute(
                "INSERT INTO identity_role_grant (id, user_id, role_code, scope_kind, granted_by) "
                "VALUES (%s, %s, 'senior_manager', 'partner_branch', 'админ')",
                (str(uuid.uuid4()), str(uuid.uuid4())))


def test_a_stand_token_works_only_on_the_stand(client: TestClient, database: Database,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    """Заглушка, доехавшая до прода, — открытая дверь. Закрыта проверкой среды."""
    user = str(uuid.uuid4())
    RoleDirectory(database).grant(user, "admin", "сид стенда")

    good = client.post("/api/identity/v1/introspect", json={"token": f"stand:{user}"})
    assert good.json()["active"] is True
    assert good.json()["roles"] == ["admin"]

    monkeypatch.setenv("APP_ENV", "production")
    refused = client.post("/api/identity/v1/introspect", json={"token": f"stand:{user}"})
    assert refused.status_code == 401
    assert "только в локальных" in refused.json()["reason"]


def test_a_user_without_roles_is_not_a_principal(client: TestClient) -> None:
    """Токен без ролей — это не «всё видно», а «ничего нельзя»."""
    response = client.post("/api/identity/v1/introspect",
                           json={"token": f"stand:{uuid.uuid4()}"})
    assert response.status_code == 403
    assert "нет ни одной действующей роли" in response.json()["reason"]


def test_an_unsigned_request_gets_nothing(client: TestClient) -> None:
    assert client.post("/api/identity/v1/introspect", json={}).status_code == 401
