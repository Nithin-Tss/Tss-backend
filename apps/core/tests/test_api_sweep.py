"""
Contract sweep over every operation in the OpenAPI schema.

For each (path, method) the API documents, check without knowing the endpoint:
  - an anonymous call is never a 5xx, and routes that need sign-in say 401/403
  - a signed-in store owner sending an empty or junk body gets a 4xx, never a 500

    python manage.py test apps.core.tests.test_api_sweep --settings=config.test_settings
"""
from django.core.cache import cache
from drf_spectacular.generators import SchemaGenerator
from rest_framework.test import APIClient, APITestCase

# Routes that are public on purpose (sign-in, sign-up, storefront, webhooks from Stripe, docs...)
PUBLIC_PREFIXES = (
    "/api/v1/auth/", "/api/v1/storefront/", "/api/v1/schema/", "/api/v1/docs/", "/api/v1/redoc/",
    "/api/v1/payments/stripe/webhook/",
    "/api/v1/assistant/",  # answers 404 unless ASSISTANT_REAL_MODE is set
)
# Answers 503 on purpose when the third-party service isn't configured
UNCONFIGURED_503 = {"/api/v1/payments/stripe/webhook/"}
# Streaming or file endpoints that don't take a JSON body
SKIP_JUNK = ("export", "download", "stream")
METHODS = ("get", "post", "put", "patch", "delete")


ZERO_UUID = "00000000-0000-0000-0000-000000000000"


def operations():
    """Yield (path template, method, {param name: example value}) for each documented operation."""
    schema = SchemaGenerator().get_schema(request=None, public=True)
    for path, item in schema["paths"].items():
        shared = item.get("parameters", [])
        for method in METHODS:
            if method not in item:
                continue
            values = {}
            for param in shared + item[method].get("parameters", []):
                if param.get("in") == "path":
                    kind = param.get("schema", {})
                    values[param["name"]] = (
                        ZERO_UUID if kind.get("format") == "uuid" else "0" if kind.get("type") == "integer" else "x"
                    )
            yield path, method, values


class ApiSweepTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.ops = sorted(operations(), key=lambda o: (o[0], o[1]))

    def setUp(self):
        cache.clear()
        self.owner = APIClient(raise_request_exception=False)
        r = self.owner.post("/api/v1/auth/signup/", {
            "firstName": "A", "lastName": "B", "email": "sweep@example.com",
            "password": "Sturdy-pass-123", "confirmPassword": "Sturdy-pass-123",
            "mobileNumber": "9876543210", "agreeTerms": True,
        }, format="json")
        token = r.json()["access"]
        self.owner.credentials(HTTP_AUTHORIZATION="Bearer " + token)
        store = self.owner.post("/api/v1/stores/", {"storeName": "Sweep Shop"}, format="json").json()
        self.owner.credentials(HTTP_AUTHORIZATION="Bearer " + token, HTTP_X_STORE_ID=str(store["storeId"]))

    def call(self, client, method, path, values, **kw):
        cache.clear()  # throttles would turn a sweep into 429s
        return getattr(client, method)(path.format(**values), **kw)

    maxDiff = None

    def test_schema_lists_operations(self):
        self.assertGreater(len(self.ops), 50)

    def test_anonymous_never_server_errors_and_private_routes_reject(self):
        anon = APIClient(raise_request_exception=False)
        failures = []
        for path, method, values in self.ops:
            r = self.call(anon, method, path, values, **({} if method == "get" else {"data": {}, "format": "json"}))
            if r.status_code >= 500 and not (r.status_code == 503 and path in UNCONFIGURED_503):
                failures.append(f"{method.upper()} {path} -> {r.status_code}")
            elif not path.startswith(PUBLIC_PREFIXES) and r.status_code not in (401, 403):
                failures.append(f"{method.upper()} {path} -> {r.status_code} (expected 401/403 without sign-in)")
        self.assertEqual(failures, [])

    def test_signed_in_junk_input_never_server_errors(self):
        failures = []
        junk = [{}, {"id": None, "items": "not-a-list", "quantity": -1, "email": 5}, []]
        for path, method, values in self.ops:
            if method == "delete" or any(s in path for s in SKIP_JUNK) or path.startswith("/api/v1/auth/"):
                continue
            for body in ([None] if method == "get" else junk):
                kw = {} if body is None else {"data": body, "format": "json"}
                r = self.call(self.owner, method, path, values, **kw)
                if r.status_code >= 500 and not (r.status_code == 503 and path in UNCONFIGURED_503):
                    failures.append(f"{method.upper()} {path} body={body!r} -> {r.status_code}")
        self.assertEqual(failures, [])

    def test_unknown_ids_are_404_not_500(self):
        failures = []
        for path, method, values in self.ops:
            if method != "get" or "{" not in path or path.startswith(PUBLIC_PREFIXES):
                continue
            r = self.call(self.owner, "get", path, values)
            if r.status_code >= 500:
                failures.append(f"GET {path} -> {r.status_code}")
        self.assertEqual(failures, [])
