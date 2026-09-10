"""Метрики админки. Отдельным модулем: регистр Prometheus глобален."""
from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

SERVICE = "internal-admin"

HTTP_REQUESTS = Counter("mmx_http_requests_total",
                        "HTTP requests completed by MM Express services.", ("service", "status"))
HTTP_DURATION = Histogram("mmx_http_request_duration_seconds", "HTTP request duration in seconds.",
                          ("service",),
                          buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30))
SERVICE_READY = Gauge("mmx_service_ready", "Dependency-aware service readiness.", ("service",))

# Действия администратора по видам и исходу. Отказ — такая же метрика, как
# успех: по всплеску отказов видно, что процесс сломался, а не что людям лень.
ACTIONS = Counter("mmx_admin_actions_total", "Admin actions, by kind and outcome.",
                  ("action", "outcome"))
