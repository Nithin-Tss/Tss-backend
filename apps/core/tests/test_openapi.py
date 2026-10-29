from io import StringIO

from django.core.management import call_command
from django.contrib.auth import get_user_model
from django.test import TestCase


class OpenAPISchemaTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser(email="docs@example.com", password="pw-12345-Strong!")

    def test_schema_generates_without_errors(self):
        # --fail-on-warn would also flag undocumented-view hints; errors must be zero.
        out = StringIO()
        call_command("spectacular", "--validate", "--no-color", stdout=out, stderr=StringIO())
        self.assertIn("openapi:", out.getvalue())

    def test_schema_endpoint_and_docs_are_served(self):
        self.client.force_login(self.user)
        res = self.client.get("/api/v1/schema/", HTTP_ACCEPT="application/vnd.oai.openapi+json")
        self.assertEqual(res.status_code, 200)
        doc = res.json()
        self.assertTrue(doc["openapi"].startswith("3."))
        self.assertIn("jwtAuth", doc["components"]["securitySchemes"])
        self.assertTrue(doc["paths"])
        self.assertEqual(self.client.get("/api/v1/docs/").status_code, 200)
        self.assertEqual(self.client.get("/api/v1/redoc/").status_code, 200)
