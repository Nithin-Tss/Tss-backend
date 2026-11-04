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


class MiddlewareEdgeTests(SimpleTestCase):
    def test_multipart_upload_must_declare_its_size(self):
        r = self.client.post("/api/v1/", "x", content_type="multipart/form-data; boundary=b", CONTENT_LENGTH="")
        self.assertEqual(r.status_code, 411)

    def test_content_type_parameters_and_case_are_ignored(self):
        r = self.client.post("/api/v1/", "{}", content_type="Application/JSON; charset=utf-8")
        self.assertNotIn(r.status_code, (400, 413, 415))

    def test_empty_body_skips_content_type_check(self):
        r = self.client.post("/api/v1/", "", content_type="text/xml")
        self.assertNotEqual(r.status_code, 415)

    def test_reads_are_not_content_type_checked(self):
        r = self.client.get("/api/v1/", CONTENT_TYPE="text/xml", CONTENT_LENGTH="5")
        self.assertNotEqual(r.status_code, 415)

    def test_negative_content_length_is_refused(self):
        r = self.client.post("/api/v1/", "{}", content_type="application/json", CONTENT_LENGTH="-1")
        self.assertEqual(r.status_code, 400)

    def test_null_byte_in_path_is_refused(self):
        r = self.client.get("/api/v1/%00/")
        self.assertEqual(r.status_code, 400)

    def test_validation_skips_non_api_paths(self):
        r = self.client.post("/admin/login/", "<a/>", content_type="text/xml")
        self.assertNotIn(r.status_code, (411, 413))
        self.assertNotEqual(r.json()["detail"] if r["Content-Type"] == "application/json" else "", "Unsupported content type.")

    def test_docs_pages_get_the_relaxed_csp_and_others_the_strict_one(self):
        docs = self.client.get("/api/v1/docs/")
        self.assertIn("cdn.jsdelivr.net", docs["Content-Security-Policy"])
        self.assertIn("frame-ancestors 'none'", docs["Content-Security-Policy"])
        strict = self.client.get("/api/v1/")
        self.assertNotIn("cdn.jsdelivr.net", strict["Content-Security-Policy"])
        self.assertEqual(strict["Cross-Origin-Resource-Policy"], "same-site")


class ErrorPageTests(SimpleTestCase):
    def test_unknown_api_route_is_json(self):
        r = self.client.get("/api/v1/nope/")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r["Content-Type"], "application/json")
        self.assertEqual(r.json(), {"detail": "Not found."})

    def test_non_api_404_stays_html(self):
        r = self.client.get("/nope/")
        self.assertEqual(r.status_code, 404)
        self.assertIn("text/html", r["Content-Type"])

    def test_api_server_error_is_json(self):
        from django.test import RequestFactory
        from apps.core.errors import server_error
        r = server_error(RequestFactory().get("/api/v1/x/"))
        self.assertEqual(r.status_code, 500)
        self.assertEqual(r["Content-Type"], "application/json")
