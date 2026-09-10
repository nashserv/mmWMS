"""Prometheus telemetry for the WB gateway with bounded, non-sensitive labels."""
from __future__ import annotations

import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

from .domain import Status


SERVICE = "wb_gateway"
ACCOUNT_STATUSES = ("active", "auth_error", "rate_limited", "disabled", "other")
OUTBOX_KINDS = ("integration_event", "wms_command")
WORKFLOW_STATES = ("reservation", "shipment")

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
WB_OPERATIONS = Counter(
    "mmx_wb_operations_total",
    "WB account lifecycle and synchronization operations.",
    ("operation", "outcome"),
)
WB_OPERATION_DURATION = Histogram(
    "mmx_wb_operation_duration_seconds",
    "WB account lifecycle and synchronization duration in seconds.",
    ("operation",),
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
)
SERVICE_PROCESS_UP = Gauge("mmx_service_process_up", "Process availability.", ("service",))
SERVICE_READY = Gauge("mmx_service_ready", "Dependency-aware service readiness.", ("service",))
WB_ACCOUNTS = Gauge("mmx_wb_accounts", "WB accounts by safe status bucket.", ("status",))
WB_ACCOUNTS_STALE = Gauge(
    "mmx_wb_accounts_stale",
    "Active or rate-limited WB accounts outside the configured synchronization freshness window.",
)
WB_SYNC_LAST_SUCCESS = Gauge(
    "mmx_wb_sync_last_success_unixtime",
    "Most recent successful WB account synchronization timestamp.",
)
WB_FBS_TASKS = Gauge("mmx_wb_fbs_tasks", "FBS tasks by canonical workflow state.", ("state",))
WB_FBS_ORDERS_OVERDUE = Gauge("mmx_fbs_orders_overdue", "Non-terminal FBS orders past deadline.")
WB_WORKFLOW_OLDEST = Gauge(
    "mmx_workflow_oldest_seconds",
    "Age of the oldest active FBS workflow in a bounded state group.",
    ("state",),
)
WB_OUTBOX_PENDING = Gauge("mmx_wb_outbox_pending", "Pending durable WB outbox records.", ("kind",))
WB_OUTBOX_OLDEST = Gauge(
    "mmx_wb_outbox_oldest_seconds",
    "Age of the oldest pending durable WB outbox record.",
    ("kind",),
)
METRICS_COLLECTION_ERRORS = Counter(
    "mmx_metrics_collection_errors_total",
    "Failures while collecting dependency-backed service metrics.",
    ("service",),
)


def observe_http(status_code: int, elapsed_seconds: float) -> None:
    HTTP_REQUESTS.labels(service=SERVICE, status=str(status_code)).inc()
    HTTP_DURATION.labels(service=SERVICE).observe(max(0.0, elapsed_seconds))


def _operation_outcome(error: BaseException) -> str:
    code = str(getattr(error, "code", ""))
    status = int(getattr(error, "status_code", 500))
    if code == "WB_RATE_LIMITED":
        return "rate_limited"
    if code in {
        "WB_AUTH_REJECTED",
        "WB_ACCESS_DENIED",
        "WB_CLIENT_SECRET_REQUIRED",
        "WB_CLIENT_SECRET_INVALID",
        "WB_CLIENT_SECRET_EXPIRED",
        "WB_CONTENT_SCOPE_REQUIRED",
        "WB_ANALYTICS_SCOPE_REQUIRED",
        "WB_MARKETPLACE_SCOPE_REQUIRED",
        "WB_PERSONAL_TOKEN_FORBIDDEN",
        "WB_SELLER_MISMATCH",
        "WB_TEST_TOKEN_FORBIDDEN",
        "WB_TOKEN_CLAIMS_INVALID",
        "WB_TOKEN_EXPIRED",
        "WB_TOKEN_MALFORMED",
        "WB_TOKEN_TYPE_UNSUPPORTED",
        "WB_WRITE_SCOPE_REQUIRED",
        "WB_REQUIRED_SCOPES_MISSING",
        "WB_SERVICE_TOKEN_TARGET_INVALID",
        "WB_SERVICE_TOKEN_TARGET_MISMATCH",
        "WB_PAYMENT_REQUIRED",
    }:
        return "auth_error"
    if status >= 500:
        return "service_error"
    return "rejected"


@contextmanager
def track_wb_operation(operation: str) -> Iterator[None]:
    """Record a fixed-name operation without retaining request or credential data."""
    started = time.monotonic()
    try:
        yield
    except Exception as error:
        WB_OPERATIONS.labels(operation=operation, outcome=_operation_outcome(error)).inc()
        raise
    else:
        WB_OPERATIONS.labels(operation=operation, outcome="success").inc()
    finally:
        WB_OPERATION_DURATION.labels(operation=operation).observe(time.monotonic() - started)


def _timestamp(value: Any) -> float | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _account_status(value: Any) -> str:
    return {
        "ACTIVE": "active",
        "AUTH_ERROR": "auth_error",
        "RATE_LIMITED": "rate_limited",
        "DISABLED": "disabled",
    }.get(str(value), "other")


def _set_workflow_metrics(tasks: list[Any], current_time: float) -> None:
    task_counts = {status.value: 0 for status in Status}
    overdue = 0
    oldest = {state: 0.0 for state in WORKFLOW_STATES}
    terminal = {Status.ACCEPTED_BY_WB, Status.RETURNED_TO_WMS, Status.CANCELLED}
    reservation = {
        Status.RECEIVED,
        Status.MANUAL_REVIEW,
        Status.RESERVATION_REQUESTED,
        Status.RESERVATION_FAILED,
    }
    shipment = {
        Status.RESERVED,
        Status.PICKING,
        Status.PICKED,
        Status.PACKING,
        Status.PACKED,
        Status.SUPPLY_ASSIGNED,
        Status.LABEL_PENDING,
        Status.LABEL_READY,
        Status.READY_FOR_DELIVERY,
        Status.IN_DELIVERY_BATCH,
        Status.OUT_FOR_DELIVERY,
        Status.HANDED_TO_WB,
        Status.DELIVERY_FAILED,
        Status.RETURN_REQUESTED,
    }
    for task in tasks:
        state = task.status if isinstance(task.status, Status) else Status(task.status)
        task_counts[state.value] += 1
        deadline = _timestamp(task.deadline)
        if state not in terminal and deadline is not None and deadline < current_time:
            overdue += 1
        updated = _timestamp(task.updated_at)
        age = max(0.0, current_time - updated) if updated is not None else 0.0
        if state in reservation:
            oldest["reservation"] = max(oldest["reservation"], age)
        elif state in shipment:
            oldest["shipment"] = max(oldest["shipment"], age)
    for state, count in task_counts.items():
        WB_FBS_TASKS.labels(state=state).set(count)
    WB_FBS_ORDERS_OVERDUE.set(overdue)
    for state in WORKFLOW_STATES:
        WB_WORKFLOW_OLDEST.labels(state=state).set(oldest[state])


def refresh_runtime_metrics(gateway: Any) -> None:
    """Refresh gauges from durable repositories; failures degrade readiness only."""
    SERVICE_PROCESS_UP.labels(service=SERVICE).set(1)
    try:
        is_ready = bool(gateway.ready())
    except Exception:
        is_ready = False
        METRICS_COLLECTION_ERRORS.labels(service=SERVICE).inc()
    SERVICE_READY.labels(service=SERVICE).set(1 if is_ready else 0)

    try:
        accounts = gateway.account_repository.all()
    except Exception:
        accounts = list(gateway.accounts.values())
        METRICS_COLLECTION_ERRORS.labels(service=SERVICE).inc()
    counts = {status: 0 for status in ACCOUNT_STATUSES}
    current_time = time.time()
    freshness_seconds = min(86_400, max(60, int(os.getenv("WB_SYNC_STALE_SECONDS", "900"))))
    stale = 0
    sync_timestamps: list[float] = []
    for account in accounts:
        bucket = _account_status(account.get("status"))
        counts[bucket] += 1
        synced_at = _timestamp(account.get("last_sync_at"))
        if synced_at is not None:
            sync_timestamps.append(synced_at)
        if bucket in {"active", "rate_limited"} and (
            synced_at is None or current_time - synced_at > freshness_seconds
        ):
            stale += 1
    for status in ACCOUNT_STATUSES:
        WB_ACCOUNTS.labels(status=status).set(counts[status])
    WB_ACCOUNTS_STALE.set(stale)
    WB_SYNC_LAST_SUCCESS.set(max(sync_timestamps, default=0.0))

    _set_workflow_metrics(list(gateway.tasks.values()), current_time)
    try:
        outboxes = gateway.workflow_repository.observability()
    except Exception:
        outboxes = {}
        METRICS_COLLECTION_ERRORS.labels(service=SERVICE).inc()
    for kind in OUTBOX_KINDS:
        values = outboxes.get(kind) or {}
        WB_OUTBOX_PENDING.labels(kind=kind).set(max(0, int(values.get("pending", 0))))
        WB_OUTBOX_OLDEST.labels(kind=kind).set(max(0.0, float(values.get("oldest_seconds", 0.0))))


def prometheus_payload(gateway: Any) -> tuple[bytes, str]:
    refresh_runtime_metrics(gateway)
    return generate_latest(), CONTENT_TYPE_LATEST
