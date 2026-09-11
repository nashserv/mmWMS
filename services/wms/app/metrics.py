"""Метрики Prometheus сервиса wms.

Лейблы ограниченные и несекретные — как в шаблоне wb-fbs-gateway. Ни владелец,
ни штрихкод, ни номер заказа в лейблы не попадают: это неограниченная
кардинальность и утечка данных клиента в общий дашборд.

Набор метрик закрывает критерии раздела 10 мастера.
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Any
from collections.abc import Iterator

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

from .domain import TaskState

SERVICE = "wms"

HTTP_REQUESTS = Counter(
    "mmx_http_requests_total",
    "HTTP requests completed by MM Express services.",
    ("service", "status"),
)
HTTP_DURATION = Histogram(
    "mmx_http_request_duration_seconds",
    "HTTP request duration in seconds.",
    ("service",),
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
)
SERVICE_PROCESS_UP = Gauge("mmx_service_process_up", "Process availability.", ("service",))
SERVICE_READY = Gauge("mmx_service_ready", "Dependency-aware service readiness.", ("service",))

WMS_TASKS = Gauge("mmx_wms_tasks", "Tasks by state.", ("state",))

# Критерий раздела 10: нажатие «печать» → движение головки < 300 мс.
# Корзины подобраны вокруг цели, иначе по гистограмме не увидеть промах.
LABEL_PRINT_DURATION = Histogram(
    "mmx_wms_label_print_duration_seconds",
    "Time from print request to handing bytes to the printer agent.",
    buckets=(0.01, 0.025, 0.05, 0.1, 0.2, 0.3, 0.5, 1, 2, 5),
)
LABELS_READY = Gauge("mmx_wms_labels_ready", "Labels fetched and waiting locally.")

# Инвариант 12: сборка без остатка обязана быть счётной.
STOCK_SHORTFALL = Counter(
    "mmx_wms_stock_shortfall_total",
    "Reservations created against stock the ledger does not have.",
)

# Инвариант 4: удержание блокировки под 100 мс, и это измеряется.
LOCK_HOLD_DURATION = Histogram(
    "mmx_wms_lock_hold_seconds",
    "Row lock hold time inside the reservation transaction.",
    ("operation",),
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1),
)

# Сколько единиц работы сделал воркер. Это ДЕЛОВАЯ метрика: ноль здесь
# законен — ночью заказов нет, и опросчику нечего заводить.
WORKER_PROCESSED = Counter(
    "mmx_wms_worker_processed_total",
    "Units processed by background workers.",
    ("worker",),
)

# Инвариант 14: молчащий воркер считается сломанным.
#
# Пульс, а не работа. Алерт на `processed_total == 0` срабатывал бы каждую
# тихую ночь: воркер жив, очередь пуста, обработано ноль — и дежурный
# приходит к исправному складу. Пару таких вызовов, и уведомления выключают,
# после чего молчит уже всё.
#
# Такт считается ВСЕГДА, даже когда делать было нечего. Ноль тактов значит
# ровно одно: воркер не крутится. Именно это и случилось в боевом контуре —
# четыре воркера стояли Up с зелёным healthcheck и нулём строк лога.
WORKER_TICKS = Counter(
    "mmx_wms_worker_ticks_total",
    "Loop iterations of background workers, whether or not there was work.",
    ("worker",),
)

# Инвариант 10: расхождение с WB — состояние и алерт.
TASKS_DIVERGED = Gauge("mmx_wms_tasks_diverged", "Tasks whose state disagrees with WB.")


def observe_http(status_code: int, elapsed_seconds: float) -> None:
    HTTP_REQUESTS.labels(service=SERVICE, status=str(status_code)).inc()
    HTTP_DURATION.labels(service=SERVICE).observe(max(0.0, elapsed_seconds))


@contextmanager
def track_lock(operation: str) -> Iterator[None]:
    started = time.monotonic()
    try:
        yield
    finally:
        LOCK_HOLD_DURATION.labels(operation=operation).observe(time.monotonic() - started)


def refresh_runtime_metrics(state: Any) -> None:
    SERVICE_PROCESS_UP.labels(service=SERVICE).set(1)
    try:
        SERVICE_READY.labels(service=SERVICE).set(1 if state.ready() else 0)
        snapshot = state.snapshot()
    except Exception:
        SERVICE_READY.labels(service=SERVICE).set(0)
        return

    by_state = snapshot.get("tasks_by_state", {})
    for task_state in TaskState:
        WMS_TASKS.labels(state=task_state.value).set(by_state.get(task_state.value, 0))
    LABELS_READY.set(snapshot.get("labels_ready", 0))
    TASKS_DIVERGED.set(by_state.get(TaskState.DIVERGED.value, 0))


def prometheus_payload(state: Any) -> tuple[bytes, str]:
    refresh_runtime_metrics(state)
    return generate_latest(), CONTENT_TYPE_LATEST


# --- Опрос Wildberries -----------------------------------------------------
# Критерий раздела 10: задержка «WB → доступность в /tasks/pull» под 2 с, p99.
# Сегодня она не измеряется вовсе, и «иногда заказы не приходят» нечем ни
# подтвердить, ни опровергнуть.

WB_SYNC_LAST_SUCCESS = Gauge(
    "mmx_wb_sync_last_success_unixtime",
    "Most recent successful WB account synchronization timestamp.",
)
WB_SYNC_LAG = Histogram(
    "mmx_wms_wb_sync_duration_seconds",
    "Duration of one WB account synchronization cycle.",
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30),
)
WB_CALLS = Counter(
    "mmx_wms_wb_calls_total",
    "Calls to Wildberries by operation and outcome.",
    ("operation", "outcome"),
)
# Инвариант 13: событие обязано доехать до шины. На проде
# integration_outbox дорос до 136 210 записей без чистки (раздел 3.6), и
# заметить это было нечем.
OUTBOX_UNPUBLISHED = Gauge(
    "mmx_wms_outbox_unpublished",
    "Events waiting in the outbox to be published.",
)
# Кабинеты под паузой Wildberries. Кабинет, упёршийся в лимит надолго,
# перестаёт получать задания вовсе — и склад не знает, что не получает.
WB_RATE_LIMITED = Gauge(
    "mmx_wms_wb_rate_limited",
    "Cabinets currently paused by the Wildberries rate limit.",
)
# Инвариант 1: `stock_balance` — проекция журнала. Расхождение с последним
# пересчётом значит, что где-то пишут мимо журнала.
INVENTORY_DIVERGENCE = Gauge(
    "mmx_wms_inventory_divergence",
    "Positions where the balance disagrees with the latest inventory count.",
)

# Наши незакрытые задания, о которых Wildberries промолчал при сверке.
# Ненулевое значение — не «тихо», а «мы сверяем задания, которых у WB нет»:
# подменённый токен, чужой кабинет, удалённый заказ.
WB_ORDERS_MISSING = Gauge(
    "mmx_wms_wb_orders_missing",
    "Our open tasks that Wildberries did not return during reconciliation.",
    ("account",),
)
# Инвариант 7: публикация остатка уходит сразу после движения, без таймеров.
STOCK_PUSH = Counter(
    "mmx_wms_stock_push_total",
    "Stock publications sent to Wildberries.",
    ("outcome",),
)
STOCK_PUSH_DELAY = Histogram(
    "mmx_wms_stock_push_delay_seconds",
    "Delay between the stock movement and the publication call leaving.",
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5),
)
# Инвариант 9: стикер лежит локально до того, как человек нажал печать.
LABELS_FETCHED = Counter(
    "mmx_wms_labels_fetched_total",
    "Labels pre-fetched from Wildberries.",
    ("format", "outcome"),
)
