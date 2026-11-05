from rest_framework.test import APITestCase

from apps.catalog.models import Product
from apps.core.tests.test_flow import StoreClientMixin

DEFS = "/api/v1/metafields/definitions/"
VALUES = "/api/v1/metafields/values/"
MO_DEFS = "/api/v1/metafields/metaobject-definitions/"
MOS = "/api/v1/metafields/metaobjects/"


class MetafieldTests(StoreClientMixin, APITestCase):
    def setUp(self):
        self.token, self.store = self.sign_up("mf@example.com", "Meta Shop")
        self.product = Product.objects.create(store_id=self.store["storeId"], title="Tea", status="active")

    def definition(self, **extra):
        body = {"owner_type": "product", "key": "brew_time", "name": "Brew time", "type": "integer",
                "validations": {"min": 1, "max": 10}, **extra}
        return self.ok("post", DEFS, body, 201)

    def test_set_and_replace_a_value(self):
        d = self.definition()
        body = {"definition": d["id"], "owner_id": str(self.product.pk), "value": 4}
        first = self.ok("post", VALUES, body, 201)
        self.assertEqual(first["namespace"], "custom")
        again = self.ok("post", VALUES, {**body, "value": 5}, 200)
        self.assertEqual(again["id"], first["id"])
        rows = self.ok("get", f"{VALUES}?owner_id={self.product.pk}")
        self.assertEqual([r["value"] for r in rows], [5])

    def test_value_is_checked_against_the_definition(self):
        d = self.definition()
        r = self.client.post(VALUES, {"definition": d["id"], "owner_id": str(self.product.pk), "value": 99}, format="json")
        self.assertEqual(r.status_code, 400, r.content)
        r = self.client.post(VALUES, {"definition": d["id"], "owner_id": str(self.product.pk), "value": "x"}, format="json")
        self.assertEqual(r.status_code, 400, r.content)

    def test_owner_must_exist_in_this_store(self):
        d = self.definition()
        r = self.client.post(VALUES, {"definition": d["id"], "owner_id": "00000000-0000-0000-0000-000000000001", "value": 3}, format="json")
        self.assertEqual(r.status_code, 400, r.content)

    def test_duplicate_definition_refused_and_type_locked(self):
        d = self.definition()
        r = self.client.post(DEFS, {"owner_type": "product", "key": "brew_time", "name": "x", "type": "integer"}, format="json")
        self.assertEqual(r.status_code, 400, r.content)
        r = self.client.patch(f"{DEFS}{d['id']}/", {"type": "boolean"}, format="json")
        self.assertEqual(r.status_code, 400, r.content)

    def test_another_store_cannot_see_definitions(self):
        self.definition()
        self.sign_up("other@example.com", "Other Shop")
        data = self.ok("get", DEFS)
        results = data["results"] if isinstance(data, dict) else data
        self.assertEqual(results, [])


class MetaobjectTests(StoreClientMixin, APITestCase):
    def setUp(self):
        self.token, self.store = self.sign_up("mo@example.com", "Object Shop")
        self.defn = self.ok("post", MO_DEFS, {
            "type": "author", "name": "Author",
            "fields": [
                {"key": "name", "name": "Name", "type": "single_line_text", "required": True},
                {"key": "born", "type": "date"},
            ],
        }, 201)

    def test_create_entry_and_validate_fields(self):
        e = self.ok("post", MOS, {"definition": self.defn["id"], "handle": "jane", "values": {"name": "Jane", "born": "1990-02-03"}}, 201)
        self.assertEqual(e["type"], "author")
        for values in ({"born": "1990-02-03"}, {"name": "J", "extra": 1}, {"name": "J", "born": "nope"}):
            r = self.client.post(MOS, {"definition": self.defn["id"], "handle": "bad", "values": values}, format="json")
            self.assertEqual(r.status_code, 400, r.content)

    def test_handle_unique_per_type(self):
        body = {"definition": self.defn["id"], "handle": "jane", "values": {"name": "Jane"}}
        self.ok("post", MOS, body, 201)
        self.assertEqual(self.client.post(MOS, body, format="json").status_code, 400)

    def test_metaobject_reference_metafield(self):
        entry = self.ok("post", MOS, {"definition": self.defn["id"], "handle": "jane", "values": {"name": "Jane"}}, 201)
        product = Product.objects.create(store_id=self.store["storeId"], title="Book", status="active")
        d = self.ok("post", DEFS, {"owner_type": "product", "key": "author", "name": "Author",
                                   "type": "metaobject_reference", "validations": {"metaobject_type": "author"}}, 201)
        self.ok("post", VALUES, {"definition": d["id"], "owner_id": str(product.pk), "value": entry["id"]}, 201)
        r = self.client.post(VALUES, {"definition": d["id"], "owner_id": str(product.pk),
                                      "value": "00000000-0000-0000-0000-000000000001"}, format="json")
        self.assertEqual(r.status_code, 400, r.content)
