from rest_framework.test import APITestCase


class SmokeTest(APITestCase):
    def test_signup_store_product_storefront(self):
        r = self.client.post("/api/v1/auth/signup/", {
            "firstName": "Asha", "lastName": "K", "email": "asha@example.com",
            "password": "Sturdy-pass-123", "confirmPassword": "Sturdy-pass-123",
            "mobileNumber": "9876543210", "agreeTerms": True,
        }, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.client.credentials(HTTP_AUTHORIZATION="Bearer " + r.json()["token"])

        r = self.client.post("/api/v1/stores/", {"storeName": "Asha Shop"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        store = r.json()
        self.client.credentials(
            HTTP_AUTHORIZATION=self.client._credentials["HTTP_AUTHORIZATION"],
            HTTP_X_STORE_ID=store["storeId"],
        )

        r = self.client.post("/api/v1/catalog/products/", {
            "title": "Linen Shirt", "status": "active", "price": "1499", "collections": ["Summer"],
        }, format="json")
        self.assertEqual(r.status_code, 201, r.content)

        r = self.client.get(f"/s/{store['storeSlug']}/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Linen Shirt", r.content.decode())
