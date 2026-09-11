"""Публикация событий в RabbitMQ.

Событие — уведомление, а не способ доставки (раздел 6.1). Поэтому падение
публикации не должно валить операцию склада: mock складывает событие в память
и отдаёт его тестам, даже когда шины нет. Потоки B и C пишут консьюмеры против
формата, а не против доступности брокера.
"""
from __future__ import annotations

import json
import os
import re
import threading
from typing import Any

from .domain import EventEnvelope, validate_event_type

EXCHANGE = os.getenv("MMX_EVENTS_EXCHANGE", "mmx.events")

# Форма JWT. Токены Wildberries — именно JWT (приложение D), и попасть в
# событие они не имеют права никогда (инвариант 15). Проверка стоит здесь,
# а не в ревью, потому что ревью пропускает, а цикл — нет.
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")

_FORBIDDEN_KEYS = frozenset({
    "token", "access_token", "api_key", "apikey", "secret", "client_secret",
    "authorization", "password", "secret_value",
})


class SecretLeak(RuntimeError):
    """В payload попало похожее на секрет. Публикация не состоится."""


class BrokerUnavailable(RuntimeError):
    """Шина недоступна. Значимо только для публикатора outbox."""


# Сколько последних событий держать в памяти публикатора. Это окно для тестов
# стенда, а не хранилище: долговечность даёт таблица outbox.
_MEMORY_LIMIT = 1000


def assert_no_secrets(payload: Any, path: str = "payload") -> None:
    """Обходит payload целиком: и ключи, и значения.

    secret_ref пропускается сознательно — это ссылка на секрет, а не секрет.
    """
    if isinstance(payload, dict):
        for key, value in payload.items():
            if str(key).lower() in _FORBIDDEN_KEYS:
                raise SecretLeak(f"{path}.{key}: поле с секретом в событии запрещено")
            assert_no_secrets(value, f"{path}.{key}")
    elif isinstance(payload, (list, tuple)):
        for index, value in enumerate(payload):
            assert_no_secrets(value, f"{path}[{index}]")
    elif isinstance(payload, str) and _JWT_RE.search(payload):
        # Значение не показываем — иначе секрет утечёт в текст исключения и лог.
        raise SecretLeak(f"{path}: значение имеет форму живого токена WB")


class EventPublisher:
    """Публикатор с памятью. Память — не кэш, а то, что читают тесты стенда."""

    def __init__(self, url: str | None = None) -> None:
        self._url = url or os.getenv("RABBITMQ_URL", "")
        self._published: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._connection: Any = None
        self._channel: Any = None

    @property
    def published(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._published)

    def clear(self) -> None:
        with self._lock:
            self._published.clear()

    def publish(self, envelope: EventEnvelope, *,
                require_broker: bool = False) -> dict[str, Any]:
        """Отправляет событие. `require_broker` — «молчание считать провалом».

        По умолчанию падение брокера проглатывается: склад от шины не зависит
        (раздел 6.1), и уронить резерв из-за недоступного RabbitMQ было бы
        ровно той бедой, которую чиним.

        Публикатор outbox зовёт с `require_broker=True`: он отмечает строку
        опубликованной, и «отправил, кажется» здесь означало бы потерянное
        событие с отметкой об успехе.
        """
        validate_event_type(envelope.type)
        body = envelope.as_dict()
        assert_no_secrets(body["payload"])

        with self._lock:
            self._published.append(body)
            if len(self._published) > _MEMORY_LIMIT:
                # Память публикатора — окно для тестов стенда, а не журнал.
                # Без ограничения долгоживущий воркер съел бы её целиком.
                del self._published[:-_MEMORY_LIMIT]

        self._try_broker(envelope.type, body, require_broker=require_broker)
        return body

    def _try_broker(self, routing_key: str, body: dict[str, Any], *,
                    require_broker: bool = False) -> None:
        if not self._url:
            if require_broker:
                raise BrokerUnavailable("RABBITMQ_URL не задан")
            return
        try:
            import pika  # локальный импорт: без брокера mock обязан работать
        except ImportError:
            if require_broker:
                raise BrokerUnavailable("нет клиента pika")
            return
        try:
            if self._connection is None or self._connection.is_closed:
                self._connection = pika.BlockingConnection(pika.URLParameters(self._url))
                self._channel = None
            if self._channel is None or self._channel.is_closed:
                # Канал переиспользуется, а не создаётся на каждое событие:
                # публикатор outbox шлёт их тысячами, а лимит каналов на
                # соединение у RabbitMQ конечен — новый канал на событие
                # исчерпал бы его за одну смену.
                self._channel = self._connection.channel()
                self._channel.exchange_declare(
                    exchange=EXCHANGE, exchange_type="topic", durable=True)
                # Подтверждения брокера. Без них `basic_publish` возвращается
                # успешно, как только байты ушли в сокет: событие считается
                # опубликованным, а RabbitMQ мог его не принять вовсе. Тогда
                # `published_at` проставлен, из outbox строка ушла, и событие
                # потеряно молча — ровно то, что outbox и должен исключать.
                self._channel.confirm_delivery()
            channel = self._channel
            channel.basic_publish(
                exchange=EXCHANGE,
                routing_key=routing_key,
                body=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                properties=pika.BasicProperties(content_type="application/json", delivery_mode=2),
                # Некуда положить — отказ, а не тишина. Событие без очереди
                # уходит в никуда: обмен принимает его и выбрасывает.
                mandatory=True,
            )
        except Exception as failure:
            # Склад не зависит от шины (раздел 6.1). Событие уже в памяти;
            # молчаливая потеря брокера не имеет права уронить операцию.
            self._connection = None
            self._channel = None
            if require_broker:
                raise BrokerUnavailable(str(failure)) from None
