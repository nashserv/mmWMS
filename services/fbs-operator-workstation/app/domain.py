"""Домен рабочего места.

Здесь живёт только то, что рабочее место знает само: как состояние задания в
wms превращается в статус на экране, что такое строка листа подбора и чем
отличается отклонённый скан от принятого.

Таблица соответствия состояний — не местное изобретение, а копия
`docs/state-mapping.md`, замороженного потоком 0. Расхождение с ним чинится
там, а не здесь (правило 9.5.2 мастера).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, UTC
from enum import Enum
from typing import Any


def now() -> str:
    return datetime.now(UTC).isoformat()


# Пространство имён для идентификатора исполнителя. То же самое, что в
# `services/wms/app/tasks.py`: имя рабочего места («Иванов», «picker-1»)
# разворачивается в один и тот же uuid по обе стороны, и «за кем задание»
# остаётся воспроизводимым от смены к смене.
#
# Контракт `wms` описывает `assignee` как uuid (версия 1.3.0), а на экране
# сборщик по-прежнему набирает себя руками: пользователей и ролей у рабочего
# места пока нет. Пока их нет, uuid выводится из имени — это допущение
# интегратора, записанное в разделе 13 мастера.
ASSIGNEE_NAMESPACE = uuid.UUID("2f1c9a44-7b8e-4d5c-9a3f-1e6b0d8c5a72")


def assignee_id(value: str | None) -> str | None:
    """Имя исполнителя → uuid. Настоящий uuid проходит как есть."""
    text = (value or "").strip()
    if not text:
        return None
    try:
        return str(uuid.UUID(text))
    except (ValueError, AttributeError):
        return str(uuid.uuid5(ASSIGNEE_NAMESPACE, text))


def uid() -> str:
    return str(uuid.uuid4())


class TaskState(str, Enum):
    """Состояние задания в wms (автомат раздела 7, enum TaskState контракта)."""

    NEW = "new"
    RESERVED = "reserved"
    PICKING = "picking"
    PICKED = "picked"
    PACKED = "packed"
    LABELED = "labeled"
    IN_SUPPLY = "in_supply"
    SHIPPED = "shipped"
    HANDED = "handed"
    ACCEPTED = "accepted"
    SHORT = "short"
    CANCELLED = "cancelled"
    MANUAL_REVIEW = "manual_review"
    DIVERGED = "diverged"


class ScreenStatus(str, Enum):
    """Статус рабочего места — то, что видит человек.

    Список тот же, что у боевого `workstation_task`: экраны, обучение и
    привычки операторов на него завязаны, и менять его заодно с переездом
    источника истины — значит менять две вещи разом.
    """

    QUEUED = "queued"
    PICKING = "picking"
    PICKED = "picked"
    PRINTED = "printed"
    PACKED = "packed"
    BOXED = "boxed"
    SHIPPED = "shipped"
    CLOSED = "closed"
    CANCELLED = "cancelled"


# docs/state-mapping.md, таблица 1. Пустых ячеек в ней нет — кроме `diverged`,
# у которого статус экрана «последний известный до расхождения», поэтому его
# здесь нет и он разбирается отдельно в screen_status().
STATE_TO_SCREEN: dict[TaskState, ScreenStatus] = {
    TaskState.NEW: ScreenStatus.QUEUED,
    TaskState.MANUAL_REVIEW: ScreenStatus.QUEUED,
    TaskState.SHORT: ScreenStatus.QUEUED,
    TaskState.RESERVED: ScreenStatus.QUEUED,
    TaskState.PICKING: ScreenStatus.PICKING,
    TaskState.PICKED: ScreenStatus.PICKED,
    TaskState.PACKED: ScreenStatus.PACKED,
    TaskState.LABELED: ScreenStatus.PRINTED,
    TaskState.IN_SUPPLY: ScreenStatus.BOXED,
    TaskState.SHIPPED: ScreenStatus.SHIPPED,
    TaskState.HANDED: ScreenStatus.CLOSED,
    TaskState.ACCEPTED: ScreenStatus.CLOSED,
    TaskState.CANCELLED: ScreenStatus.CANCELLED,
}

# Канонический статус — третья колонка той же таблицы. Нужен биллингу и порталу,
# рабочее место его только показывает и передаёт дальше.
STATE_TO_CANONICAL: dict[TaskState, str] = {
    TaskState.NEW: "RECEIVED",
    TaskState.MANUAL_REVIEW: "MANUAL_REVIEW",
    TaskState.SHORT: "RESERVATION_FAILED",
    TaskState.RESERVED: "RESERVED",
    TaskState.PICKING: "PICKING",
    TaskState.PICKED: "PICKED",
    TaskState.PACKED: "PACKED",
    TaskState.LABELED: "LABEL_READY",
    TaskState.IN_SUPPLY: "SUPPLY_ASSIGNED",
    TaskState.SHIPPED: "OUT_FOR_DELIVERY",
    TaskState.HANDED: "HANDED_TO_WB",
    TaskState.ACCEPTED: "ACCEPTED_BY_WB",
    TaskState.CANCELLED: "CANCELLED",
}

# Состояния, из которых задание уже не вернётся в работу. Считать «сколько
# ждёт подбора» по ним нельзя.
TERMINAL_STATES = frozenset({
    TaskState.HANDED, TaskState.ACCEPTED, TaskState.CANCELLED,
})

# Что сборщик может взять в работу прямо сейчас.
PICKABLE_STATES = frozenset({TaskState.RESERVED})


def parse_state(value: Any) -> TaskState | None:
    """Состояние из ответа wms. Неизвестное значение — не догадка, а None.

    Придумать состояние за сервис нельзя: экран покажет вымысел, а расхождение
    контрактов останется незамеченным.
    """
    try:
        return TaskState(str(value))
    except ValueError:
        return None


def screen_status(state: TaskState | None, previous: ScreenStatus | None = None) -> ScreenStatus:
    """Статус экрана по состоянию задания.

    `diverged` — «последний известный до расхождения» (docs/state-mapping.md):
    наш статус разошёлся со статусом WB, и подменять его выдуманным нельзя.
    Пока прошлое неизвестно — задание показывается в очереди, но признак
    расхождения (инвариант 10) живёт отдельным полем и поднимает алерт.
    """
    if state is None:
        return previous or ScreenStatus.QUEUED
    if state is TaskState.DIVERGED:
        return previous or ScreenStatus.QUEUED
    return STATE_TO_SCREEN.get(state, ScreenStatus.QUEUED)


def canonical_status(state: TaskState | None, previous: str | None = None) -> str | None:
    if state is None:
        return previous
    if state is TaskState.DIVERGED:
        return previous
    return STATE_TO_CANONICAL.get(state)


class ScanResult(str, Enum):
    """Разбор скана — `pick_line.scan_result` миграции 006.

    Отделён от «принято / не принято» намеренно: экран обязан показать
    сборщику, ЧТО именно не так. «Не тот штрихкод» и «товар чужого владельца» —
    разные ошибки с разными действиями.
    """

    OK = "ok"
    WRONG_BARCODE = "wrong_barcode"
    WRONG_OWNER = "wrong_owner"
    NOT_FOUND = "not_found"
    SHORT = "short"


# Причины отмены, которые ставит само рабочее место. Инвариант 11: причина
# обязательна на уровне схемы, и в боевом контуре все 2645 отмен были без неё.
# Список закрыт: свободный текст в этом поле снова превратит его в NULL по сути.
class CancelReason(str, Enum):
    OPERATOR_SHORT = "operator_short"                # не нашли товар на полке
    OPERATOR_DAMAGED = "operator_damaged"            # товар повреждён
    OPERATOR_WRONG_ITEM = "operator_wrong_item"      # на полке лежит не то
    WB_CANCELLED = "wb_cancelled"                    # отменил Wildberries
    SUPERVISOR_DECISION = "supervisor_decision"      # решение начальника смены


@dataclass(slots=True)
class Placement:
    """Где лежит товар. Строка `StockPlacement` контракта.

    Адрес и коробка — не украшение: сборщик должен видеть, куда идти, а не
    искать глазами (раздел «Лист подбора» файла 03).
    """

    barcode: str
    cell_address: str | None = None
    box_barcode: str | None = None
    state: str = "good"
    quantity: int = 0
    route_order: int | None = None

    @classmethod
    def from_contract(cls, row: dict[str, Any]) -> Placement:
        # `cell` вместо `cell_address` — форма заглушки потока 0. Читаем оба:
        # строка листа без адреса отправляет сборщика искать вещь глазами.
        return cls(
            barcode=str(row.get("barcode") or ""),
            cell_address=_text_or_none(row.get("cell_address") or row.get("cell")),
            box_barcode=_text_or_none(row.get("box_barcode")),
            state=str(row.get("state") or "good"),
            quantity=_int_or(row.get("quantity"), 0),
            route_order=_int_or_none(row.get("route_order")),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "barcode": self.barcode,
            "cell_address": self.cell_address,
            "box_barcode": self.box_barcode,
            "state": self.state,
            "quantity": self.quantity,
            "route_order": self.route_order,
        }


@dataclass(slots=True)
class Task:
    """Проекция задания на экране.

    Это копия того, что отдал wms, а не самостоятельная запись. Единственный,
    кто её меняет, — опросчик: пока писателей двое, они однажды разойдутся, и
    экран покажет то, чего в wms нет.
    """

    task_id: str
    owner_external_id: str
    barcode: str
    quantity: int
    state: TaskState | None
    raw_state: str
    screen: ScreenStatus
    canonical: str | None = None
    wb_order_id: str | None = None
    seller_sku: str | None = None
    name: str | None = None
    deadline: str | None = None
    wb_status: str | None = None
    assignee: str | None = None
    claim_expires_at: str | None = None
    label_ready: bool = False
    label_fallback: bool = False
    label_format: str | None = None
    label_invalidated: bool = False
    cancel_reason: str | None = None
    manual_review_code: str | None = None
    diverged: bool = False
    placements: list[Placement] = field(default_factory=list)
    # Когда задание завёл wms. Не подставляется «сейчас», если сервис его не
    # прислал: по такому полю задержка «задание в wms → задание на экране»
    # всегда выходила бы нулевой, то есть метрика показывала бы идеал ровно
    # там, где мерить нечем.
    created_at: str | None = None
    updated_at: str | None = None

    @property
    def route_order(self) -> tuple[int, str]:
        """Ключ сортировки листа подбора — маршрут обхода, а не порядок заказов.

        Сборщик идёт змейкой по стеллажам (раздел «Лист подбора» файла 03).
        Строка без адреса уходит в конец: искать её всё равно придётся глазами,
        и обход из-за неё ломать не нужно.
        """
        orders = [p.route_order for p in self.placements if p.route_order is not None]
        return (min(orders) if orders else 1_000_000_000, self.cell_address or "~")

    @property
    def cell_address(self) -> str | None:
        for placement in self.placements:
            if placement.cell_address:
                return placement.cell_address
        return None

    @property
    def box_barcode(self) -> str | None:
        for placement in self.placements:
            if placement.box_barcode:
                return placement.box_barcode
        return None

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "owner_external_id": self.owner_external_id,
            "barcode": self.barcode,
            "seller_sku": self.seller_sku,
            "name": self.name,
            "quantity": self.quantity,
            "state": self.raw_state,
            "screen_status": self.screen.value,
            "canonical_status": self.canonical,
            "wb_order_id": self.wb_order_id,
            "wb_status": self.wb_status,
            "deadline": self.deadline,
            "assignee": self.assignee,
            "claim_expires_at": self.claim_expires_at,
            "label_ready": self.label_ready,
            "label_format": self.label_format,
            "label_invalidated": self.label_invalidated,
            "cancel_reason": self.cancel_reason,
            "manual_review_code": self.manual_review_code,
            "diverged": self.diverged,
            "cell_address": self.cell_address,
            "box_barcode": self.box_barcode,
            "placements": [placement.as_dict() for placement in self.placements],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def _text_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _int_or(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def task_from_contract(projection: dict[str, Any], *,
                       placements: list[dict[str, Any]] | None = None,
                       previous: Task | None = None) -> Task:
    """Задание контракта → задание экрана.

    Читается по схеме `TaskProjection`. Поля, которых нет, не выдумываются:
    отсутствующий адрес честно показывается пустым, потому что сборщику
    выдуманная ячейка хуже пустой.
    """
    state = parse_state(projection.get("state"))
    label = projection.get("label") or {}
    if not isinstance(label, dict):
        label = {}
    if not label and projection.get("label_id"):
        # Контракт кладёт сводку по стикеру в блок `label`; заглушка потока 0
        # отдаёт только `label_id`. Наличие идентификатора — это всё же
        # «стикер выписан», и прятать это от упаковщика хуже, чем прочитать
        # запасным путём. Счётчик расхождения ставит вызывающий код.
        label = {"ready": True, "format": None, "fallback": True}
    rows = [Placement.from_contract(row) for row in (placements or []) if isinstance(row, dict)]
    # Маршрут обхода — по нему сортируется лист (раздел «Лист подбора»).
    rows.sort(key=lambda p: (p.route_order is None, p.route_order or 0, p.cell_address or "~"))
    return Task(
        task_id=str(projection.get("task_id") or ""),
        owner_external_id=str(projection.get("owner_external_id") or ""),
        barcode=str(projection.get("barcode") or ""),
        quantity=_int_or(projection.get("quantity"), 1),
        state=state,
        raw_state=str(projection.get("state") or ""),
        screen=screen_status(state, previous.screen if previous else None),
        canonical=canonical_status(state, previous.canonical if previous else None),
        wb_order_id=_text_or_none(projection.get("wb_order_id")),
        seller_sku=_text_or_none(projection.get("seller_sku")),
        name=_text_or_none(projection.get("name")),
        deadline=_text_or_none(projection.get("deadline")),
        wb_status=_text_or_none(projection.get("wb_status")),
        assignee=_text_or_none(projection.get("assignee")),
        claim_expires_at=_text_or_none(projection.get("claim_expires_at")),
        label_ready=bool(label.get("ready")) and not label.get("invalidated_at"),
        label_fallback=bool(label.get("fallback")),
        label_format=_text_or_none(label.get("format")),
        label_invalidated=bool(label.get("invalidated_at")),
        cancel_reason=_text_or_none(projection.get("cancel_reason")),
        manual_review_code=_text_or_none(projection.get("manual_review_code")),
        diverged=state is TaskState.DIVERGED,
        placements=rows,
        created_at=_text_or_none(projection.get("created_at")),
        updated_at=_text_or_none(projection.get("updated_at")),
    )
