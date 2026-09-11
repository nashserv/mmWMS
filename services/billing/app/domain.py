"""Домен биллинга: услуги, конверт события, формы записей.

Здесь нет обращений к базе и к сети — только правила, которые обязаны
выполняться одинаково и в сервисе, и в консьюмере, и в тестах.
"""
from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo
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


# Зона склада. Все даты — периоды, дни выработки, границы месяцев —
# считаются в ней.
#
# `occurred_at.date()` брал дату в UTC: событие в 02:00 по Москве
# 1 сентября — это 23:00 31 августа по UTC, и начисление уезжало в ЧУЖОЙ
# МЕСЯЦ. Счёт за сентябрь недосчитывал ночную смену первого числа, а
# августовский счёт, уже выставленный, получал начисление задним числом.
# Склад в Москве, смены в Москве, счета в Москве.
WAREHOUSE_ZONE = ZoneInfo(os.getenv("BILLING_TIMEZONE", "Europe/Moscow"))


def now() -> datetime:
    return datetime.now(timezone.utc)


def local_date(moment: datetime) -> date:
    """Дата события в зоне склада."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(WAREHOUSE_ZONE).date()


def today() -> date:
    """Сегодня — по складу, а не по UTC."""
    return local_date(now())


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
        """Дата события в зоне склада, а не в UTC.

        Событие в 02:00 по Москве 1 сентября — это 23:00 31 августа по UTC.
        По UTC оно уезжало в чужой месяц.
        """
        return local_date(self.occurred_at)

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
    # Момента нет — конверт неразбираем, и это отказ.
    #
    # Подстановка «сегодня» выглядела спасением выручки, а была тихой ложью:
    # событие сентябрьской ночи, доехавшее в октябре, начислялось октябрём — в
    # период, за который счёт ещё не выставлен, и клиент платил за чужой месяц.
    # А главное, разобрать это было нечем: в базе лежала дата, которой не было
    # у события.
    #
    # Выручка не теряется: BAD_ENVELOPE видно в отчёте, событие переигрывается
    # `POST /unbilled/{id}/replay`, когда издатель починит конверт.
    raise EnvelopeError(
        "occurred_at отсутствует: без момента события нельзя определить ни "
        "период, ни версию тарифа, а подставить сегодняшний день значит "
        "начислить клиенту чужой месяц")


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
