"""
Request validation and security headers on /api/.

    python manage.py test apps.core.tests.test_middleware --settings=config.test_settings
"""
from django.test import SimpleTestCase, override_settings


class MiddlewareTests(SimpleTestCase):
    def test_security_headers_on_api(self):
        r = self.client.get("/api/v1/")
        self.assertEqual(r["Cache-Control"], "no-store")
        self.assertIn("frame-ancestors 'none'", r["Content-Security-Policy"])
        self.assertIn("Permissions-Policy", r)

    def test_no_api_headers_outside_api(self):
        r = self.client.get("/admin/login/")
        self.assertNotIn("Content-Security-Policy", r)

    def test_unsupported_content_type(self):
        r = self.client.post("/api/v1/", "<a/>", content_type="text/xml")
        self.assertEqual(r.status_code, 415)

    def test_json_body_passes_validation(self):
        r = self.client.post("/api/v1/", "{}", content_type="application/json")
        self.assertNotIn(r.status_code, (400, 413, 415))

    @override_settings(API_MAX_REQUEST_BYTES=10)
    def test_oversized_body(self):
        r = self.client.post("/api/v1/", '{"a": "%s"}' % ("x" * 50), content_type="application/json")
        self.assertEqual(r.status_code, 413)

    def test_null_byte_in_url(self):
        r = self.client.get("/api/v1/?q=a%00b")
        self.assertEqual(r.status_code, 400)

    def test_bad_content_length(self):
        r = self.client.post("/api/v1/", "{}", content_type="application/json", CONTENT_LENGTH="abc")
        self.assertEqual(r.status_code, 400)
