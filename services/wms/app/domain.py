"""Доменные типы сервиса wms: состояния, коды ошибок, конверт события.

Значения заморожены потоком 0. Источники: раздел 7 и приложения A, C, E
мастер-контекста, таблица docs/state-mapping.md.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def uid() -> str:
    return str(uuid.uuid4())


class TaskState(str, Enum):
    """Автомат задания. Полная таблица переходов — docs/state-mapping.md."""

    NEW = "new"
    RESERVED = "reserved"
    PICKING = "picking"
    PICKED = "picked"
    PACKED = "packed"
    LABELED = "labeled"
    IN_SUPPLY = "in_supply"
    SHIPPED = "shipped"
    # Передано человеком и подтверждено сверкой — разные факты (раздел 2.12).
    HANDED = "handed"
    ACCEPTED = "accepted"
    SHORT = "short"
    CANCELLED = "cancelled"
    MANUAL_REVIEW = "manual_review"
    DIVERGED = "diverged"


class StockState(str, Enum):
    """Состояние вещи на полке. Домен wms_stock_state в схеме."""

    GOOD = "good"
    RESERVED = "reserved"
    QUARANTINE = "quarantine"
    DEFECT = "defect"
    BLOCKED = "blocked"
    PROCESSING = "processing"


# Какой статус Wildberries соответствует нашему состоянию задания.
# Таблица 1 файла docs/state-mapping.md, заморожена потоком 0. Отсюда работает
# сверка: расхождение с этой таблицей — состояние `diverged` и алерт, а не
# тихая перезапись (инвариант 10). В боевом контуре таких расхождений 2467 из
# 6374, и все они молчали.
EXPECTED_WB_STATUS: dict[str, str] = {
    TaskState.NEW.value: "new",
    TaskState.MANUAL_REVIEW.value: "new",
    TaskState.SHORT.value: "new",
    TaskState.RESERVED.value: "new",
    TaskState.PICKING.value: "new",
    TaskState.PICKED.value: "new",
    TaskState.PACKED.value: "new",
    TaskState.LABELED.value: "new",
    TaskState.IN_SUPPLY.value: "confirm",
    TaskState.SHIPPED.value: "complete",
    TaskState.HANDED.value: "complete",
    TaskState.ACCEPTED.value: "complete",
    TaskState.CANCELLED.value: "cancel",
}

# Обратное направление — так работает опросчик (таблица 2 того же файла).
WB_STATUS_TO_STATE: dict[str, str] = {
    "new": TaskState.RESERVED.value,
    "confirm": TaskState.IN_SUPPLY.value,
    "complete": TaskState.SHIPPED.value,
    "cancel": TaskState.CANCELLED.value,
}


def agrees_with_wb(state: str, wb_status: str | None) -> bool:
    """Согласуются ли наше состояние и статус Wildberries.

    `diverged` согласуется с чем угодно: расхождение уже зафиксировано, и
    заново расходиться ему некуда. Неизвестный статус WB считается
    расхождением — незнакомое значение разбирает человек, а не догадка.
    """
    if state == TaskState.DIVERGED.value:
        return True
    if wb_status is None:
        return True
    return EXPECTED_WB_STATUS.get(state) == wb_status


class ErrorCode(str, Enum):
    """Коды отказа резерва (приложение C).

    Сохранены все до единого: на них завязаны уже написанные клиенты, и
    AMBIGUOUS_PRODUCT_MAPPING отличается от PRODUCT_MAPPING_MISSING по
    смыслу — два совпадения это не «берём первое» (раздел 3.2).
    """

    SELLER_MAPPING_MISSING = "SELLER_MAPPING_MISSING"
    PRODUCT_MAPPING_MISSING = "PRODUCT_MAPPING_MISSING"
    AMBIGUOUS_PRODUCT_MAPPING = "AMBIGUOUS_PRODUCT_MAPPING"
    WAREHOUSE_UNKNOWN = "WAREHOUSE_UNKNOWN"
    INSUFFICIENT_STOCK = "INSUFFICIENT_STOCK"
    SERIALIZATION_RETRY = "SERIALIZATION_RETRY"


class DiscrepancyKind(str, Enum):
    SHORTAGE = "shortage"
    SURPLUS = "surplus"
    MISMATCH = "mismatch"
    DAMAGE = "damage"
    # Клапан «собрать без остатка» (раздел 6.5).
    LEDGER_SHORT = "ledger_short"


# Типы событий, разрешённые к эмиссии (приложение E, WMS_EVENT_TYPES).
# Список валидируется при отправке: незнакомый тип — ошибка, а не новое событие.
WMS_EVENT_TYPES = frozenset({
    "wms.reservation.succeeded.v1",
    "wms.reservation.failed.v1",
    "wms.picking.started.v1",
    "wms.item.scanned.v1",
    "wms.picking.completed.v1",
    "wms.packing.completed.v1",
    "wms.label.attached.v1",
    "wms.returned.to.shelf.v1",
    "wms.return.expected.v1",
    "wms.return.received.v1",
    "wms.return.resellable.v1",
    "wms.return.defective.v1",
    "wms.order.cancelled.v1",
    # Новое в версии 1.2: сборка без остатка перестаёт быть молчаливой (раздел 6.5).
    "wms.stock.shortfall.v1",
    # Новое в версии 1.3: приёмку было нечем ни эмитить (инвариант 13 требует
    # событие на любое физическое движение), ни тарифицировать (раздел 3.4
    # числит её среди услуг с тарифом).
    "wms.receipt.completed.v1",
})

INVENTORY_EVENT_TYPES = frozenset({
    "inventory.movement.recorded.v1",
    "inventory.stock.updated.v1",
})

# События Wildberries, которые издаёт wms после поглощения шлюза (раздел 6.3).
# Их нет в списке WMS_EVENT_TYPES приложения E — тот перечисляет только
# события склада, — но каналы для них описаны в самом asyncapi.yaml, и
# `wb.supply.shipped.v1` тарифицируется потоком C (раздел 3.4). Без этого
# набора публикация такого события упиралась бы в собственную же валидацию.
WB_EVENT_TYPES = frozenset({
    "wb.fbs.order.synced.v1",
    "wb.fbs.order.updated.v1",
    "wb.supply.shipped.v1",
    "wb.fbs.return.detected.v1",
})


class EventTypeNotAllowed(ValueError):
    """Попытка отправить событие, которого нет в каталоге."""


@dataclass
class EventEnvelope:
    """Конверт события (раздел 2.4, приложение E).

    additionalProperties: false в контракте — здесь ровно эти шесть полей и
    ни одного лишнего. Токены и любые секреты внутрь не попадают никогда
    (инвариант 15): за этим следит валидация в events.py.
    """

    type: str
    tenant_id: str
    payload: dict[str, Any]
    correlation_id: str
    event_id: str = field(default_factory=uid)
    occurred_at: str = field(default_factory=now)

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "tenant_id": self.tenant_id,
            "type": self.type,
            "occurred_at": self.occurred_at,
            "payload": self.payload,
            "correlation_id": self.correlation_id,
        }


def validate_event_type(event_type: str) -> str:
    if event_type not in WMS_EVENT_TYPES | INVENTORY_EVENT_TYPES | WB_EVENT_TYPES:
        raise EventTypeNotAllowed(f"событие {event_type!r} отсутствует в каталоге")
    return event_type
