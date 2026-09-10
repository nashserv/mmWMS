import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families

from app import api
from app.accounts import MemoryAccountRepository
from app.fakes import FakeWB, FakeWBError, FakeWMS
from app.secrets import MemorySecretStore
from app.metrics import _operation_outcome
from app.service import Gateway, GatewayError
from app.workflow import MemoryWorkflowRepository
from test_connection_lifecycle import synthetic_token


def metric_value(payload: str, name: str, **labels: str) -> float:
    for family in text_string_to_metric_families(payload):
        for sample in family.samples:
            if sample.name == name and all(sample.labels.get(key) == value for key, value in labels.items()):
                return float(sample.value)
    raise AssertionError(f"metric not found: {name} {labels}")


class GatewayMetricsTest(unittest.TestCase):
    def setUp(self):
        self.original_gateway = api.gateway
        api._requests.clear()
        self.wb = FakeWB()
        self.workflow = MemoryWorkflowRepository()
        api.gateway = Gateway(
            self.wb,
            FakeWMS(),
            MemorySecretStore(),
            MemoryAccountRepository(),
            self.workflow,
        )
        self.client = TestClient(api.app)
        self.token = synthetic_token()

    def tearDown(self):
        api.gateway = self.original_gateway

    def connect(self):
        response = self.client.post(
            "/sellers/seller-a/wb-accounts",
            json={"name": "Основной", "token": self.token},
            headers={"Idempotency-Key": "metrics-connect-0001"},
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def test_metrics_are_public_to_internal_scraper_and_never_expose_credentials(self):
        environment = {
            "APP_ENV": "production",
            "WB_ADAPTER": "live",
            "SECRET_PROVIDER": "vault",
            "ACCOUNT_STORAGE_BACKEND": "postgres",
        }
        with patch.dict(os.environ, environment, clear=False):
            os.environ.pop("INTERNAL_SERVICE_TOKEN", None)
            response = self.client.get("/metrics")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/plain", response.headers["content-type"])
        self.assertEqual(metric_value(response.text, "mmx_service_process_up", service="wb_gateway"), 1)
        self.assertNotIn(self.token, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_metrics_reflect_real_account_sync_outbox_and_rate_limit_state(self):
        account = self.connect()
        deadline = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        self.wb.add_order(account["id"], orderId=501, sku="SKU-501", deadline=deadline)
        synced = self.client.post(f"/sellers/seller-a/wb-accounts/{account['id']}/sync")
        self.assertEqual(synced.status_code, 200, synced.text)

        self.wb.inject("verify", FakeWBError(429, f"must not leak {self.token}", 7))
        limited = self.client.post(f"/sellers/seller-a/wb-accounts/{account['id']}/verify")
        self.assertEqual(limited.status_code, 429)

        payload = self.client.get("/metrics").text
        self.assertEqual(metric_value(payload, "mmx_wb_accounts", status="rate_limited"), 1)
        # Initial projection plus both durable reservation transitions.
        self.assertEqual(metric_value(payload, "mmx_wb_outbox_pending", kind="integration_event"), 3)
        self.assertGreaterEqual(
            metric_value(payload, "mmx_wb_operations_total", operation="sync", outcome="success"),
            1,
        )
        self.assertGreaterEqual(
            metric_value(payload, "mmx_wb_operations_total", operation="verify", outcome="rate_limited"),
            1,
        )
        self.assertNotIn(self.token, payload)
        self.assertNotIn(account["id"], payload)
        self.assertNotIn("seller-a", payload)

    def test_wb_metrics_use_only_bounded_technical_labels(self):
        self.connect()
        payload = self.client.get("/metrics").text
        allowed = {"operation", "outcome", "status", "state", "kind", "le"}
        observed = 0
        for family in text_string_to_metric_families(payload):
            for sample in family.samples:
                if sample.name.startswith("mmx_wb_"):
                    observed += 1
                    self.assertTrue(set(sample.labels).issubset(allowed), sample)
        self.assertGreater(observed, 0)

    def test_malformed_credential_is_classified_as_auth_error_without_echo(self):
        rejected = self.client.post(
            "/sellers/seller-a/wb-accounts",
            json={"name": "Rejected", "token": "malformed-value"},
            headers={"Idempotency-Key": "metrics-rejected-0001"},
        )
        self.assertEqual(rejected.status_code, 422)
        self.assertNotIn("malformed-value", rejected.text)
        payload = self.client.get("/metrics").text
        self.assertGreaterEqual(
            metric_value(payload, "mmx_wb_operations_total", operation="connect", outcome="auth_error"),
            1,
        )

    def test_all_server_credential_failures_map_to_auth_alert_bucket(self):
        for code in (
            "WB_CLIENT_SECRET_INVALID",
            "WB_CLIENT_SECRET_EXPIRED",
            "WB_SERVICE_TOKEN_TARGET_INVALID",
            "WB_SERVICE_TOKEN_TARGET_MISMATCH",
            "WB_PAYMENT_REQUIRED",
        ):
            self.assertEqual(_operation_outcome(GatewayError(422, code, "safe")), "auth_error")


if __name__ == "__main__":
    unittest.main()
