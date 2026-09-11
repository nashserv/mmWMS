"""Права по дереву менеджеров.

«Менеджер видит только своих клиентов и свою комиссию, старший — свою ветку
целиком» (файл 04). Проверяется отказом, а не пустой выдачей: пустой список
читается как «у вас ничего нет» и прячет настоящую причину.
"""
from __future__ import annotations

import importlib
import uuid
from typing import Any
from collections.abc import Iterator

import pytest
from starlette.testclient import TestClient

from app.db import Database
from app.principal import Principal, Principals
from app.service import BillingService

from conftest import event


class FakeIdentity(Principals):
    """identity ещё не поднят рядом — работаем против его ответа, не против кода."""

    def __init__(self, people: dict[str, Principal]) -> None:
        super().__init__(base_url="http://identity.stand")
        self.people = people

    def of(self, authorization: str | None) -> Principal:
        from app.principal import Unauthorized

        token = (authorization or "").removeprefix("Bearer ").strip()
        if token not in self.people:
            raise Unauthorized("identity не признал токен")
        return self.people[token]


@pytest.fixture
def people(stand: dict[str, Any]) -> dict[str, Principal]:
    # Ключи латиницей: заголовок Authorization обязан быть ASCII, и кириллица
    # в токене падает не там, где проверяется право.
    return {
        "admin": Principal(user_id="u-admin", roles=frozenset({"admin"})),
        "senior": Principal(user_id="u-senior", roles=frozenset({"senior_manager"}),
                            partner_branches=(stand["senior"],)),
        "manager": Principal(user_id="u-manager", roles=frozenset({"account_manager"}),
                             partner_branches=(stand["manager"],)),
        "client": Principal(user_id="u-client", roles=frozenset({"owner"}),
                            sellers=("seller-1",)),
    }


@pytest.fixture
def client(database: Database, people: dict[str, Principal],
           monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", database.url)
    from app import api as api_module

    importlib.reload(api_module)
    api_module.principals = FakeIdentity(people)
    with TestClient(api_module.app) as handle:
        yield handle


def as_(who: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {who}"}


def test_a_manager_cannot_read_the_commission_of_their_senior(
        client: TestClient, stand: dict[str, Any]) -> None:
    """Прямая ссылка на чужую комиссию обязана получить отказ, а не нули."""
    response = client.get(f"/api/billing/v1/partners/{stand['senior']}/commission",
                          params={"period": "2026-09"}, headers=as_("manager"))
    assert response.status_code == 403
    assert "не в вашей ветке" in response.json()["error"]


def test_a_senior_sees_the_whole_branch_including_the_manager(
        client: TestClient, stand: dict[str, Any]) -> None:
    assert client.get(f"/api/billing/v1/partners/{stand['manager']}/commission",
                      params={"period": "2026-09"}, headers=as_("senior")).status_code == 200
    tree = client.get("/api/billing/v1/partners", headers=as_("senior")).json()["partners"]
    assert {row["name"] for row in tree} == {"Зардал", "Менеджер кабинетов"}


def test_a_manager_sees_only_their_own_node_of_the_tree(
        client: TestClient, stand: dict[str, Any]) -> None:
    tree = client.get("/api/billing/v1/partners", headers=as_("manager")).json()["partners"]
    assert [row["name"] for row in tree] == ["Менеджер кабинетов"]


def test_accruals_of_a_foreign_cabinet_are_not_listed(
        client: TestClient, database: Database, stand: dict[str, Any]) -> None:
    """Чужой кабинет не появляется в выдаче даже с его cabinet_id в параметрах."""
    other = str(uuid.uuid4())
    with database.transaction() as cursor:
        cursor.execute("INSERT INTO cabinet (id, seller_external_id, name) "
                       "VALUES (%s, 'seller-чужой', 'Чужой ИП')", (other,))
    service = BillingService(database)
    service.ingest(event("wms.packing.completed.v1", {"seller_id": "seller-1"}))
    service.ingest(event("wms.packing.completed.v1", {"seller_id": "seller-чужой"}))

    mine = client.get("/api/billing/v1/accruals", headers=as_("manager")).json()
    assert [row["cabinet_seller"] for row in mine["accruals"]] == ["seller-1"]

    asked_for_foreign = client.get("/api/billing/v1/accruals",
                                   params={"cabinet_id": other}, headers=as_("manager")).json()
    assert asked_for_foreign["count"] == 0

    everything = client.get("/api/billing/v1/accruals", headers=as_("admin")).json()
    assert everything["count"] == 2


def test_a_client_sees_their_own_cabinet_and_nothing_else(
        client: TestClient, database: Database, stand: dict[str, Any]) -> None:
    """ЛК клиента: своя расшифровка, включая наценку партнёра, и только своя."""
    with database.transaction() as cursor:
        cursor.execute("INSERT INTO cabinet (id, seller_external_id, name) "
                       "VALUES (gen_random_uuid(), 'seller-чужой', 'Чужой ИП')")
    service = BillingService(database)
    service.ingest(event("wms.packing.completed.v1", {"seller_id": "seller-1"}))
    service.ingest(event("wms.packing.completed.v1", {"seller_id": "seller-чужой"}))

    body = client.get("/api/billing/v1/accruals", headers=as_("client")).json()
    assert [row["cabinet_seller"] for row in body["accruals"]] == ["seller-1"]
    assert body["accruals"][0]["partner_amount"] == "15.00", (
        "клиент должен видеть, что 15 из 45 — наценка партнёра")


def test_margin_is_not_shown_to_partners_at_all(
        client: TestClient, stand: dict[str, Any]) -> None:
    """Себестоимость — внутреннее число. Партнёру видна комиссия, не наша маржа."""
    assert client.get("/api/billing/v1/reports/margin", params={"period": "2026-09"},
                      headers=as_("senior")).status_code == 403
    assert client.get("/api/billing/v1/reports/margin", params={"period": "2026-09"},
                      headers=as_("admin")).status_code == 200


def test_a_partner_cannot_change_their_own_markup(
        client: TestClient, stand: dict[str, Any]) -> None:
    """Наценку ставит MM-Express, а не тот, кто её получает."""
    response = client.post(f"/api/billing/v1/partners/{stand['senior']}/markups",
                           json={"service": "packing", "markup": "50"}, headers=as_("senior"))
    assert response.status_code == 403


def test_without_a_token_nothing_is_shown(client: TestClient) -> None:
    response = client.get("/api/billing/v1/accruals")
    assert response.status_code == 401


def test_an_unknown_identity_url_fails_closed_outside_the_stand(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Стенд без identity открыт. Прод без identity — закрыт, а не открыт молча."""
    from app.principal import Unauthorized

    monkeypatch.setenv("APP_ENV", "production")
    with pytest.raises(Unauthorized, match="IDENTITY_URL"):
        Principals(base_url="").of(None)

    monkeypatch.setenv("APP_ENV", "test")
    assert Principals(base_url="").of(None).open_stand is True


# ------------------------------------------- маршруты, которые были открыты

def test_the_tariff_list_needs_a_token(client: TestClient) -> None:
    """Прайс — не секрет, но и не улица: по нему видно, сколько платят
    клиенты и какая у склада маржа."""
    assert client.get("/api/billing/v1/tariffs").status_code == 401
    assert client.get("/api/billing/v1/tariffs", headers=as_("client")).status_code == 200


def test_ingesting_an_event_needs_the_right_to_write_money(client: TestClient) -> None:
    """`/events` превращает событие в начисление клиенту.

    Маршрут был открыт: кто угодно мог выставить клиенту любую сумму или,
    наоборот, не выставить — повторив событие с чужим `event_id`.
    """
    payload = {"event_id": "11111111-1111-4111-8111-111111111111",
               "type": "wms.packing.completed.v1", "occurred_at": "2026-09-11T10:00:00Z",
               "payload": {"seller_id": "seller-1"}}
    assert client.post("/api/billing/v1/events", json=payload).status_code == 401
    assert client.post("/api/billing/v1/events", json=payload,
                       headers=as_("client")).status_code == 403


def test_the_consumer_may_ingest_with_a_service_token(
        client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Консьюмер шины не человек, и роли человека ему не нужны."""
    monkeypatch.setenv("SERVICE_TOKEN", "service-token-for-tests")
    payload = {"event_id": "22222222-2222-4222-8222-222222222222",
               "type": "wms.packing.completed.v1", "occurred_at": "2026-09-11T10:00:00Z",
               "payload": {"seller_id": "seller-1"}}
    response = client.post("/api/billing/v1/events", json=payload,
                           headers={"Authorization": "Bearer service-token-for-tests"})
    assert response.status_code == 200, response.text


def test_shift_output_is_for_the_warehouse_head(client: TestClient) -> None:
    """Кто сколько сделал — про людей, а не про деньги клиента."""
    assert client.get("/api/billing/v1/reports/shift").status_code == 401
    assert client.get("/api/billing/v1/reports/shift",
                      headers=as_("client")).status_code == 403
    assert client.get("/api/billing/v1/reports/shift",
                      headers=as_("admin")).status_code == 200


def test_an_invoice_of_another_client_is_not_visible(
        client: TestClient, stand: dict[str, Any]) -> None:
    """В акте видно, сколько платит другой клиент и какая у него наценка.

    Отвечаем 404, а не 403: существование чужого счёта — тоже сведение.
    """
    assert client.get("/api/billing/v1/invoices/33333333-3333-4333-8333-333333333333"
                      ).status_code == 401
    response = client.get(
        "/api/billing/v1/invoices/33333333-3333-4333-8333-333333333333",
        headers=as_("client"))
    assert response.status_code == 404


def test_a_blocking_handler_is_not_declared_async() -> None:
    """Обработчик с синхронным psycopg/httpx обязан быть обычным `def`.

    FastAPI запускает `def` в пуле потоков, а `async def` — прямо в цикле
    событий: блокирующий запрос к базе внутри `async def` останавливает ВЕСЬ
    процесс на время запроса. Пять экранов, опрашивающих раз в секунду,
    превращают это в очередь из ждущих запросов.
    """
    import ast
    import pathlib

    for name in ("billing", "portal", "internal-admin", "identity"):
        path = (pathlib.Path(__file__).resolve().parents[3]
                / "services" / name / "app" / "api.py")
        if not path.is_file():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        offenders: list[str] = []
        for node in tree.body:
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            decorated = any(
                isinstance(item, ast.Call)
                and isinstance(item.func, ast.Attribute)
                and item.func.attr in ("get", "post", "put", "patch", "delete")
                for item in node.decorator_list)
            if not decorated:
                continue
            awaits = any(isinstance(inner, ast.Await | ast.AsyncWith | ast.AsyncFor)
                         for inner in ast.walk(node))
            if not awaits:
                offenders.append(node.name)
        assert not offenders, (
            f"{name}: обработчики {offenders} объявлены async, но ничего не ждут — "
            f"их синхронные запросы к базе остановят весь процесс")


# --- сервисный токен: читает всё, не пишет ничего ---------------------------
#
# Полный прогон проверяет «на каждую операцию есть начисление» и до 12.09.2026
# читал `billing_accrual` напрямую по BILLING_DATABASE_URL — то есть проверял
# стык, минуя стык. Переименование колонки ломало прогон там, где контракт не
# менялся; сломанный маршрут прогон не видел вовсе.

SERVICE_SECRET = "development-only-service-token"


@pytest.fixture
def service_client(database: Database, people: dict[str, Principal],
                   monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", database.url)
    monkeypatch.setenv("SERVICE_TOKEN", SERVICE_SECRET)
    from app import api as api_module

    importlib.reload(api_module)
    api_module.principals = FakeIdentity(people)
    with TestClient(api_module.app) as handle:
        yield handle


def _an_accrual(database: Database, stand: dict[str, Any]) -> str:
    """Начисление, которое потом ищут по событию."""
    event_id = str(uuid.uuid4())
    with database.transaction() as cursor:
        cursor.execute(
            "INSERT INTO billing_accrual "
            "  (id, event_id, event_type, tenant_id, cabinet_id, seller_external_id, "
            "   service, quantity, unit_price, markup, amount, partner_amount, occurred_on) "
            "VALUES (%s, %s, 'wms.packing.completed.v1', 'test', %s, 'seller-1', "
            "        'packing', 1, 30.00, 15.00, 45.00, 0, CURRENT_DATE)",
            (str(uuid.uuid4()), event_id, stand["cabinet"]))
    return event_id


def _service() -> dict[str, str]:
    return {"Authorization": f"Bearer {SERVICE_SECRET}"}


def test_a_service_token_finds_the_accrual_of_one_operation(
        service_client: TestClient, database: Database, stand: dict[str, Any]) -> None:
    """Точечный вопрос «есть ли начисление на эту операцию».

    Ровно он нужен шагу 12 прогона, и ровно его не было — поэтому прогон и
    ходил в базу.
    """
    event_id = _an_accrual(database, stand)

    answer = service_client.get("/api/billing/v1/accruals",
                                params={"event_id": event_id}, headers=_service())

    assert answer.status_code == 200, (
        f"сервисный токен не пустили к начислениям: {answer.text[:200]}")
    found = answer.json()["accruals"]
    assert len(found) == 1, f"поиск по event_id вернул {len(found)} строк вместо одной"
    assert str(found[0]["event_id"]) == event_id


def test_a_service_token_sees_cabinets_that_belong_to_nobody_in_particular(
        service_client: TestClient, database: Database, stand: dict[str, Any]) -> None:
    """Сервис не человек: он спрашивает про чужие кабинеты по долгу службы.

    Принципал с пустой видимостью отдал бы пустой список — и прогон решил бы,
    что начисления нет, вместо того чтобы сказать «меня не пустили».
    """
    _an_accrual(database, stand)

    answer = service_client.get("/api/billing/v1/accruals", headers=_service())

    assert answer.status_code == 200
    assert answer.json()["accruals"], (
        "сервису видно пусто — он отличит это от «начислений нет» только чудом")


def test_a_service_token_cannot_write_money(
        service_client: TestClient, stand: dict[str, Any]) -> None:
    """Читает — да, пишет — нет.

    Общий секрет лежит в переменных окружения половины стенда. Дать ему право
    закрывать периоды значит сделать эту переменную ключом от денег.
    """
    answer = service_client.post("/api/billing/v1/periods/2026-09/close",
                                 headers=_service())

    assert answer.status_code in (401, 403), (
        f"сервисный токен закрыл период — он получил право писать деньги "
        f"(ответ {answer.status_code})")
