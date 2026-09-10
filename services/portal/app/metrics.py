"""Метрики ЛК клиента. Отдельным модулем: регистр Prometheus глобален."""
from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

SERVICE = "portal"

HTTP_REQUESTS = Counter("mmx_http_requests_total",
                        "HTTP requests completed by MM Express services.", ("service", "status"))
HTTP_DURATION = Histogram("mmx_http_request_duration_seconds", "HTTP request duration in seconds.",
                          ("service",),
                          buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30))
SERVICE_READY = Gauge("mmx_service_ready", "Dependency-aware service readiness.", ("service",))

# Выгрузки клиента: акт за период он забирает сам, без участия бухгалтера.
EXPORTS = Counter("mmx_portal_exports_total", "Documents exported by clients.", ("kind",))
