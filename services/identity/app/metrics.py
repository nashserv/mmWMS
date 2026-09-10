"""Метрики роле́вой части identity.

Отдельным модулем, как у остальных сервисов: регистр Prometheus глобален, и
объявление счётчика внутри модуля приложения ломает любой повторный импорт —
в том числе в тестах.
"""
from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

SERVICE = "identity"

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
SERVICE_READY = Gauge("mmx_service_ready", "Dependency-aware service readiness.", ("service",))

# Отказ в проверке токена — событие безопасности, а не строка лога.
INTROSPECT_REFUSED = Counter(
    "mmx_identity_introspect_refused_total",
    "Token introspections refused, by reason.",
    ("reason",),
)
