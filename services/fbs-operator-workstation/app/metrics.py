"""Метрики рабочего места.

Мониторинг боевого контура следил за портами, а не за работой: четыре воркера
стояли Up с зелёным healthcheck и нулём строк лога (раздел 3.5 мастера).
Поэтому здесь нет ни одной метрики вида «процесс жив» без пары к ней —
«и вот сколько он сделал за минуту».

Лейблы ограниченные и несекретные: ни владелец, ни штрихкод, ни номер заказа
в лейбл не попадают. Это неограниченная кардинальность и утечка данных клиента
в общий дашборд.
"""
from __future__ import annotations

import time
from typing import Any

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

SERVICE = "fbs_operator_workstation"

# --- общее для платформы ---------------------------------------------------

PICKER_IDENTIFIED = Counter(
    "mmx_workstation_picker_identified_total",
    "Как опознан сборщик, начавший обход: по токену или по набранному имени. "
    "Доля «имени» — это доля смен, про которые нельзя ответить, кто их отработал.",
    ["how"])

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

# --- полный путь задания ---------------------------------------------------
#
# Три числа подряд: сколько заданий ждёт в wms, сколько забрано опросом,
# сколько показано на экране. Расхождение между соседними — алерт: именно в
# этих зазорах и терялись «новые заказы, которые иногда не падают».

TASKS_AVAILABLE_IN_WMS = Gauge(
    "mmx_workstation_tasks_available_in_wms",
    "Tasks waiting for picking as reported by wms in the last poll.",
)
TASKS_PULLED = Gauge(
    "mmx_workstation_tasks_pulled",
    "Tasks the poller received in the last poll.",
)
TASKS_ON_SCREEN = Gauge(
    "mmx_workstation_tasks_on_screen",
    "Tasks in the local projection, by screen status.",
    ("screen_status",),
)
TASKS_RETIRED = Counter(
    "mmx_workstation_tasks_retired_total",
    "Tasks removed from the screen after a by-name check confirmed they are done.",
)
TASKS_DIVERGED = Gauge(
    "mmx_workstation_tasks_diverged",
    "Tasks whose wms state diverged from Wildberries (invariant 10).",
)

# --- опрос -----------------------------------------------------------------

POLLS = Counter(
    "mmx_workstation_polls_total",
    "Polls of /tasks/pull by outcome.",
    ("outcome",),
)
POLL_DURATION = Histogram(
    "mmx_workstation_poll_duration_seconds",
    "Duration of one /tasks/pull call.",
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)
# Задержка «задание доступно в wms → задание в проекции экрана». Критерий
# раздела 10: WB → экран меньше 5 секунд p99.
TASK_VISIBLE_DELAY = Histogram(
    "mmx_workstation_task_visible_delay_seconds",
    "Delay between a task becoming available in wms and appearing on screen.",
    buckets=(0.1, 0.25, 0.5, 1, 2, 3, 5, 10, 30, 60),
)

# --- воркеры (инвариант 14) ------------------------------------------------

WORKER_PROCESSED = Counter(
    "mmx_workstation_worker_processed_total",
    "Units processed by a workstation worker. Zero for 15 minutes is an alert.",
    ("worker",),
)
WORKER_LAST_PROCESSED = Gauge(
    "mmx_workstation_worker_last_processed_unixtime",
    "When the worker last processed anything at all.",
    ("worker",),
)
WORKER_UP = Gauge(
    "mmx_workstation_worker_up",
    "Worker loop is running. Meaningless alone — pair it with processed_total.",
    ("worker",),
)
WORKER_ERRORS = Counter(
    "mmx_workstation_worker_errors_total",
    "Worker iterations that ended in an error.",
    ("worker",),
)

# --- шина как ускоритель, а не источник истины ------------------------------

BUS_NUDGES = Counter(
    "mmx_workstation_bus_nudges_total",
    "Bus events that triggered an early poll. The bus never writes the projection.",
    ("outcome",),
)
BUS_CONNECTED = Gauge(
    "mmx_workstation_bus_connected",
    "Inbox consumer holds a broker connection. Zero is legal: polling carries the shift.",
)

# --- подбор и контрольный скан ---------------------------------------------

SCANS = Counter(
    "mmx_workstation_scans_total",
    "Scans by result. wrong_barcode at packing is the last line of defence.",
    ("stage", "scan_result"),
)
PICK_SESSIONS = Gauge(
    "mmx_workstation_pick_sessions_open",
    "Open picking sessions. Five pickers means five, not one per 6497 tasks.",
)

# --- печать ----------------------------------------------------------------
#
# Критерий раздела 10: от нажатия «печать» до движения головки меньше 300 мс,
# из них до записи в устройство — меньше 50 мс.

PRINT_CLICK_TO_AGENT = Histogram(
    "mmx_workstation_print_click_to_agent_seconds",
    "From the operator's click to handing bytes to the station agent.",
    buckets=(0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.2, 0.3, 0.5, 1, 2, 5),
)
# Имя честное: это время до СПУЛЕРА, а не до бумаги. `WritePrinter`
# возвращается, когда очередь приняла байты; головка двинется позже, а у
# выключенного принтера не двинется вовсе. Называть это «записью в устройство»
# значит мерить не то, что обещано, и получать зелёную метрику при пустом
# лотке.
PRINT_AGENT_WRITE = Histogram(
    "mmx_workstation_print_spooler_write_seconds",
    "Agent-reported time of the RAW write into the print spooler (not the paper).",
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.3, 1),
)
PRINTS = Counter(
    "mmx_workstation_prints_total",
    "Print attempts by outcome and label format.",
    ("outcome", "label_format"),
)
AGENTS_CONNECTED = Gauge(
    "mmx_workstation_print_agents_connected",
    "Station agents holding a live push connection. Polling agents cost a second of latency.",
)

# --- расхождения контракта --------------------------------------------------

CONTRACT_FALLBACKS = Counter(
    "mmx_workstation_contract_fallbacks_total",
    "Responses that did not match the frozen contract and were read by fallback shape.",
    ("route", "field"),
)
WMS_CALLS = Counter(
    "mmx_workstation_wms_calls_total",
    "Calls to wms by route and outcome.",
    ("route", "outcome"),
)
WMS_CALL_DURATION = Histogram(
    "mmx_workstation_wms_call_duration_seconds",
    "Duration of one call to wms.",
    ("route",),
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)


def worker_beat(worker: str, units: int = 1) -> None:
    """Отметка «воркер сделал работу».

    Вызывается даже при нуле обработанных заданий — но тогда с units=0:
    важно различать «воркер жив и очередь пуста» и «воркер молчит». Первое
    нормально, второе — инвариант 14.
    """
    WORKER_UP.labels(worker=worker).set(1)
    if units > 0:
        WORKER_PROCESSED.labels(worker=worker).inc(units)
    WORKER_LAST_PROCESSED.labels(worker=worker).set(time.time())


def worker_stopped(worker: str) -> None:
    WORKER_UP.labels(worker=worker).set(0)


def observe_http(status_code: int, elapsed_seconds: float) -> None:
    HTTP_REQUESTS.labels(service=SERVICE, status=str(status_code)).inc()
    HTTP_DURATION.labels(service=SERVICE).observe(max(0.0, elapsed_seconds))


def refresh_runtime_metrics(app_state: Any) -> None:
    """Обновляет калибры перед отдачей /metrics."""
    SERVICE_PROCESS_UP.labels(service=SERVICE).set(1)
    try:
        SERVICE_READY.labels(service=SERVICE).set(1 if app_state.ready() else 0)
    except Exception:  # noqa: BLE001 — неготовность не должна ронять сбор метрик
        SERVICE_READY.labels(service=SERVICE).set(0)


def prometheus_payload(app_state: Any) -> tuple[bytes, str]:
    refresh_runtime_metrics(app_state)
    return generate_latest(), CONTENT_TYPE_LATEST
