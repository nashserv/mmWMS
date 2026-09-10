"""Тесты mock-сервиса: проверяют контракт, а не выдумки реализации.

Каждый тест назван по тому инварианту или разделу мастера, который защищает.
"""
from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from app import fixtures
from app.api import create_app, publisher, state
from app.domain import EventEnvelope, WMS_EVENT_TYPES
from app.events import SecretLeak, assert_no_secrets

BASE = "/api/mmx/wms/v1"


@pytest.fixture()
def client() -> TestClient:
    state.reset()
    publisher.clear()
    return TestClient(create_app())


def call(client: TestClient, path: str, params: dict | None = None) -> dict:
    response = client.post(
        f"{BASE}{path}",
        json={"jsonrpc": "2.0", "method": "call", "params": params or {}, "id": 7},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    # Конверт JSON-RPC: ответ обязан лежать в result и вернуть тот же id.
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == 7
    return body["result"]


def reserve(client: TestClient, barcode: str, *, seller: str = "seller-a",
            quantity: int = 1, order_id: int = 5001) -> dict:
    # wb_order_id обязателен по контракту — вызываем так же, как настоящий клиент.
    return call(client, "/reservations", {
        "seller_external_id": seller, "barcode": barcode, "quantity": quantity,
        "wb_order_id": order_id, "idempotency_key": f"idem-{order_id}",
        "correlation_id": f"corr-{order_id}",
    })


# ------------------------------------------------------------------ резерв

def test_known_barcode_reserves(client: TestClient) -> None:
    result = reserve(client, "2000000000011")
    # Клиент считает принятым только при status == "reserved" (приложение C).
    assert result["status"] == "reserved"
    assert result["owner_external_id"] == "seller-a"
    assert result["error_code"] is None
    assert result["task_id"]


def test_unknown_barcode_gives_product_mapping_missing(client: TestClient) -> None:
    result = reserve(client, "0000000000000")
    assert result["status"] != "reserved"
    assert result["error_code"] == "PRODUCT_MAPPING_MISSING"
    assert result["task_id"] is None


def test_two_matches_are_ambiguous_not_first_wins(client: TestClient) -> None:
    """Раздел 3.2: поиск с limit=2, два совпадения — это AMBIGUOUS.

    У одной карточки несколько размеров; взять первое — значит отгрузить не ту вещь.
    """
    result = reserve(client, fixtures.AMBIGUOUS_BARCODE)
    assert result["error_code"] == "AMBIGUOUS_PRODUCT_MAPPING"


def test_unknown_seller_gives_seller_mapping_missing(client: TestClient) -> None:
    result = reserve(client, "2000000000011", seller="seller-not-here")
    assert result["error_code"] == "SELLER_MAPPING_MISSING"


def test_ledger_short_reserves_and_leaves_a_trace(client: TestClient) -> None:
    """Инвариант 12: сборка без остатка оставляет след.

    Молчаливого _force_reservation (раздел 3.1) больше нет — резерв проходит,
    но событие wms.stock.shortfall.v1 обязано вылететь.
    """
    result = reserve(client, fixtures.ZERO_STOCK_BARCODE, seller="seller-a")
    assert result["status"] == "reserved"
    assert result["ledger_short"] is True

    shortfall = [e for e in publisher.published if e["type"] == "wms.stock.shortfall.v1"]
    assert len(shortfall) == 1
    payload = shortfall[0]["payload"]
    # Поля строго по пункту 3 файла 01.
    for field in ("owner_id", "sku_id", "cell_id", "qty_short", "task_id"):
        assert field in payload, f"в payload не хватает {field}"
    assert payload["qty_short"] > 0
    # Расхождение обязано быть адресным: без ячейки инвентаризация не знает,
    # куда идти (раздел 6.5).
    assert payload["cell_id"], "у сборки без остатка должна быть ячейка"

    # Два события одного задания не имеют права делить один номер: на строгую
    # монотонность sequence опираются потребители (приложение E).
    # У payload shortfall номера нет по контракту, поэтому смотрим только те
    # события, где sequence предусмотрен.
    task_id = result["task_id"]
    sequences = [event["payload"]["sequence"] for event in publisher.published
                 if event["payload"].get("task_id") == task_id
                 and "sequence" in event["payload"]]
    assert len(sequences) == len(set(sequences)), f"номера событий повторяются: {sequences}"


def test_a_client_registered_at_runtime_can_reserve(client: TestClient) -> None:
    """Happy-path целиком: завели клиента, товар, остаток — резерв проходит.

    Это путь шага 1 прогона. Пока маршруты только читали, заведённый на ходу
    продавец не существовал для резерва, и любой вызов возвращал
    SELLER_MAPPING_MISSING — весь прогон вставал на первом шаге, и потоки B и C
    не могли пройти ни одного сценария против заглушки.
    """
    seller = "runtime-seller"
    barcode = "4600000000017"

    call(client, "/sellers", {"seller_external_id": seller, "name": "Клиент на ходу",
                              "inn": "0000000000", "allow_ledger_short": True})
    call(client, "/catalog/products/ensure", {
        "seller_external_id": seller, "barcode": barcode, "name": "Товар на ходу"})
    call(client, "/warehouse/documents", {
        "seller_external_id": seller, "reference": "OPEN-1", "doc_type": "opening",
        "comment": "начальный остаток от владельца компании",
        "lines": [{"barcode": barcode, "quantity": 10, "cell_address": "FR-01-01"}]})

    result = call(client, "/reservations", {
        "seller_external_id": seller, "barcode": barcode, "quantity": 2,
        "wb_order_id": 5501, "idempotency_key": "idem-5501", "correlation_id": "corr-5501"})

    assert result["status"] == "reserved", result
    assert result["owner_external_id"] == seller
    assert result["error_code"] is None
    # Остаток был — значит это обычный резерв, а не клапан раздела 6.5.
    assert result["ledger_short"] is False


def test_registering_a_client_twice_does_not_create_a_second_one(client: TestClient) -> None:
    """Инвариант 5. Новый owner_id обесценил бы уже выпущенные события."""
    first = call(client, "/sellers", {"seller_external_id": "twice", "name": "Первый"})
    second = call(client, "/sellers", {"seller_external_id": "twice", "name": "Второй"})
    # Форма — SellerResult: один владелец, а не список (контракт не знает
    # списка вовсе). Тот же owner_id и `created: false` на повторе.
    assert first["owner_id"] == second["owner_id"]
    assert first["created"] is True and second["created"] is False


def test_opening_document_is_idempotent_by_reference(client: TestClient) -> None:
    """Повторный документ — тот же ответ, а не второй начальный остаток."""
    call(client, "/sellers", {"seller_external_id": "open-twice"})
    call(client, "/catalog/products/ensure",
         {"seller_external_id": "open-twice", "barcode": "4600000000024"})
    params = {"seller_external_id": "open-twice", "reference": "OPEN-SAME",
              "doc_type": "opening",
              "lines": [{"barcode": "4600000000024", "quantity": 7,
                         "cell_address": "FR-01-02"}]}
    first = call(client, "/warehouse/documents", params)
    second = call(client, "/warehouse/documents", params)
    # Форма — WarehouseDocumentResult: внутреннего document_id клиент не
    # видит, идемпотентность доказывает `reference` и неизменившийся остаток.
    assert first["reference"] == second["reference"]
    assert first["state"] == second["state"] == "applied"

    stocks = call(client, "/catalog/stocks/bulk",
                  {"seller_external_id": "open-twice"})["stocks"]
    assert [row["available"] for row in stocks if row["barcode"] == "4600000000024"] == [7]


def test_a_sku_of_one_owner_is_invisible_to_another(client: TestClient) -> None:
    """Изоляция владельца доходит до каталога (инвариант 6).

    Одинаковый штрихкод у двух клиентов — это две разные вещи на полке.
    """
    call(client, "/sellers", {"seller_external_id": "owner-one"})
    call(client, "/catalog/products/ensure",
         {"seller_external_id": "owner-one", "barcode": "4600000000031"})
    call(client, "/sellers", {"seller_external_id": "owner-two"})

    result = call(client, "/reservations", {
        "seller_external_id": "owner-two", "barcode": "4600000000031", "quantity": 1,
        "wb_order_id": 5502, "correlation_id": "corr-5502"})
    assert result["error_code"] == "PRODUCT_MAPPING_MISSING"


def test_receipt_registers_an_owner_the_warehouse_sees_for_the_first_time(
        client: TestClient) -> None:
    """Приложение C: владельца заводят по seller_name и seller_inn.

    Приёмка — первый момент, когда вещь вообще появляется на складе.
    """
    result = call(client, "/receipts", {
        "seller_external_id": "brand-new", "warehouse_code": "RUM",
        "reference": "RCP-NEW-1", "seller_name": "ИП Новый", "seller_inn": "1111111111",
        "lines": [{"barcode": "4600000000048", "expected_qty": 4, "actual_qty": 4}]})
    assert result["state"] == "accepted"

    reserved = call(client, "/reservations", {
        "seller_external_id": "brand-new", "barcode": "4600000000048", "quantity": 1,
        "wb_order_id": 5503, "correlation_id": "corr-5503"})
    assert reserved["status"] == "reserved", reserved


def test_wb_account_upsert_keeps_the_token_out(client: TestClient) -> None:
    """Инвариант 15: принимается ссылка на секрет, не его значение."""
    result = call(client, "/wb/accounts", {
        "op": "upsert", "external_id": "wb-new-1", "seller_external_id": "seller-a",
        "display_name": "Новый кабинет", "secret_ref": "vault://mmx/stand/new",
        "mode": "shadow", "status": "ACTIVE"})
    assert result["created"] is True
    account = next(a for a in result["accounts"] if a["external_id"] == "wb-new-1")
    assert account["secret_ref"].startswith("vault://")
    assert not {key.lower() for key in account} & {
        "token", "api_key", "secret", "access_token", "authorization"}


def test_valve_is_per_owner_not_global(client: TestClient) -> None:
    """Раздел 6.5: клапан отключается на уровне владельца товара.

    У seller-b он выключен, поэтому нехватка остатка — отказ, а не сборка в
    минус. Товар заводим этому же владельцу: чужой SKU дал бы отказ маппинга,
    а не проверку клапана (инвариант 6).
    """
    call(client, "/catalog/products/ensure", {
        "seller_external_id": "seller-b", "barcode": "2000000000777",
        "name": "Товар без остатка у seller-b"})

    result = reserve(client, "2000000000777", seller="seller-b")
    assert result["error_code"] == "INSUFFICIENT_STOCK"


# ------------------------------------------------------------------ выдача заданий

def test_pull_returns_tasks(client: TestClient) -> None:
    reserve(client, "2000000000011", order_id=6001)
    reserve(client, "2000000000028", order_id=6002)
    result = call(client, "/tasks/pull", {"assignee": "picker-1", "limit": 10})
    assert len(result["tasks"]) == 2
    assert result["served_at"]


def test_task_is_never_handed_to_two_pickers(client: TestClient) -> None:
    """Шаг 8 прогона: пять параллельных сессий, ни одно задание не выдано двоим."""
    for index in range(5):
        reserve(client, "2000000000011", order_id=7000 + index)

    first = call(client, "/tasks/pull", {"assignee": "picker-1", "limit": 5})
    second = call(client, "/tasks/pull", {"assignee": "picker-2", "limit": 5})

    first_ids = {row["task"]["task_id"] for row in first["tasks"]}
    second_ids = {row["task"]["task_id"] for row in second["tasks"]}
    # Лизинг обязателен: без срока задание зависнет за пропавшим сборщиком.
    assert all(row["leased_until"] for row in first["tasks"])
    assert first_ids and not second_ids & first_ids
    assert len(first_ids) == 5


# ------------------------------------------------------------------ этикетка

def test_label_is_local_before_packing(client: TestClient) -> None:
    """Инвариант 9: стикер лежит локально до того, как человек нажал печать."""
    task_id = reserve(client, "2000000000011", order_id=8001)["task_id"]
    label = call(client, f"/tasks/{task_id}/label")
    # `content_type` — MIME-тип, имя формата лежит в `format`. `payload` —
    # base64: контракт объявляет `contentEncoding: base64`, и клиент,
    # выучивший у заглушки сырой ZPL, споткнулся бы на настоящем сервисе.
    assert label["content_type"] == "application/x-zpl"
    assert label["format"] == "zplv"
    assert base64.b64decode(label["payload"]).decode("utf-8").startswith("^XA")
    assert len(label["checksum"]) == 64


def test_cancel_invalidates_the_label(client: TestClient) -> None:
    """Раздел 6.6: после отмены локальный стикер помечается недействительным."""
    task_id = reserve(client, "2000000000011", order_id=8002)["task_id"]
    result = call(client, f"/tasks/{task_id}/cancel",
                  {"reason": "клиент отменил", "cancellation_event_id": "evt-1"})
    # Форма — TaskCancelResult: состояние в `state`, причина обязательна.
    assert result["state"] == "cancelled"
    assert result["cancel_reason"] == "клиент отменил"
    assert result["label_invalidated"] is True
    assert call(client, f"/tasks/{task_id}/label")["error_code"] == "LABEL_NOT_READY"


def test_cancel_without_a_reason_is_refused(client: TestClient) -> None:
    """Инвариант 11: причина отмены обязательна.

    В боевом контуре у всех 2645 отмен она была NULL (раздел 3).
    """
    task_id = reserve(client, "2000000000011", order_id=8003)["task_id"]
    result = call(client, f"/tasks/{task_id}/cancel", {})
    assert result["error_code"] == "CANCEL_REASON_REQUIRED"


# ------------------------------------------------------------------ скан

def test_wrong_barcode_is_rejected_at_the_bench(client: TestClient) -> None:
    """Раздел 4: главный рубеж качества — контрольный скан при упаковке."""
    task_id = reserve(client, "2000000000011", order_id=8004)["task_id"]
    result = call(client, f"/tasks/{task_id}/scan", {"barcode": "2000000000042"})
    assert result["status"] == "rejected"
    assert result["scan_result"] == "wrong_barcode"


def test_matching_barcode_is_picked(client: TestClient) -> None:
    task_id = reserve(client, "2000000000011", order_id=8005)["task_id"]
    result = call(client, f"/tasks/{task_id}/scan", {"barcode": "2000000000011"})
    # Клиент принимает, только если owner совпал и status == "picked" (приложение C).
    assert result["status"] == "picked"
    assert result["owner_external_id"] == "seller-a"


# ------------------------------------------------------------------ приёмка

def test_receipt_is_idempotent_by_reference(client: TestClient) -> None:
    """Инвариант 5: повтор — тот же ответ, а не вторая приёмка."""
    params = {"seller_external_id": "seller-a", "warehouse_code": "RUM",
              "reference": "RCP-0001",
              "lines": [{"barcode": "2000000000011", "expected_qty": 5, "actual_qty": 5}]}
    first = call(client, "/receipts", params)
    second = call(client, "/receipts", params)
    assert first["receipt_id"] == second["receipt_id"]


def test_receipt_shortage_creates_a_discrepancy(client: TestClient) -> None:
    """Шаг 3 прогона: баланс по факту, не по ожиданию, расхождение зафиксировано."""
    result = call(client, "/receipts", {
        "seller_external_id": "seller-a", "warehouse_code": "RUM", "reference": "RCP-0002",
        "lines": [{"barcode": "2000000000011", "expected_qty": 10, "actual_qty": 7}]})
    # Расхождение несёт ещё и свой идентификатор, адрес и время: экран
    # начальника склада читает их по /discrepancies, и без них расхождение
    # неадресно (раздел 6.5).
    assert len(result["discrepancies"]) == 1
    row = result["discrepancies"][0]
    assert (row["barcode"], row["kind"], row["qty"], row["decision"]) == (
        "2000000000011", "shortage", 3, "pending")
    assert row["discrepancy_id"] and row["created_at"]


# ------------------------------------------------------------------ коробки

def test_box_without_a_comment_is_refused(client: TestClient) -> None:
    """Раздел 2.9: комментарий обязателен — иначе коробку через месяц не найти."""
    result = call(client, "/boxes", {"barcode": "BOX-9999",
                                     "seller_external_id": "seller-a", "comment": "  "})
    assert result["error_code"] == "BOX_COMMENT_REQUIRED"


# ------------------------------------------------------------------ остатки

def test_bulk_stocks_drop_broken_rows(client: TestClient) -> None:
    """Приложение C: строки без штрихкода или с отрицательным available отбрасываются."""
    result = call(client, "/catalog/stocks/bulk", {"seller_external_id": "seller-a"})
    assert result["stocks"]
    for row in result["stocks"]:
        assert row["barcode"]
        assert isinstance(row["available"], int) and row["available"] >= 0


def test_reservation_lowers_available(client: TestClient) -> None:
    """Инвариант 7: в WB публикуется заниженный остаток."""
    before = {r["barcode"]: r["available"]
              for r in call(client, "/catalog/stocks/bulk",
                            {"seller_external_id": "seller-a"})["stocks"]}
    reserve(client, "2000000000011", quantity=3, order_id=9001)
    after = {r["barcode"]: r["available"]
             for r in call(client, "/catalog/stocks/bulk",
                           {"seller_external_id": "seller-a"})["stocks"]}
    assert after["2000000000011"] <= before["2000000000011"] - 3


# ------------------------------------------------------------------ кабинеты WB

def test_wb_accounts_never_expose_a_token(client: TestClient) -> None:
    """Инвариант 15: значение токена не появляется в ответах API никогда."""
    result = call(client, "/wb/accounts")
    for account in result["accounts"]:
        assert account["secret_ref"].startswith("vault://")
        for forbidden in ("token", "api_key", "secret", "authorization"):
            assert forbidden not in {key.lower() for key in account
                                     if key.lower() != "token_type"}


# ------------------------------------------------------------------ события

def test_every_emitted_event_is_in_the_catalogue(client: TestClient) -> None:
    reserve(client, "2000000000011", order_id=9101)
    for event in publisher.published:
        assert event["type"] in WMS_EVENT_TYPES


def test_envelope_has_exactly_the_contract_fields(client: TestClient) -> None:
    """Раздел 2.4: конверт с additionalProperties: false — ровно шесть полей."""
    reserve(client, "2000000000011", order_id=9102)
    assert publisher.published
    for event in publisher.published:
        assert set(event) == {"event_id", "tenant_id", "type", "occurred_at",
                              "payload", "correlation_id"}


def test_sequence_grows_within_one_task(client: TestClient) -> None:
    """Приложение E: потребители полагаются на монотонность в пределах задания."""
    task_id = reserve(client, "2000000000011", order_id=9103)["task_id"]
    call(client, f"/tasks/{task_id}/scan", {"barcode": "2000000000011"})
    call(client, f"/tasks/{task_id}/pack")

    sequences = [event["payload"]["sequence"] for event in publisher.published
                 if event["payload"].get("task_id") == task_id
                 and "sequence" in event["payload"]]
    assert sequences == sorted(sequences)
    assert len(set(sequences)) == len(sequences)


def test_a_token_shaped_value_never_reaches_the_bus() -> None:
    """Инвариант 15 как механическая проверка, а не как пожелание."""
    jwt = ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
           ".eyJzdWIiOiIxMjM0NTY3ODkwIn0"
           ".dQw4w9WgXcQabcdefghijklmnop")
    with pytest.raises(SecretLeak):
        assert_no_secrets({"nested": {"value": jwt}})
    with pytest.raises(SecretLeak):
        assert_no_secrets({"api_key": "whatever"})
    # Ссылка на секрет — это не секрет, она проходить обязана.
    assert_no_secrets({"secret_ref": "vault://mmx/wb/seller-a"})


def test_publish_refuses_an_unknown_event_type() -> None:
    from app.domain import EventTypeNotAllowed
    from app.events import EventPublisher

    with pytest.raises(EventTypeNotAllowed):
        EventPublisher(url="").publish(EventEnvelope(
            type="wms.something.invented.v1", tenant_id="mm-express",
            payload={}, correlation_id="corr-1"))


# ------------------------------------------------------------------ работа без шины

def test_tasks_are_visible_without_a_broker(client: TestClient) -> None:
    """Шаг 14 прогона: RabbitMQ выключен, задания продолжают появляться.

    Событие — уведомление, а не способ доставки (раздел 6.1). Брокера в тестах
    нет вовсе, и /tasks/pull обязан работать.
    """
    reserve(client, "2000000000011", order_id=9201)
    assert len(call(client, "/tasks/pull", {"assignee": "picker-1", "limit": 5})["tasks"]) == 1
