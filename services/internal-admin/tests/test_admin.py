"""Админка: тонкий слой над биллингом плюс журнал того, кто что сделал."""
from __future__ import annotations

import importlib
import json
from typing import Any
from collections.abc import Iterator

import pytest
from starlette.testclient import TestClient

from app.audit import trim
from app.db import Database
from app.upstream import Upstream


class FakeBilling:
    """Биллинг за админкой. Работаем против его ответа, не против его кода."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.answers: dict[str, tuple[int, Any]] = {}
        self.down = False

    def _answer(self, path: str) -> tuple[int, Any]:
        return self.answers.get(path, (200, {}))

    def get(self, path: str, authorization: str | None,
            params: dict[str, Any] | None = None) -> tuple[int, Any]:
        if self.down:
            raise Upstream("биллинг недоступен: connection refused")
        self.calls.append(("GET", path, params))
        return self._answer(path)

    def post(self, path: str, authorization: str | None,
             json_body: dict[str, Any] | None = None) -> tuple[int, Any]:
        if self.down:
            raise Upstream("биллинг недоступен: connection refused")
        self.calls.append(("POST", path, json_body))
        return self._answer(path)


class FakeIdentity:
    def __init__(self, principal: dict[str, Any] | None = None) -> None:
        self.principal = principal or {"active": True, "user_id": "u-admin", "roles": ["admin"]}

    def whoami(self, authorization: str | None) -> dict[str, Any]:
        return self.principal

    def roles(self) -> list[dict[str, Any]]:
        return [{"code": "admin", "name": "Администратор"}]

    def grant(self, authorization: str | None, body: dict[str, Any]) -> tuple[int, Any]:
        return 201, {"grant": body}


@pytest.fixture
def parts(database: Database, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", database.url)
    from app import api as api_module

    importlib.reload(api_module)
    api_module.billing = FakeBilling()
    api_module.identity = FakeIdentity()
    with database.transaction() as cursor:
        cursor.execute("TRUNCATE admin_audit RESTART IDENTITY")
    with TestClient(api_module.app) as client:
        yield {"client": client, "billing": api_module.billing, "module": api_module}


def test_onboarding_goes_to_billing_and_lands_in_the_journal(parts: dict[str, Any]) -> None:
    """«Ни одного ручного SQL» имеет смысл только вместе с «записано, кто это сделал»."""
    parts["billing"].answers["/api/billing/v1/onboarding"] = (201, {"state": "ok", "steps": []})

    response = parts["client"].post("/api/admin/v1/onboarding", json={
        "seller_external_id": "stand-seller-099", "name": "ИП Тест",
        "contract_reference": "ДГ-1", "opening_stock": [{"barcode": "1"}, {"barcode": "2"}]})

    assert response.status_code == 201
    assert parts["billing"].calls[0][1] == "/api/billing/v1/onboarding"

    journal = parts["client"].get("/api/admin/v1/audit").json()
    assert len(journal) == 1
    assert journal[0]["action"] == "клиент заведён"
    assert journal[0]["subject"] == "stand-seller-099"
    assert journal[0]["actor_roles"] == ["admin"]
    # Начальный остаток в журнале не нужен целиком — он раздувает строку.
    assert journal[0]["request"]["opening_stock"] == "2 строк"


def test_a_refusal_is_written_down_too(parts: dict[str, Any]) -> None:
    """Неудачная попытка нужна в журнале не меньше удачной: по ней и разбирают."""
    parts["billing"].answers["/api/billing/v1/onboarding"] = (
        400, {"error": "в secret_ref передан живой токен WB"})

    response = parts["client"].post("/api/admin/v1/onboarding",
                                    json={"seller_external_id": "stand-seller-098"})

    assert response.status_code == 400
    journal = parts["client"].get("/api/admin/v1/audit").json()
    assert journal[0]["status"] == 400
    assert "живой токен" in journal[0]["response"]["error"]


def test_approving_a_tariff_signs_it_with_the_person_who_pressed(
        parts: dict[str, Any]) -> None:
    """Цена без подписи в счёт не идёт — значит, подпись берётся из входа, не из тела."""
    parts["billing"].answers["/api/billing/v1/tariff-versions/v-1/approve"] = (200, {})

    parts["client"].post("/api/admin/v1/tariff-versions/v-1/approve", json={})

    method, path, body = parts["billing"].calls[0]
    assert path == "/api/billing/v1/tariff-versions/v-1/approve"
    assert body["approved_by"] == "u-admin"


def test_a_restricted_report_does_not_break_the_first_screen(parts: dict[str, Any]) -> None:
    """Партнёру маржа не видна. Экран обязан это сказать, а не показать пустоту."""
    parts["billing"].answers["/api/billing/v1/reports/margin"] = (403, {"error": "нельзя"})
    parts["billing"].answers["/api/billing/v1/reports/unbilled"] = (403, {"error": "нельзя"})
    parts["billing"].answers["/api/billing/v1/cabinets"] = (200, {"cabinets": []})

    body = parts["client"].get("/api/admin/v1/overview").json()

    assert body["restricted"] == {"unbilled": True, "margin": True}
    assert body["margin"] == []


def test_the_overview_puts_the_worst_cabinet_first(parts: dict[str, Any]) -> None:
    """Отчёт нужен, чтобы увидеть, кто съедает смену, а не чтобы листать алфавит."""
    parts["billing"].answers["/api/billing/v1/cabinets"] = (200, {"cabinets": []})
    parts["billing"].answers["/api/billing/v1/reports/unbilled"] = (200, {"reasons": []})
    parts["billing"].answers["/api/billing/v1/reports/margin"] = (200, {"cabinets": [
        {"seller_external_id": "a", "margin": "100.00"},
        {"seller_external_id": "b", "margin": "-40.00"},
        {"seller_external_id": "c", "margin": "5.00"}]})

    body = parts["client"].get("/api/admin/v1/overview").json()

    assert [row["seller_external_id"] for row in body["margin"]] == ["b", "c", "a"]


def test_when_billing_is_down_the_admin_says_who_is_broken(parts: dict[str, Any]) -> None:
    """502, а не 500: сломалась не админка, и чинить надо не её."""
    parts["billing"].down = True
    response = parts["client"].get("/api/admin/v1/partners")
    assert response.status_code == 502
    assert "биллинг недоступен" in response.json()["error"]


def test_the_screen_is_served(parts: dict[str, Any]) -> None:
    page = parts["client"].get("/")
    assert page.status_code == 200
    assert "админка" in page.text


def test_the_journal_trims_bulk_payloads_but_keeps_everything_else() -> None:
    request = {"seller_external_id": "s-1", "opening_stock": [{"barcode": "1"}],
               "secret_ref": "vault://mmx/wb/s-1"}
    trimmed = trim(request)
    assert trimmed["opening_stock"] == "1 строк"
    # secret_ref — ссылка, а не секрет: её как раз и нужно видеть в журнале.
    assert trimmed["secret_ref"] == "vault://mmx/wb/s-1"


# ------------------------------- маршруты, которые были открыты настежь

def _with_identity(database: Database, monkeypatch: pytest.MonkeyPatch,
                   principal: dict[str, Any]) -> TestClient:
    """Админка с заданным ответом identity."""
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", database.url)
    from app import api as api_module

    importlib.reload(api_module)
    api_module.billing = FakeBilling()
    api_module.identity = FakeIdentity(principal)
    return TestClient(api_module.app)


def test_admin_routes_are_closed_without_a_token(
        database: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    """Раньше открыты были ВСЕ, включая /audit и /roles: по ним видно, кто
    чем управляет и какими действиями."""
    client = _with_identity(database, monkeypatch,
                            {"active": False, "reason": "токен не предъявлен"})
    for path in ("/api/admin/v1/overview", "/api/admin/v1/partners",
                 "/api/admin/v1/audit", "/api/admin/v1/roles",
                 "/api/admin/v1/whoami"):
        assert client.get(path).status_code == 401, f"{path} отвечает без токена"


def test_a_picker_has_no_business_in_the_admin_panel(
        database: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    """Админка меняет деньги и права. Сборочная роль туда не ходит."""
    client = _with_identity(database, monkeypatch,
                            {"active": True, "user_id": "u-picker", "roles": ["picker"]})
    assert client.get("/api/admin/v1/overview").status_code == 403


def test_an_accountant_is_let_in(database: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _with_identity(database, monkeypatch,
                            {"active": True, "user_id": "u-acc", "roles": ["accountant"]})
    assert client.get("/api/admin/v1/partners").status_code == 200


def test_the_author_of_an_approval_comes_from_the_token(parts: dict[str, Any]) -> None:
    """`approved_by` из тела перезаписывается: подпись за того, кого назовут,
    при разборе инцидента ничего не стоит."""
    parts["client"].post("/api/admin/v1/tariff-versions/v-1/approve",
                         json={"approved_by": "кто-то-другой"})
    path, _token, body = parts["billing"].calls[-1]
    assert body["approved_by"] == "u-admin", (path, body)


def test_a_token_shaped_value_never_reaches_the_journal(parts: dict[str, Any]) -> None:
    """Журнал админки читают при разборе инцидентов, и живой токен живёт в нём
    дольше, чем сам инцидент (инвариант 15)."""
    import base64

    shaped = ".".join(
        base64.urlsafe_b64encode(part).decode().rstrip("=")
        for part in (b'{"alg":"HS256"}', b'{"sub":"x"}', b"sig"))
    parts["client"].post("/api/admin/v1/onboarding",
                         json={"seller_external_id": "s-1", "secret_ref": shaped})
    rows = parts["module"].audit.recent(10)
    assert rows, "действие не попало в журнал"
    written = json.dumps(rows[0]["request"], ensure_ascii=False)
    assert shaped not in written, "строка формы JWT сохранена в журнале целиком"
