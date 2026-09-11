"""Публикатор outbox: события из базы в RabbitMQ.

Событие — уведомление, а не способ доставки (раздел 6.1). Склад уже сделал
свою работу и записал её в базу до того, как этот воркер проснулся; его дело —
чтобы биллинг, портал и аналитика узнали. Поэтому недоступный брокер здесь не
теряет ничего: строка остаётся неопубликованной и уедет следующим циклом.

Ретеншен тоже здесь. В боевом контуре `integration_outbox` дорос до 136 210
записей без чистки вообще (раздел 3.6). Чистится только опубликованное и
только целыми партициями: неопубликованное событие не удаляется никогда — это
потеря факта, а не мусор.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
from typing import Any

from .. import repositories as repo
from ..domain import EventEnvelope
from ..events import EventPublisher
from ..postgres import ConnectionPool, pool as shared_pool, single, transaction
from .loop import Worker, configure_logging

log = logging.getLogger("wms.outbox")

BATCH = int(os.getenv("WMS_OUTBOX_BATCH", "200"))
# На сколько пачка закрепляется за публикатором. Больше, чем занимает её
# отправка, и меньше, чем человек готов ждать восстановления после падения.
LEASE_SECONDS = int(os.getenv("WMS_OUTBOX_LEASE_SECONDS", "30"))
IDLE_SECONDS = float(os.getenv("WMS_OUTBOX_IDLE_SECONDS", "0.2"))
# Сколько дней держать опубликованное. Партиция удаляется целиком и только
# если в ней не осталось ни одного неопубликованного события.
RETENTION_DAYS = int(os.getenv("WMS_OUTBOX_RETENTION_DAYS", "90"))
MAINTENANCE_EVERY = int(os.getenv("WMS_OUTBOX_MAINTENANCE_TICKS", "600"))


class OutboxPublisher:
    def __init__(self, pool: ConnectionPool, publisher: EventPublisher | None = None) -> None:
        self._pool = pool
        self._publisher = publisher or EventPublisher()
        self._ticks = 0

    def tick(self) -> int:
        self._ticks += 1
        if self._ticks % MAINTENANCE_EVERY == 1:
            self._maintain()
        return self._drain()

    def _drain(self) -> int:
        """Забрать пачку, отпустить базу, опубликовать, отметить.

        Три шага, а не один, ровно из-за инварианта 2: разговор с брокером —
        это сеть, и держать на нём открытую транзакцию с блокировками строк
        нельзя. Бэкенд, ждущий клиента с открытой транзакцией, — тот самый
        отпечаток, по которому полный прогон ловит вызов внутри транзакции.

        Плата за это — доставка «хотя бы один раз»: если процесс умрёт между
        публикацией и отметкой, событие уедет второй раз. Так и задумано,
        поэтому в конверте есть `event_id` — потребитель различает повтор по
        нему, а порядок в пределах задания повтор не нарушает.
        """
        claimed = self._claim()
        if not claimed:
            return 0

        published: list[tuple[int, Any]] = []
        failed: list[tuple[int, Any, str]] = []
        for row in claimed:
            envelope = EventEnvelope(
                type=row["type"], tenant_id=row["tenant_id"],
                payload=row["payload"], correlation_id=row["correlation_id"],
                event_id=str(row["event_id"]),
                occurred_at=_isoformat(row["occurred_at"]))
            try:
                self._publisher.publish(envelope, require_broker=True)
            except Exception as failure:
                failed.append((row["id"], row["occurred_at"], str(failure)))
                continue
            published.append((row["id"], row["occurred_at"]))

        with self._pool.connection() as connection:
            with transaction(connection) as cursor:
                repo.mark_published(cursor, published)
                repo.mark_failed(cursor, failed)
        if failed:
            log.warning("не ушло в шину: %d событий, первое — %s",
                        len(failed), failed[0][2][:120])
        return len(published)

    def _claim(self) -> list[dict[str, Any]]:
        """Короткая транзакция: прочитать пачку и сразу отпустить блокировки.

        Лизинг явный (`claimed_until`), а не только `FOR UPDATE SKIP LOCKED`:
        блокировка строки живёт до конца запроса, а публикация начинается
        после него — второй публикатор в это окно видел те же события
        непубликованными и отправлял их второй раз.
        """
        with self._pool.connection() as connection:
            # Одиночный запрос без явной транзакции: блокировка строк всё равно
            # живёт ровно до конца этого запроса, а BEGIN/COMMIT вокруг него
            # только добавляют круг до сервера и промежуток idle in transaction.
            with single(connection) as cursor:
                return [dict(row) for row in repo.pending_outbox(
                    cursor, BATCH, lease_seconds=LEASE_SECONDS)]

    def _maintain(self) -> None:
        """Партиции вперёд и чистка опубликованного.

        Заводится сразу несколько месяцев вперёд: публикация не должна
        упереться в отсутствующий раздел в первую же ночь после Нового года.
        """
        cutoff = (dt.date.today() - dt.timedelta(days=RETENTION_DAYS)).replace(day=1)
        with self._pool.connection() as connection:
            with transaction(connection) as cursor:
                for month in range(0, 4):
                    cursor.execute(
                        "SELECT wms_outbox_ensure_partition("
                        "    (date_trunc('month', now()) + make_interval(months => %s))::date)",
                        (month,))
                cursor.execute("SELECT dropped FROM wms_outbox_drop_published_before(%s)",
                               (cutoff,))
                dropped = [row["dropped"] for row in cursor.fetchall()]
        if dropped:
            log.info("ретеншен outbox: удалены партиции %s", ", ".join(dropped))


def _isoformat(value: object) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def main() -> None:
    configure_logging()
    publisher = OutboxPublisher(shared_pool())
    Worker("outbox_publisher", idle_seconds=IDLE_SECONDS).run(publisher.tick)


if __name__ == "__main__":
    main()
