"""Домен биллинга: услуги, конверт события, формы записей.

Здесь нет обращений к базе и к сети — только правила, которые обязаны
выполняться одинаково и в сервисе, и в консьюмере, и в тестах.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any


class Service(str, Enum):
    """Услуги MM-Express. Совпадает с доменом billing_service в миграции 001."""

    RECEIVING = "receiving"
    STORAGE = "storage"
    LABELING = "labeling"
    PACKING = "packing"
    PICKING = "picking"
    SHIPPING = "shipping"
    RETURNS = "returns"
    ORDER_PROCESSING = "order_processing"


class PartnerRole(str, Enum):
    SENIOR_MANAGER = "senior_manager"
    ACCOUNT_MANAGER = "account_manager"


class UnbilledReason(str, Enum):
    """Почему тарифицируемое событие не стало начислением.

    Не «коды ошибок», а список причин, по которым выручка не доехала до счёта.
    Каждая обязана быть видна поимённо: разница между 6374 заданиями и 145
    начислениями (раздел 3.4) состоит именно из них.
    """

    BAD_ENVELOPE = "BAD_ENVELOPE"
    SELLER_UNKNOWN = "SELLER_UNKNOWN"
    CABINET_UNKNOWN = "CABINET_UNKNOWN"
    NO_TARIFF = "NO_TARIFF"
    TARIFF_NOT_APPROVED = "TARIFF_NOT_APPROVED"
    NO_QUANTITY = "NO_QUANTITY"
    PERIOD_CLOSED = "PERIOD_CLOSED"


class EnvelopeError(ValueError):
    """Конверт не соответствует разделу 2.4 мастера."""


def now() -> datetime:
    return datetime.now(timezone.utc)


def uid() -> str:
    return str(uuid.uuid4())


# Шесть полей конверта, все обязательны (раздел 2.4, приложение E).
ENVELOPE_FIELDS = ("event_id", "tenant_id", "type", "occurred_at", "payload", "correlation_id")


@dataclass(frozen=True)
class Envelope:
    event_id: str
    tenant_id: str
    type: str
    occurred_at: datetime
    payload: dict[str, Any]
    correlation_id: str | None = None

    @property
    def occurred_on(self) -> date:
        return self.occurred_at.date()

    @staticmethod
    def parse(body: Any) -> "Envelope":
        """Разбирает конверт шины, отказывая внятно.

        Отказ — не исключение в лог, а причина BAD_ENVELOPE в billing_unbilled:
        событие, которое биллинг не понял, обязано остаться видимым.
        """
        if not isinstance(body, dict):
            raise EnvelopeError("конверт не является объектом")
        missing = [name for name in ("event_id", "type", "payload") if not body.get(name)]
        if missing:
            raise EnvelopeError(f"в конверте нет полей: {', '.join(missing)}")
        try:
            event_id = str(uuid.UUID(str(body["event_id"])))
        except (ValueError, AttributeError, TypeError) as error:
            raise EnvelopeError("event_id не uuid") from error
        payload = body["payload"]
        if not isinstance(payload, dict):
            raise EnvelopeError("payload не является объектом")
        return Envelope(
            event_id=event_id,
            tenant_id=str(body.get("tenant_id") or ""),
            type=str(body["type"]),
            occurred_at=parse_moment(body.get("occurred_at")),
            payload=payload,
            correlation_id=(str(body["correlation_id"]) if body.get("correlation_id") else None),
        )


def parse_moment(value: Any) -> datetime:
    """Момент события. Без него нельзя определить ни период, ни версию тарифа."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value.strip():
        text = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as error:
            raise EnvelopeError(f"occurred_at неразбираем: {value!r}") from error
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    # Момента нет — берём текущий. Отказывать нельзя: событие уже произошло,
    # и потерять выручку из-за пустого поля хуже, чем начислить его сегодняшним
    # днём. След остаётся в billing_inbox.payload.
    return now()


@dataclass(frozen=True)
class Tier:
    """Объёмная ступень тарифа. up_to=None — последняя, «и далее»."""

    unit_price: Decimal
    up_to: Decimal | None = None
    minimum: Decimal = Decimal("0")


@dataclass(frozen=True)
class PartnerShare:
    """Доля партнёра в наценке: кто, сколько за единицу и из какого слоя."""

    partner_id: str
    markup: Decimal
    price_layer_id: str | None = None


@dataclass(frozen=True)
class Charge:
    """Посчитанное начисление до записи в базу."""

    quantity: Decimal
    unit_price: Decimal
    markup: Decimal
    amount: Decimal
    partner_amount: Decimal
    shares: tuple[PartnerShare, ...] = field(default_factory=tuple)

    @property
    def net_amount(self) -> Decimal:
        return self.amount - self.partner_amount
