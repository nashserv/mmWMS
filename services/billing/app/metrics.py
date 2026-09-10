"""Метрики Prometheus сервиса billing.

Лейблы ограниченные и несекретные, как в шаблоне платформы: ни кабинет, ни
партнёр, ни номер задания в лейблы не попадают — это неограниченная
кардинальность и данные клиента в общем дашборде.

Главное здесь — две метрики, которых на проде нет и из-за отсутствия которых
никто не знал, что тарифицируется два процента работы склада:
mmx_billing_unbilled_total (что не доехало до счёта) и
mmx_billing_worker_processed_total (инвариант 14: молчащий воркер сломан).
"""
from __future__ import annotations

from typing import Any

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

SERVICE = "billing"

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

ACCRUALS = Counter(
    "mmx_billing_accruals_total",
    "Accruals created, by service.",
    ("service",),
)
ACCRUED_AMOUNT = Counter(
    "mmx_billing_accrued_rubles_total",
    "Rubles charged to clients, by service.",
    ("service",),
)

# Разница между «склад отработал» и «клиенту выставлено», поимённо по причине.
UNBILLED = Counter(
    "mmx_billing_unbilled_total",
    "Billable events that did not become an accrual, by reason.",
    ("reason",),
)

# Инвариант 14: молчащий воркер считается сломанным. Ноль по этой метрике —
# повод для алерта, а не для спокойствия.
WORKER_PROCESSED = Counter(
    "mmx_billing_worker_processed_total",
    "Units processed by billing workers.",
    ("worker",),
)

# Кабинет, доехавший до биллинга событием, а не онбордингом (файл 04:
# «21 кабинет WB заведены при 4 записях в sellers»).
CABINETS_NEEDING_ONBOARDING = Counter(
    "mmx_billing_cabinets_auto_onboarded_total",
    "Cabinets created from an event instead of the onboarding flow.",
)

SHIFT_OUTPUT_REJECTED = Counter(
    "mmx_billing_shift_output_rejected_total",
    "Physical-action events whose shift output could not be recorded.",
    ("event_type",),
)

CONSUMER_LAG = Gauge(
    "mmx_billing_inbox_pending",
    "Events received but not yet processed.",
)


def observe_http(status_code: int, elapsed_seconds: float) -> None:
    HTTP_REQUESTS.labels(service=SERVICE, status=str(status_code)).inc()
    HTTP_DURATION.labels(service=SERVICE).observe(max(0.0, elapsed_seconds))


def refresh_runtime_metrics(database: Any) -> None:
    SERVICE_PROCESS_UP.labels(service=SERVICE).set(1)
    try:
        with database.cursor() as cursor:
            cursor.execute("SELECT count(*) AS pending FROM billing_inbox "
                           "WHERE processed_at IS NULL")
            CONSUMER_LAG.set(int(cursor.fetchone()["pending"]))
        SERVICE_READY.labels(service=SERVICE).set(1)
    except Exception:
        SERVICE_READY.labels(service=SERVICE).set(0)


def prometheus_payload(database: Any) -> tuple[bytes, str]:
    refresh_runtime_metrics(database)
    return generate_latest(), CONTENT_TYPE_LATEST
