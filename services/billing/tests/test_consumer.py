"""Консьюмер шины: чужая беда не должна стоить выручки.

Отличать «событие плохое» от «база упала» здесь принципиально. Плохое событие
уходит в мёртвые письма — его разбирает человек. Упавшая база — не повод
терять деньги за чужую беду: перезагрузили Postgres, и вся пачка ушла бы в
разбор вручную.
"""
from __future__ import annotations

import json
import threading
from typing import Any

import psycopg
import pytest

from app.consumer import Consumer, DEAD_LETTER_QUEUE, QUEUE
from app.db import Database

from conftest import event, rows


class FakeMethod:
    def __init__(self, tag: int) -> None:
        self.delivery_tag = tag


class FakeChannel:
    """Канал pika: запоминает, что объявлено и чем закончилось сообщение."""

    def __init__(self, messages: list[dict[str, Any]]) -> None:
        self.declared: dict[str, dict[str, Any]] = {}
        self.bound: list[tuple[str, str]] = []
        self.acked: list[int] = []
        self.nacked: list[tuple[int, bool]] = []
        self.is_closed = False
        self._messages = messages

    def exchange_declare(self, **_kwargs: Any) -> None:
        return None

    def queue_declare(self, queue: str, **kwargs: Any) -> None:
        self.declared[queue] = kwargs

    def queue_bind(self, queue: str, **_kwargs: Any) -> None:
        self.bound.append((queue, _kwargs.get("routing_key", "")))

    def basic_qos(self, **_kwargs: Any) -> None:
        return None

    def consume(self, _queue: str, **_kwargs: Any):
        for index, message in enumerate(self._messages, start=1):
            yield FakeMethod(index), None, json.dumps(message).encode("utf-8")

    def basic_ack(self, tag: int) -> None:
        self.acked.append(tag)

    def basic_nack(self, tag: int, requeue: bool = False) -> None:
        self.nacked.append((tag, requeue))

    def cancel(self) -> None:
        return None


class FakeConnection:
    def __init__(self, channel: FakeChannel) -> None:
        self._channel = channel
        self.is_closed = False

    def channel(self) -> FakeChannel:
        return self._channel

    def close(self) -> None:
        self.is_closed = True


def build(monkeypatch: pytest.MonkeyPatch, service: Any,
          messages: list[dict[str, Any]]) -> tuple[Consumer, FakeChannel]:
    channel = FakeChannel(messages)
    from app import consumer as module

    monkeypatch.setattr(module.pika, "BlockingConnection",
                        lambda *_args, **_kwargs: FakeConnection(channel))
    monkeypatch.setattr(module.pika, "URLParameters", lambda url: url)
    handler = Consumer("amqp://stand", service)
    handler._stopping = threading.Event()
    return handler, channel


class Recording:
    """Сервис, который отвечает так, как велит тест."""

    def __init__(self, database: Database, outcome: Any = None) -> None:
        self.db = database
        self.seen: list[dict[str, Any]] = []
        self._outcome = outcome

    def ingest(self, body: dict[str, Any]) -> dict[str, Any]:
        self.seen.append(body)
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome or {"outcome": "accrued", "amount": "45.00",
                                 "service": "packing"}


def test_the_dead_letter_queue_is_actually_declared(
        database: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    """Ссылка на очередь мёртвых писем ничего не создаёт.

    Если такой очереди нет, RabbitMQ выбрасывает отвергнутое сообщение молча:
    «dead_letters пуст» значит тогда не «потерь нет», а «терялось в никуда»
    (раздел 3.5).
    """
    handler, channel = build(monkeypatch, Recording(database), [])
    handler._consume()

    assert DEAD_LETTER_QUEUE in channel.declared, (
        "очередь мёртвых писем не объявлена — отвергнутые события исчезают")
    assert channel.declared[QUEUE]["arguments"]["x-dead-letter-routing-key"] == \
        DEAD_LETTER_QUEUE


def test_a_database_outage_returns_the_event_to_the_queue(
        database: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    """Упала база — событие возвращается в очередь, а не в мёртвые письма.

    Иначе перезагрузка Postgres превращается в пачку событий, потерянных для
    выручки: каждое из них надо потом переигрывать руками.
    """
    service = Recording(database, psycopg.OperationalError("сервер перезагружается"))
    handler, channel = build(monkeypatch, service,
                             [event("wms.packing.completed.v1", {"seller_id": "seller-1"})])
    reconnected: list[bool] = []
    monkeypatch.setattr(type(database), "reconnect",
                        lambda self: reconnected.append(True))

    handler._consume()

    assert channel.nacked == [(1, True)], (
        f"событие отправлено в мёртвые письма при отказе базы: {channel.nacked}")
    assert not channel.acked
    assert reconnected, "пул не пересоздан: следующее событие получит то же мёртвое соединение"


def test_a_broken_event_goes_to_dead_letters_with_a_trace(
        database: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    """Плохое событие разбирает человек — и ему нужно, что именно случилось.

    След пишется отдельной транзакцией: та, в которой обработчик упал, уже
    откатилась, и запись в неё — это отсутствие записи.
    """
    envelope = event("wms.packing.completed.v1", {"seller_id": "seller-1"})
    with database.transaction() as cursor:
        cursor.execute(
            "INSERT INTO billing_inbox (event_id, event_type, tenant_id, payload, "
            "                           occurred_at, attempts) "
            "VALUES (%s, %s, 'mm-express', '{}'::jsonb, now(), 1)",
            (envelope["event_id"], envelope["type"]))

    service = Recording(database, RuntimeError("тариф посчитался в минус"))
    handler, channel = build(monkeypatch, service, [envelope])

    handler._consume()

    assert channel.nacked == [(1, False)], (
        f"плохое событие не ушло в мёртвые письма: {channel.nacked}")
    trace = rows(database, "SELECT outcome, last_error FROM billing_inbox WHERE event_id = %s",
                 (envelope["event_id"],))
    assert trace and trace[0]["outcome"] == "failed", (
        "след отказа не записан: событие ушло в разбор без единого объяснения")
    assert "минус" in (trace[0]["last_error"] or "")


def test_a_message_that_is_not_json_is_not_retried_forever(
        database: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    """Не JSON — переигрывать нечего: в очередь такое не возвращают."""
    handler, channel = build(monkeypatch, Recording(database), [])

    class Garbage(FakeChannel):
        def consume(self, _queue: str, **_kwargs: Any):
            yield FakeMethod(1), None, b"\xff\xfe not json at all"

    from app import consumer as module
    broken = Garbage([])
    monkeypatch.setattr(module.pika, "BlockingConnection",
                        lambda *_args, **_kwargs: FakeConnection(broken))
    handler._consume()

    assert broken.nacked == [(1, False)]


# --- воркер хранения представляется складу ---------------------------------
#
# С этапа 1 склад спрашивает, кто пришёл (находка 1.3). Воркер хранения ходит
# в него за коробко-местами и токена не посылал: 401 на каждый кабинет и на
# каждый досчитываемый день. Хранение не начислялось НИ ОДНОМУ клиенту, а
# выглядело это как «у всех ноль коробок» — отказ склада считался отсутствием
# количества.

def test_the_storage_worker_introduces_itself_to_the_warehouse(monkeypatch) -> None:
    """Запрос к складу несёт сервисный токен."""
    from app import worker

    seen: dict[str, object] = {}

    class Answer:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {"result": {"placements": [{"box_barcode": "BOX-1"}]}}

    def fake_post(url: str, **kwargs: object) -> Answer:
        seen["url"] = url
        seen["headers"] = kwargs.get("headers")
        return Answer()

    monkeypatch.setattr(worker.httpx, "post", fake_post)
    monkeypatch.setenv("SERVICE_TOKEN", "stand-fake-secret-1")

    assert worker.box_places("кто-то") == 1
    headers = seen["headers"] or {}
    assert headers.get("authorization") == "Bearer stand-fake-secret-1", (
        "воркер хранения пошёл в склад без токена — склад ответит 401, и "
        "хранение не будет начислено никому")


def test_a_warehouse_that_refuses_is_not_reported_as_missing_quantity() -> None:
    """Отказ склада называется отказом склада, а не отсутствием количества.

    Разница не косметическая. По причине «нет количества» дежурный идёт
    смотреть пустые склады, по «склад недоступен» — связь с `wms`. Ровно из-за
    этой подмены 162 строки за такт выглядели нормой, и то, что хранение не
    начисляется вообще, не заметили сутки.
    """
    from app.domain import UnbilledReason

    assert UnbilledReason.WAREHOUSE_UNAVAILABLE.value == "WAREHOUSE_UNAVAILABLE"
    assert UnbilledReason.WAREHOUSE_UNAVAILABLE is not UnbilledReason.NO_QUANTITY
