"""Низкие находки этапа 2. Мелкие по коду, не мелкие по последствиям.

Общее у них одно: каждая делает ответ системы неправдой. Приёмка висит
незакрытой — счёт клиенту не выставлен. Коробка с чужим штрихкодом — товар в
чужой коробке. Текст исключения наружу — кусок SQL у клиента в ответе.
"""
from __future__ import annotations

import uuid

import pytest

from app.postgres import ConnectionPool, single
from app.receiving import ReceivingOperations
from app.service import CatalogOperations, StockOperations, WmsService

from dbfixtures import require_database, unique


@pytest.fixture(scope="module")
def pool() -> ConnectionPool:
    connections = ConnectionPool(require_database(), max_size=8)
    yield connections
    connections.close()


def make_client(pool: ConnectionPool, name: str) -> dict:
    catalog = CatalogOperations(pool)
    seller, account = unique("seller"), unique("wb")
    barcode = f"46{uuid.uuid4().int % 10**11:011d}"
    catalog.upsert_owner({"seller_external_id": seller, "name": name})
    catalog.upsert_wb_account({
        "op": "upsert", "external_id": account, "seller_external_id": seller,
        "display_name": f"Кабинет {name}", "secret_ref": f"vault://mmx/test/{account}",
        "mode": "live", "status": "ACTIVE", "wb_warehouse_id": 1})
    catalog.ensure_product({"seller_external_id": seller, "barcode": barcode})
    return {"seller": seller, "account": account, "barcode": barcode,
            "cell": unique("cell").upper()}


@pytest.fixture()
def client(pool: ConnectionPool) -> dict:
    return make_client(pool, "Мелочи")


def rows(pool: ConnectionPool, sql: str, params: tuple) -> list[dict]:
    with pool.connection() as connection:
        with single(connection) as cursor:
            cursor.execute(sql, params)
            return cursor.fetchall()


# ------------------------------------------------------------------ приёмка

def test_a_receipt_left_counting_can_be_finished(
        pool: ConnectionPool, client: dict) -> None:
    """Недосчитанная приёмка досчитывается повтором, а не висит навсегда.

    Повтор возвращал её как есть — досчитать было нечем. Приёмка оставалась в
    `counting`, событие `wms.receipt.completed.v1` не уходило, и счёт клиенту
    не выставлялся вовсе.
    """
    receiving = ReceivingOperations(pool, WmsService(pool))
    reference = unique("ТН")
    first = receiving.receive({
        "seller_external_id": client["seller"], "reference": reference,
        "lines": [
            {"barcode": client["barcode"], "expected_qty": 5, "actual_qty": 5,
             "cell_address": client["cell"], "comment": "пересчитано"},
            # Вторую строку объявили, но не пересчитали: приёмка не закрыта.
            {"barcode": client["barcode"], "expected_qty": 3,
             "cell_address": client["cell"] + "-B", "comment": "ещё не считали"},
        ]})
    assert first["state"] == "counting", "сцена не собрана: приёмка уже закрыта"

    second = receiving.receive({
        "seller_external_id": client["seller"], "reference": reference,
        "lines": [{"barcode": client["barcode"], "expected_qty": 3, "actual_qty": 3,
                   "cell_address": client["cell"] + "-B", "comment": "досчитали"}]})

    assert second["state"] == "accepted", (
        f"состояние {second['state']}: приёмку нечем досчитать, она висит "
        f"незакрытой, и счёт клиенту не выставится")

    total = rows(pool, "SELECT COALESCE(SUM(b.qty), 0) AS qty FROM stock_balance b "
                       "  JOIN owner o ON o.id = b.owner_id JOIN sku s ON s.id = b.sku_id "
                       " WHERE o.seller_external_id = %s AND s.barcode = %s "
                       "   AND b.state = 'good'", (client["seller"], client["barcode"]))
    assert int(total[0]["qty"]) == 8, (
        f"на складе {total[0]['qty']} вместо 8: досчёт либо не принял товар, "
        f"либо принял уже принятое второй раз")

    lines = rows(pool, "SELECT count(*) AS n FROM receipt_line l "
                       "  JOIN receipt r ON r.id = l.receipt_id "
                       " WHERE r.reference = %s", (reference,))
    assert int(lines[0]["n"]) == 2, "досчёт завёл вторую строку про тот же товар"

    events = rows(pool, "SELECT count(*) AS n FROM outbox "
                        " WHERE type = 'wms.receipt.completed.v1' "
                        "   AND payload->>'doc_ref' = %s", (reference,))
    assert int(events[0]["n"]) == 1, (
        f"событий о завершённой приёмке {events[0]['n']}: счёт выставится "
        f"столько же раз")


# ------------------------------------------------------------------ коробки

def test_a_box_barcode_belongs_to_one_client(pool: ConnectionPool) -> None:
    """Штрихкод коробки уникален на складе, а не у клиента.

    `ON CONFLICT (barcode) DO UPDATE` без проверки владельца перекладывал в
    чужую коробку наш товар: изоляция владельца кончалась на штрихкоде.
    """
    first = make_client(pool, "Коробка А")
    second = make_client(pool, "Коробка Б")
    box = unique("box").upper()
    receiving = ReceivingOperations(pool)

    receiving.receive({
        "seller_external_id": first["seller"], "reference": unique("ТН"),
        "lines": [{"barcode": first["barcode"], "expected_qty": 2, "actual_qty": 2,
                   "box_barcode": box, "cell_address": first["cell"],
                   "comment": "коробка первого"}]})

    with pytest.raises(ValueError, match="другому клиенту"):
        receiving.receive({
            "seller_external_id": second["seller"], "reference": unique("ТН"),
            "lines": [{"barcode": second["barcode"], "expected_qty": 2, "actual_qty": 2,
                       "box_barcode": box, "cell_address": second["cell"],
                       "comment": "чужая коробка"}]})

    inside = rows(pool, "SELECT o.seller_external_id FROM box b "
                        "  JOIN owner o ON o.id = b.owner_id WHERE b.barcode = %s", (box,))
    assert inside[0]["seller_external_id"] == first["seller"], "коробка сменила владельца"


# ----------------------------------------------------------------- документы

def test_a_document_cannot_invent_a_state_or_a_type(
        pool: ConnectionPool, client: dict) -> None:
    """Документом заводят годный товар и брак — и ничего больше.

    `reserved` ставит только резерв: проставить его документом значит завести
    бронь без задания, под которую никто не приедет.
    """
    stock = StockOperations(pool)
    with pytest.raises(ValueError, match="документом не проставляется"):
        stock.apply_document({
            "seller_external_id": client["seller"], "reference": unique("ОТК"),
            "doc_type": "adjustment",
            "lines": [{"barcode": client["barcode"], "quantity": 1,
                       "cell_address": client["cell"], "state": "reserved"}]})

    with pytest.raises(ValueError, match="doc_type"):
        stock.apply_document({
            "seller_external_id": client["seller"], "reference": unique("ОТК"),
            "doc_type": "придуманный",
            "lines": [{"barcode": client["barcode"], "quantity": 1,
                       "cell_address": client["cell"], "state": "good"}]})


def test_a_document_with_nothing_countable_answers_instead_of_crashing(
        pool: ConnectionPool, client: dict) -> None:
    """Документ из одних пустых строк — ответ «ноль движений», а не падение.

    `owner_id` присваивался внутри цикла по строкам: документ, в котором ни
    одна строка не прошла проверку, ронял обработчик `UnboundLocalError`, и
    клиент получал 500 вместо честного «ничего не принято».
    """
    result = StockOperations(pool).apply_document({
        "seller_external_id": client["seller"], "reference": unique("ОТК"),
        "doc_type": "adjustment",
        "lines": [{"barcode": client["barcode"], "quantity": 0,
                   "cell_address": client["cell"], "state": "good"},
                  {"barcode": "", "quantity": 5, "cell_address": client["cell"]}]})

    assert result["moves"] == 0
    assert result["duplicate"] is True


# ------------------------------------------------------------ выбор кабинета

def test_a_client_with_two_cabinets_must_name_the_one(pool: ConnectionPool) -> None:
    """«Первый из списка» — это поставка в чужой кабинет.

    У клиента с двумя кабинетами ответ зависел от порядка строк в базе.
    """
    from app.shipments import ShipmentOperations

    catalog = CatalogOperations(pool)
    seller = unique("seller")
    catalog.upsert_owner({"seller_external_id": seller, "name": "Два кабинета"})
    for suffix in ("a", "b"):
        external = unique(f"wb-{suffix}")
        catalog.upsert_wb_account({
            "op": "upsert", "external_id": external, "seller_external_id": seller,
            "display_name": f"Кабинет {suffix}", "secret_ref": f"vault://mmx/test/{external}",
            "mode": "live", "status": "ACTIVE", "wb_warehouse_id": 1})

    shipments = ShipmentOperations(pool, WmsService(pool))
    with pytest.raises(ValueError, match="назовите нужный"):
        shipments.handle({"seller_external_id": seller, "action": "open",
                          "idempotency_key": unique("open")})

    named = rows(pool, "SELECT a.external_id FROM wb_account a JOIN owner o "
                       "    ON o.id = a.owner_id WHERE o.seller_external_id = %s "
                       " ORDER BY a.external_id", (seller,))[0]["external_id"]
    opened = shipments.handle({"seller_external_id": seller, "action": "open",
                               "idempotency_key": unique("open"),
                               "wb_account_external_id": named})
    assert opened["state"] == "open"

    with pytest.raises(ValueError, match="не принадлежит клиенту"):
        shipments.handle({"seller_external_id": seller, "action": "open",
                          "idempotency_key": unique("open"),
                          "wb_account_external_id": unique("чужой")})


# ---------------------------------------------------- текст ошибки наружу

class _BrokenPool:
    """Пул, который падает так же, как настоящая база под нагрузкой.

    psycopg кладёт в текст отказа и сам запрос, и значения параметров — именно
    это и уезжало клиенту.
    """

    LEAK = "SELECT secret FROM wb_account WHERE token = 'eyJhbGciOiJIUzI1NiJ9'"

    def connection(self, *_args, **_kwargs):
        raise RuntimeError(self.LEAK)

    def close(self) -> None:
        pass


def test_an_internal_failure_gives_a_number_not_a_fragment_of_sql() -> None:
    """Клиент получает номер для лога, а не текст исключения.

    По номеру ту же ошибку находят в логе целиком, и там она безопасна. В
    ответе клиенту не должно быть ни SQL, ни значений параметров.
    """
    import re

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app import routes as routes_module
    from dbfixtures import service_headers

    application = FastAPI()
    application.include_router(routes_module.create_router(_BrokenPool()))

    with TestClient(application, raise_server_exceptions=False) as http:
        answer = http.post(
            "/api/mmx/wms/v1/storage/count",
            json={"jsonrpc": "2.0", "id": 1,
                  "params": {"seller_external_id": "кто-нибудь",
                             "barcode": "4600000000001"}},
            headers=service_headers())

    body = answer.json()
    text = str(body)
    assert "eyJ" not in text, "в ответе клиенту оказался фрагмент токена"
    assert "SELECT" not in text, "в ответе клиенту оказался фрагмент SQL"
    assert re.search(r"request_id=[0-9a-f]{12}", body["error"]["message"]), (
        f"вместо номера для лога клиент получил текст ошибки: "
        f"{body['error']['message']!r}")
