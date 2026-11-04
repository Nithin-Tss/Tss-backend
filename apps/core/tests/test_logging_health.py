"""Request ids, access log, JSON log format and the /healthz and /readyz checks."""
import json
import logging
from unittest import mock

from django.test import SimpleTestCase, TestCase, override_settings

from apps.core.logging_utils import JsonFormatter, RequestIdFilter, request_id_var


class RequestLogTests(TestCase):
    def test_response_carries_a_generated_request_id(self):
        response = self.client.get("/healthz")
        self.assertEqual(len(response.headers["X-Request-ID"]), 32)

    def test_a_sane_incoming_request_id_is_kept(self):
        response = self.client.get("/healthz", HTTP_X_REQUEST_ID="proxy-abc.123")
        self.assertEqual(response.headers["X-Request-ID"], "proxy-abc.123")

    def test_a_bad_incoming_request_id_is_replaced(self):
        response = self.client.get("/healthz", HTTP_X_REQUEST_ID="bad id\twith spaces")
        self.assertNotIn(" ", response.headers["X-Request-ID"])
        self.assertEqual(len(response.headers["X-Request-ID"]), 32)

    def test_api_request_is_logged_without_its_query_string(self):
        with self.assertLogs("tss.access", level="INFO") as logs:
            self.client.get("/api/v1/auth/me/?token=secret")
        line = logs.records[0]
        self.assertEqual((line.method, line.status), ("GET", 401))
        self.assertNotIn("secret", line.getMessage())

    def test_non_api_requests_are_not_access_logged(self):
        with self.assertNoLogs("tss.access", level="INFO"):
            self.client.get("/healthz")

    @override_settings(LOG_SLOW_REQUEST_SECONDS=0)
    def test_slow_request_is_a_warning(self):
        with self.assertLogs("tss.access", level="INFO") as logs:
            self.client.get("/api/v1/auth/me/")
        self.assertEqual(logs.records[0].levelno, logging.WARNING)
        self.assertIn("SLOW", logs.records[0].getMessage())

    def test_request_id_is_available_while_the_request_runs_and_cleared_after(self):
        seen = []
        with mock.patch("apps.core.health._probe_cache", side_effect=lambda: seen.append(request_id_var.get())):
            self.client.get("/readyz", HTTP_X_REQUEST_ID="trace-1")
        self.assertEqual(seen, ["trace-1"])
        self.assertEqual(request_id_var.get(), "-")


class LogFormatTests(SimpleTestCase):
    def record(self, **extra):
        record = logging.LogRecord("tss.x", logging.WARNING, __file__, 1, "hello %s", ("world",), None)
        RequestIdFilter().filter(record)
        record.__dict__.update(extra)
        return record

    def test_json_line_has_the_fields(self):
        token = request_id_var.set("rid-1")
        try:
            entry = json.loads(JsonFormatter().format(self.record(method="GET", status=200)))
        finally:
            request_id_var.reset(token)
        self.assertEqual(
            (entry["message"], entry["level"], entry["requestId"], entry["method"], entry["status"]),
            ("hello world", "WARNING", "rid-1", "GET", 200),
        )

    def test_json_line_includes_the_exception(self):
        try:
            raise ValueError("boom")
        except ValueError:
            import sys
            record = self.record()
            record.exc_info = sys.exc_info()
        self.assertIn("ValueError: boom", json.loads(JsonFormatter().format(record))["exception"])


class HealthTests(TestCase):
    def test_healthz(self):
        self.assertEqual(self.client.get("/healthz").json(), {"status": "ok"})

    def test_healthz_is_get_only(self):
        self.assertEqual(self.client.post("/healthz").status_code, 405)

    def test_readyz_ok(self):
        response = self.client.get("/readyz")
        self.assertEqual((response.status_code, response.json()), (200, {"status": "ok", "failing": []}))

    def test_readyz_reports_a_failing_cache_without_the_cause(self):
        with mock.patch("apps.core.health.cache.get", return_value=None), self.assertLogs("apps.core.health", "ERROR"):
            response = self.client.get("/readyz")
        self.assertEqual((response.status_code, response.json()["failing"]), (503, ["cache"]))

    def test_readyz_reports_a_failing_database(self):
        with mock.patch("apps.core.health.connection.cursor", side_effect=RuntimeError("db password=hunter2")), \
                self.assertLogs("apps.core.health", "ERROR"):
            response = self.client.get("/readyz")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("hunter2", response.content.decode())

    @override_settings(SECURE_SSL_REDIRECT=True)
    def test_health_checks_are_not_redirected_to_https(self):
        from django.middleware.security import SecurityMiddleware

        redirected = SecurityMiddleware(lambda r: None).process_request(mock.Mock(
            path_info="/healthz", path="/healthz", is_secure=lambda: False, META={}, get_host=lambda: "x", get_full_path=lambda: "/healthz"))
        self.assertIsNone(redirected)
