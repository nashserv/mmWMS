"""Аутентификация identity. Раздел 12: identity — единственный источник
пользователей и ролей, и он обязан спрашивать, кто к нему пришёл.

До этих тестов `/grants`, `/grants/{id}/revoke` и `/users/{id}/roles` были
открыты: выдать себе роль `admin` мог кто угодно, у кого есть сетевой доступ.
"""
from __future__ import annotations

import importlib
import uuid
from collections.abc import Iterator

import pytest
from starlette.testclient import TestClient

from app import tokens as jwt
from app.db import Database


@pytest.fixture
def client(database: Database, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", database.url)
    monkeypatch.delenv("IDENTITY_UPSTREAM_URL", raising=False)
    monkeypatch.delenv("IDENTITY_SIGNING_KEY", raising=False)
    from app import api as api_module

    importlib.reload(api_module)
    with TestClient(api_module.app) as handle:
        yield handle


def _grant(database: Database, role: str) -> str:
    """Завести пользователя с одной глобальной ролью."""
    user_id = str(uuid.uuid4())
    with database.transaction() as cursor:
        cursor.execute(
            "INSERT INTO identity_role_grant (id, user_id, role_code, scope_kind, granted_by) "
            "VALUES (%s, %s, %s, 'global', %s)",
            (uuid.uuid4(), user_id, role, str(uuid.uuid4())))
    return user_id


@pytest.fixture
def admin_user(database: Database) -> str:
    return _grant(database, "admin")


@pytest.fixture
def picker_user(database: Database) -> str:
    return _grant(database, "picker")


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def issue_for(client, user_id: str) -> str:
    """Токен через тот же маршрут, которым им пользуется стенд."""
    response = client.post("/api/identity/v1/tokens", json={"user_id": user_id})
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


# ------------------------------------------------------------- сам токен


def test_a_forged_payload_does_not_pass(client, admin_user) -> None:
    """Подпись проверяется раньше, чем читается полезная нагрузка."""
    token = issue_for(client, admin_user)
    header, payload, signature = token.split(".")
    import base64
    import json

    tampered = json.loads(base64.urlsafe_b64decode(payload + "=="))
    tampered["sub"] = str(uuid.uuid4())
    forged = f"{header}.{jwt.b64url(json.dumps(tampered).encode())}.{signature}"

    response = client.post("/api/identity/v1/introspect", json={"token": forged})
    assert response.status_code == 401
    assert response.json()["active"] is False


def test_alg_none_is_not_a_supported_algorithm(client, admin_user) -> None:
    """`alg: none` не отклоняется проверкой — его нет в коде вовсе."""
    token = issue_for(client, admin_user)
    _, payload, _ = token.split(".")
    import json

    header = jwt.b64url(json.dumps({"alg": "none", "typ": "JWT", "kid": "x"}).encode())
    response = client.post("/api/identity/v1/introspect",
                           json={"token": f"{header}.{payload}."})
    assert response.status_code == 401


def test_jwks_never_shows_the_private_half(client) -> None:
    document = client.get("/.well-known/jwks.json").json()
    assert document["keys"], "JWKS пуст — проверять подпись будет нечем"
    for key in document["keys"]:
        assert key["kty"] == "OKP" and key["crv"] == "Ed25519"
        assert set(key) <= {"kty", "crv", "use", "alg", "kid", "x"}, (
            f"в JWKS лишние поля: {set(key)} — приватной части там быть не может")


# --------------------------------------------------- кто и что может делать


def test_grants_without_a_token_are_refused(client) -> None:
    """Без токена 401. Раньше выдать себе admin мог кто угодно."""
    response = client.post("/api/identity/v1/grants",
                           json={"user_id": str(uuid.uuid4()), "role_code": "picker"})
    assert response.status_code == 401


def test_a_picker_cannot_hand_out_roles(client, picker_user) -> None:
    """С ролью picker на админском маршруте 403."""
    token = issue_for(client, picker_user)
    response = client.post("/api/identity/v1/grants", headers=bearer(token),
                           json={"user_id": str(uuid.uuid4()), "role_code": "admin"})
    assert response.status_code == 403


def test_an_admin_can_hand_out_roles(client, admin_user) -> None:
    token = issue_for(client, admin_user)
    response = client.post("/api/identity/v1/grants", headers=bearer(token),
                           json={"user_id": str(uuid.uuid4()), "role_code": "picker"})
    assert response.status_code == 201


def test_the_author_of_a_grant_comes_from_the_token(client, admin_user) -> None:
    """`granted_by` из тела игнорируется: подпись за того, кого назовут, при
    разборе инцидента ничего не стоит."""
    token = issue_for(client, admin_user)
    response = client.post("/api/identity/v1/grants", headers=bearer(token),
                           json={"user_id": str(uuid.uuid4()), "role_code": "picker",
                                 "granted_by": "кто-то-другой"})
    assert response.status_code == 201
    assert response.json()["grant"]["granted_by"] == admin_user


def test_a_broken_uuid_is_four_hundred_not_five_hundred(client, admin_user) -> None:
    token = issue_for(client, admin_user)
    response = client.post("/api/identity/v1/grants", headers=bearer(token),
                           json={"user_id": "не-uuid", "role_code": "picker"})
    assert response.status_code == 400


def test_own_roles_are_visible_but_someone_elses_are_not(client, picker_user, admin_user) -> None:
    token = issue_for(client, picker_user)
    assert client.get(f"/api/identity/v1/users/{picker_user}/roles",
                      headers=bearer(token)).status_code == 200
    assert client.get(f"/api/identity/v1/users/{admin_user}/roles",
                      headers=bearer(token)).status_code == 403


def test_a_token_without_roles_is_not_active(client, database) -> None:
    """Подлинный токен человека без ролей — это `active: false`.

    Роль могли отозвать минуту назад, а токен живёт до конца смены. Токен
    отвечает «кто это», справочник — «что ему сейчас можно».
    """
    stranger = str(uuid.uuid4())
    response = client.post("/api/identity/v1/tokens", json={"user_id": stranger})
    assert response.status_code == 403, "токен без единой роли выдавать незачем"
