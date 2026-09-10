"""Публикатор outbox: событие доезжает до шины, а склад от неё не зависит.

Отдельный тест на то, что во время разговора с брокером не остаётся открытых
транзакций. Первая версия публикатора держала пачку под `FOR UPDATE SKIP
LOCKED`, пока pika ходила в RabbitMQ, — и полный прогон честно поймал это как
HTTP-вызов внутри транзакции (инвариант 2). Чтобы это не вернулось молча,
проверка живёт здесь, а не только в прогоне.
"""
from __future__ import annotations

import uuid

import pytest

from app import repositories as repo
from app.domain import EventEnvelope
from app.postgres import ConnectionPool, transaction
from app.workers.outbox_publisher import OutboxPublisher

from dbfixtures import require_database, unique


@pytest.fixture(scope="module")
def pool() -> ConnectionPool:
    connections = ConnectionPool(require_database(), max_size=6)
    yield connections
    connections.close()


class Recorder:
    """Публикатор, который вместо шины смотрит, что делает база.

    Это и есть проверка: на месте настоящего сетевого вызова спрашиваем
    Postgres, не ждёт ли кто-нибудь клиента с открытой транзакцией.
    """

    def __init__(self, pool: ConnectionPool, *, fail_on: set[str] | None = None) -> None:
        self._pool = pool
        self._fail_on = fail_on or set()
        self.sent: list[EventEnvelope] = []
        self.idle_in_transaction: list[str] = []

    def publish(self, envelope: EventEnvelope, *, require_broker: bool = False) -> dict:
        with self._pool.connection() as connection:
            with transaction(connection) as cursor:
                cursor.execute(
                    "SELECT left(coalesce(query, ''), 80) AS query FROM pg_stat_activity "
                    " WHERE datname = current_database() AND pid <> pg_backend_pid() "
                    "   AND state = 'idle in transaction'")
                self.idle_in_transaction.extend(row["query"] for row in cursor.fetchall())
        if envelope.type in self._fail_on:
            raise RuntimeError("шина недоступна")
        self.sent.append(envelope)
        return envelope.as_dict()


def put_event(pool: ConnectionPool, event_type: str, aggregate: uuid.UUID) -> uuid.UUID:
    event_id = uuid.uuid4()
    with pool.connection() as connection:
        with transaction(connection) as cursor:
            sequence = repo.next_sequence(cursor, aggregate)
            repo.insert_outbox(
                cursor, event_id=event_id, event_type=event_type, tenant_id="mm-express",
                payload={"task_id": str(aggregate), "sequence": sequence},
                correlation_id=unique("corr"), aggregate_id=aggregate, sequence=sequence)
    return event_id


def drain_until_handled(publisher: OutboxPublisher, pool: ConnectionPool,
                        event_id: uuid.UUID, *, batches: int = 60) -> None:
    """Крутит публикатор, пока очередь не дойдёт до нужного события.

    Очередь идёт по времени появления, а в базе стенда уже лежит хвост
    неопубликованного от прошлых прогонов. Ждать «одного цикла» здесь значило
    бы проверять чужие события вместо своих.
    """
    for _ in range(batches):
        row = published_at(pool, event_id)
        if row and (row["published_at"] is not None or int(row["attempts"]) > 0):
            return
        if publisher._drain() == 0 and _pending_before(pool, event_id) == 0:
            return


def _pending_before(pool: ConnectionPool, event_id: uuid.UUID) -> int:
    with pool.connection() as connection:
        with transaction(connection) as cursor:
            cursor.execute(
                "SELECT count(*) AS n FROM outbox "
                " WHERE published_at IS NULL "
                "   AND (occurred_at, id) < (SELECT occurred_at, id FROM outbox "
                "                             WHERE event_id = %s)", (event_id,))
            row = cursor.fetchone()
            return int(row["n"]) if row else 0


def published_at(pool: ConnectionPool, event_id: uuid.UUID) -> object:
    with pool.connection() as connection:
        with transaction(connection) as cursor:
            cursor.execute("SELECT published_at, attempts, last_error FROM outbox "
                           " WHERE event_id = %s", (event_id,))
            return cursor.fetchone()


def test_nothing_holds_a_transaction_while_the_broker_is_talked_to(
        pool: ConnectionPool) -> None:
    """Инвариант 2: разговор с шиной идёт вне транзакции.

    Открытая транзакция на время сетевого вызова — это заодно блокировка строк
    outbox, то есть очередь публикации, стоящая на скорости брокера.
    """
    aggregate = uuid.uuid4()
    for _ in range(3):
        put_event(pool, "wms.picking.started.v1", aggregate)

    recorder = Recorder(pool)
    OutboxPublisher(pool, recorder)._drain()

    assert len(recorder.sent) >= 3
    assert not recorder.idle_in_transaction, (
        f"во время публикации бэкенд ждал клиента с открытой транзакцией: "
        f"{recorder.idle_in_transaction[:2]}")


def test_a_published_event_is_marked_and_never_sent_twice(pool: ConnectionPool) -> None:
    """Опубликованное не публикуется второй раз при следующем цикле."""
    aggregate = uuid.uuid4()
    event_id = put_event(pool, "wms.picking.completed.v1", aggregate)

    recorder = Recorder(pool)
    publisher = OutboxPublisher(pool, recorder)
    drain_until_handled(publisher, pool, event_id)

    row = published_at(pool, event_id)
    assert row and row["published_at"] is not None

    before = len(recorder.sent)
    publisher._drain()
    assert not [event for event in recorder.sent[before:]
                if event.event_id == str(event_id)], "событие ушло в шину дважды"


def test_a_broker_failure_keeps_the_event_and_counts_the_attempt(
        pool: ConnectionPool) -> None:
    """Раздел 6.1: недоступная шина ничего не теряет.

    Неопубликованное событие не удаляется никогда — это потеря факта, а не
    мусор. Ошибка при этом обязана быть видимой: молчащий воркер считается
    сломанным (инвариант 14).
    """
    aggregate = uuid.uuid4()
    event_id = put_event(pool, "wms.item.scanned.v1", aggregate)

    recorder = Recorder(pool, fail_on={"wms.item.scanned.v1"})
    drain_until_handled(OutboxPublisher(pool, recorder), pool, event_id)

    row = published_at(pool, event_id)
    assert row is not None
    assert row["published_at"] is None, "событие отмечено опубликованным, не уехав"
    assert int(row["attempts"]) >= 1, "неудачная попытка не сосчитана"
    assert row["last_error"], "причина, по которой событие не уехало, не сохранена"
