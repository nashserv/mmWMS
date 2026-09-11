"""Симулятор Wildberries — та часть стенда, которой верят все остальные.

Он не «заглушка для тестов»: на нём проверяются лимит кабинета, форма ответа,
статусы заказов и пагинация. Симулятор, который отвечает не так, как WB,
делает зелёными тесты, которые в бою красные, — и найти это можно будет
только на кабинете клиента.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api import app, simulator


@pytest.fixture(autouse=True)
def _clean() -> None:
    simulator.reset()


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


def seed(client: TestClient, account: str, count: int = 1) -> list[int]:
    answer = client.post("/__stand__/seed-orders", json={
        "account": account, "count": count, "barcode": "4600000000011",
        "deadline": "2026-09-12T10:00:00Z"})
    assert answer.status_code == 200, answer.text
    return [int(order["id"]) for order in answer.json()["orders"]]


def test_a_new_order_looks_the_way_wildberries_sends_it(client: TestClient) -> None:
    """Штрихкод лежит в `skus`, а не рядом с артикулом (раздел 3.2).

    Главный источник путаницы в маппинге: у WB поле называется `sku`, а лежит
    в нём именно штрихкод.
    """
    seed(client, "acc-1")
    answer = client.get("/api/v3/orders", headers={"X-Stand-Account": "acc-1"})

    assert answer.status_code == 200
    order = answer.json()["orders"][0]
    assert order["skus"] == ["4600000000011"]
    assert order["supplierStatus"] == "new"
    assert "ddate" in order, "без срока нельзя проверить порядок по дедлайну"


def test_orders_of_one_cabinet_are_not_visible_to_another(client: TestClient) -> None:
    """Кабинеты изолированы: чужой заказ не приезжает никогда."""
    seed(client, "acc-1", 2)
    seed(client, "acc-2", 3)

    mine = client.get("/api/v3/orders", headers={"X-Stand-Account": "acc-1"}).json()
    assert len(mine["orders"]) == 2


def test_a_supply_moves_orders_through_confirm_to_complete(client: TestClient) -> None:
    """Путь статуса: new → confirm (в поставке) → complete (передана).

    На этих переходах держится сверка: `confirm` до передачи — законное
    состояние задания, которое ещё лежит на складе.
    """
    orders = seed(client, "acc-1", 2)
    supply = client.post("/api/v3/supplies", json={"name": "x"},
                         headers={"X-Stand-Account": "acc-1"}).json()["id"]

    client.patch(f"/api/marketplace/v3/supplies/{supply}/orders",
                 json={"orders": orders}, headers={"X-Stand-Account": "acc-1"})
    after_add = client.get("/api/v3/orders", headers={"X-Stand-Account": "acc-1"}).json()
    assert {order["supplierStatus"] for order in after_add["orders"]} == {"confirm"}

    client.patch(f"/api/v3/supplies/{supply}/deliver",
                 headers={"X-Stand-Account": "acc-1"})
    after_deliver = client.get("/api/v3/orders", headers={"X-Stand-Account": "acc-1"}).json()
    assert {order["supplierStatus"] for order in after_deliver["orders"]} == {"complete"}


def test_statuses_are_answered_by_name(client: TestClient) -> None:
    """`POST /api/v3/orders/status` — адресный вопрос, адресный ответ.

    О заказе, которого у кабинета нет, симулятор МОЛЧИТ — как и WB. Иначе
    сверка никогда не увидит «задание пропало из кабинета».
    """
    orders = seed(client, "acc-1", 2)
    answer = client.post("/api/v3/orders/status",
                         json={"orders": orders + [999_999_999]},
                         headers={"X-Stand-Account": "acc-1"})

    assert answer.status_code == 200
    returned = {int(row["id"]) for row in answer.json()["orders"]}
    assert returned == set(orders), "симулятор ответил про чужой или несуществующий заказ"


def test_a_cancelled_order_says_so(client: TestClient) -> None:
    """Отмену у WB устраивает покупатель; ручки «отмени» в API нет."""
    orders = seed(client, "acc-1", 1)
    client.post("/__stand__/cancel-orders",
                json={"account": "acc-1", "orders": orders})

    answer = client.get("/api/v3/orders", headers={"X-Stand-Account": "acc-1"}).json()
    assert answer["orders"][0]["supplierStatus"] == "cancel"


def test_the_cabinet_limit_is_per_cabinet_and_real(client: TestClient) -> None:
    """300 запросов в минуту НА КАБИНЕТ — ради этого симулятор и написан.

    Без настоящего лимита на стенде не воспроизвести блокировку, ради которой
    в разделе 6.4 держат ограничитель.
    """
    import app.api as module

    previous = module.RATE_LIMIT
    module.RATE_LIMIT = 3
    try:
        simulator.reset()
        seed(client, "acc-1")
        codes = [client.get("/api/v3/orders",
                            headers={"X-Stand-Account": "acc-1"}).status_code
                 for _ in range(5)]
        assert 429 in codes, f"лимит не сработал: {codes}"

        # Соседний кабинет своим лимитом не задет.
        other = client.get("/api/v3/orders", headers={"X-Stand-Account": "acc-2"})
        assert other.status_code == 200, "лимит посчитан глобально, а он на кабинет"
    finally:
        module.RATE_LIMIT = previous


def test_reset_forgets_everything_but_does_not_reuse_numbers(client: TestClient) -> None:
    """Сброс не возвращает прежние номера заказов и поставок.

    Счётчик с фиксированного числа выдавал бы те же номера, что уже лежат в
    Postgres от прошлых прогонов: опросчик отсеивал бы свежие заказы как
    известные, и шаг 4 полного прогона краснел бы причиной, никак с ним не
    связанной.
    """
    first = seed(client, "acc-1", 1)[0]
    supply_one = client.post("/api/v3/supplies", json={"name": "x"},
                             headers={"X-Stand-Account": "acc-1"}).json()["id"]

    assert client.post("/__stand__/reset").status_code == 200
    assert client.get("/api/v3/orders",
                      headers={"X-Stand-Account": "acc-1"}).json()["orders"] == []

    second = seed(client, "acc-1", 1)[0]
    supply_two = client.post("/api/v3/supplies", json={"name": "x"},
                             headers={"X-Stand-Account": "acc-1"}).json()["id"]

    assert second >= first, f"номер заказа откатился: {second} после {first}"
    assert supply_two != supply_one, "номер поставки повторился после сброса"


def test_a_sticker_comes_in_the_asked_format(client: TestClient) -> None:
    """ZPL нативно, картинка — откат (раздел 6.6, вопрос 2 раздела 13)."""
    orders = seed(client, "acc-1", 1)
    answer = client.post("/api/v3/orders/stickers?type=zplv",
                         json={"orders": orders},
                         headers={"X-Stand-Account": "acc-1"})

    assert answer.status_code == 200
    sticker = answer.json()["stickers"][0]
    assert sticker["file"].startswith("^XA"), "в zplv-стикере не ZPL"

    bad = client.post("/api/v3/orders/stickers?type=лазерный",
                      json={"orders": orders}, headers={"X-Stand-Account": "acc-1"})
    assert bad.status_code == 400, "неизвестный формат принят молча"


def test_more_than_a_hundred_stickers_at_once_is_refused(client: TestClient) -> None:
    """До 100 за вызов (приложение D). Предел настоящий, не наш."""
    answer = client.post("/api/v3/orders/stickers",
                         json={"orders": list(range(101))},
                         headers={"X-Stand-Account": "acc-1"})
    assert answer.status_code == 400
