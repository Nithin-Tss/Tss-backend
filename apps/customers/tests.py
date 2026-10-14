from rest_framework import status
from rest_framework.test import APITestCase

from apps.core.tests.test_flow import StoreClientMixin
from apps.customers.models import Customer, CustomerAddress
from apps.identity.models import User
from apps.tenancy.models import Store, StoreMembership


class CustomerAddressManagementTest(StoreClientMixin, APITestCase):
    def setUp(self):
        # 1. Owner & Store
        self.owner_token, self.store = self.sign_up("storeowner@example.com", "Test Fashion Store")
        self.store_id = self.store["storeId"]

        # 2. Regular Customer / Shopper (Not an admin/store member)
        self.client.post("/api/v1/auth/signup/", {
            "firstName": "Alice",
            "lastName": "Smith",
            "email": "alice@example.com",
            "password": "StrongPassword123!",
            "confirmPassword": "StrongPassword123!",
            "mobileNumber": "9123456780",
            "agreeTerms": True,
        }, format="json")
        login_res = self.client.post("/api/v1/auth/login/", {
            "email": "alice@example.com",
            "password": "StrongPassword123!",
        }, format="json")
        self.alice_token = login_res.json()["access"]

        # 3. Another Regular Customer (Bob)
        self.client.post("/api/v1/auth/signup/", {
            "firstName": "Bob",
            "lastName": "Jones",
            "email": "bob@example.com",
            "password": "StrongPassword123!",
            "confirmPassword": "StrongPassword123!",
            "mobileNumber": "9123456781",
            "agreeTerms": True,
        }, format="json")
        login_res_bob = self.client.post("/api/v1/auth/login/", {
            "email": "bob@example.com",
            "password": "StrongPassword123!",
        }, format="json")
        self.bob_token = login_res_bob.json()["access"]

    def auth_alice(self):
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {self.alice_token}",
            HTTP_X_STORE_ID=self.store_id,
        )

    def auth_bob(self):
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {self.bob_token}",
            HTTP_X_STORE_ID=self.store_id,
        )

    def auth_owner(self):
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {self.owner_token}",
            HTTP_X_STORE_ID=self.store_id,
        )

    def test_customer_add_view_edit_delete_address(self):
        """Customer Profile address management flow."""
        self.auth_alice()

        # Initial list is empty
        res = self.client.get("/api/v1/customers/addresses/?customer=me")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(len(res.json()), 0)

        # 1. Add first shipping address - should automatically become default
        addr_data = {
            "address_type": "shipping",
            "first_name": "Alice",
            "last_name": "Smith",
            "company": "Smith & Co",
            "address_line_1": "123 Collins St",
            "address_line_2": "Apt 4B",
            "city": "Melbourne",
            "state": "VIC",
            "postal_code": "3000",
            "country": "Australia",
            "phone": "+61 412345678",
            "is_default": False,  # Should become True because it's first of type
        }
        res = self.client.post("/api/v1/customers/addresses/", addr_data, format="json")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.content)
        addr1 = res.json()
        self.assertTrue(addr1["is_default"])
        self.assertEqual(addr1["address_line_1"], "123 Collins St")
        self.assertEqual(addr1["city"], "Melbourne")

        # 2. Add second shipping address (is_default=True)
        addr2_data = {
            "address_type": "shipping",
            "first_name": "Alice",
            "last_name": "Smith",
            "address_line_1": "456 Bourke St",
            "city": "Melbourne",
            "state": "VIC",
            "postal_code": "3001",
            "country": "Australia",
            "is_default": True,
        }
        res2 = self.client.post("/api/v1/customers/addresses/", addr2_data, format="json")
        self.assertEqual(res2.status_code, status.HTTP_201_CREATED)
        addr2 = res2.json()
        self.assertTrue(addr2["is_default"])

        # Check that addr1 was downgraded from default (single default per type rule)
        res_list = self.client.get("/api/v1/customers/addresses/?customer=me")
        addresses = res_list.json()
        self.assertEqual(len(addresses), 2)
        addr1_refreshed = next(a for a in addresses if a["id"] == addr1["id"])
        addr2_refreshed = next(a for a in addresses if a["id"] == addr2["id"])
        self.assertFalse(addr1_refreshed["is_default"])
        self.assertTrue(addr2_refreshed["is_default"])

        # 3. Edit address line 1
        edit_res = self.client.patch(f"/api/v1/customers/addresses/{addr1['id']}/", {
            "address_line_1": "789 Flinders St",
        }, format="json")
        self.assertEqual(edit_res.status_code, status.HTTP_200_OK)
        self.assertEqual(edit_res.json()["address_line_1"], "789 Flinders St")

        # 4. Set as default via set-default action
        set_def_res = self.client.post(f"/api/v1/customers/addresses/{addr1['id']}/set-default/")
        self.assertEqual(set_def_res.status_code, status.HTTP_200_OK)
        self.assertTrue(set_def_res.json()["is_default"])

        # Verify addr2 is no longer default
        res_list = self.client.get("/api/v1/customers/addresses/?customer=me")
        addr1_check = next(a for a in res_list.json() if a["id"] == addr1["id"])
        addr2_check = next(a for a in res_list.json() if a["id"] == addr2["id"])
        self.assertTrue(addr1_check["is_default"])
        self.assertFalse(addr2_check["is_default"])

        # 5. Delete default address: addr2 should be promoted to default
        del_res = self.client.delete(f"/api/v1/customers/addresses/{addr1['id']}/")
        self.assertEqual(del_res.status_code, status.HTTP_204_NO_CONTENT)

        res_list_after = self.client.get("/api/v1/customers/addresses/?customer=me")
        self.assertEqual(len(res_list_after.json()), 1)
        self.assertTrue(res_list_after.json()[0]["is_default"])
        self.assertEqual(res_list_after.json()[0]["id"], addr2["id"])

    def test_security_customer_cannot_access_or_modify_other_customer_address(self):
        """Security: Alice cannot read, edit, or delete Bob's address."""
        # Bob creates an address
        self.auth_bob()
        res_bob = self.client.post("/api/v1/customers/addresses/", {
            "address_type": "shipping",
            "first_name": "Bob",
            "last_name": "Jones",
            "address_line_1": "100 King St",
            "city": "Sydney",
            "state": "NSW",
            "postal_code": "2000",
            "country": "Australia",
        }, format="json")
        self.assertEqual(res_bob.status_code, status.HTTP_201_CREATED)
        bob_addr_id = res_bob.json()["id"]
        bob_cust_id = res_bob.json()["customer"]

        # Alice attempts to query Bob's addresses with ?customer=<bob_cust_id>
        self.auth_alice()
        forbidden_get = self.client.get(f"/api/v1/customers/addresses/?customer={bob_cust_id}")
        self.assertEqual(forbidden_get.status_code, status.HTTP_403_FORBIDDEN)

        # Alice attempts to edit Bob's address
        forbidden_patch = self.client.patch(f"/api/v1/customers/addresses/{bob_addr_id}/", {
            "city": "Hacked City",
        }, format="json")
        self.assertIn(forbidden_patch.status_code, (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND))

        # Alice attempts to delete Bob's address
        forbidden_del = self.client.delete(f"/api/v1/customers/addresses/{bob_addr_id}/")
        self.assertIn(forbidden_del.status_code, (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND))

        # Alice attempts to set default on Bob's address
        forbidden_def = self.client.post(f"/api/v1/customers/addresses/{bob_addr_id}/set-default/")
        self.assertIn(forbidden_def.status_code, (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND))

    def test_admin_address_management(self):
        """Admin can view, add, edit, and delete any customer's address."""
        # Alice creates an address
        self.auth_alice()
        res_alice = self.client.post("/api/v1/customers/addresses/", {
            "address_type": "shipping",
            "first_name": "Alice",
            "last_name": "Smith",
            "address_line_1": "10 High St",
            "city": "Brisbane",
            "state": "QLD",
            "postal_code": "4000",
            "country": "Australia",
        }, format="json")
        alice_addr_id = res_alice.json()["id"]
        alice_cust_id = res_alice.json()["customer"]

        # Store owner manages Alice's address
        self.auth_owner()

        # Admin lists Alice's addresses
        admin_list = self.client.get(f"/api/v1/customers/addresses/?customer={alice_cust_id}")
        self.assertEqual(admin_list.status_code, status.HTTP_200_OK)
        self.assertEqual(len(admin_list.json()), 1)

        # Admin adds an address for Alice
        admin_add = self.client.post("/api/v1/customers/addresses/", {
            "customer": alice_cust_id,
            "address_type": "billing",
            "first_name": "Alice",
            "last_name": "Smith",
            "address_line_1": "20 Queen St",
            "city": "Brisbane",
            "state": "QLD",
            "postal_code": "4000",
            "country": "Australia",
            "is_default": True,
        }, format="json")
        self.assertEqual(admin_add.status_code, status.HTTP_201_CREATED)
        billing_addr_id = admin_add.json()["id"]

        # Admin edits Alice's billing address
        admin_edit = self.client.patch(f"/api/v1/customers/addresses/{billing_addr_id}/", {
            "address_line_1": "22 Queen St Suite 5",
        }, format="json")
        self.assertEqual(admin_edit.status_code, status.HTTP_200_OK)
        self.assertEqual(admin_edit.json()["address_line_1"], "22 Queen St Suite 5")

        # Admin deletes Alice's billing address
        admin_del = self.client.delete(f"/api/v1/customers/addresses/{billing_addr_id}/")
        self.assertEqual(admin_del.status_code, status.HTTP_204_NO_CONTENT)

    def test_validation_required_fields(self):
        """Missing required address fields returns 400 with field errors."""
        self.auth_alice()
        res = self.client.post("/api/v1/customers/addresses/", {
            "first_name": "",
            "address_line_1": "",
        }, format="json")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        err = res.json()
        self.assertIn("first_name", err)
        self.assertIn("address_line_1", err)
        self.assertIn("city", err)
        self.assertIn("state", err)
        self.assertIn("postal_code", err)
        self.assertIn("country", err)

    def test_store_isolation(self):
        """Addresses in store 1 are not visible from store 2."""
        # Alice creates address in store 1
        self.auth_alice()
        res = self.client.post("/api/v1/customers/addresses/", {
            "address_type": "shipping",
            "first_name": "Alice",
            "last_name": "Smith",
            "address_line_1": "1 Store One Way",
            "city": "Perth",
            "state": "WA",
            "postal_code": "6000",
            "country": "Australia",
        }, format="json")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        addr1_id = res.json()["id"]

        # Owner 2 creates store 2
        owner2_token, store2 = self.sign_up("owner2@example.com", "Second Store")
        store2_id = store2["storeId"]

        # Alice visits store 2
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {self.alice_token}",
            HTTP_X_STORE_ID=store2_id,
        )
        # In store 2, Alice's address from store 1 is not in the list
        s2_list = self.client.get("/api/v1/customers/addresses/?customer=me")
        self.assertEqual(s2_list.status_code, status.HTTP_200_OK)
        self.assertNotIn(addr1_id, [a["id"] for a in s2_list.json()])

        # Directly trying to get or patch addr1 with store 2 header returns 404
        s2_get = self.client.get(f"/api/v1/customers/addresses/{addr1_id}/")
        self.assertEqual(s2_get.status_code, status.HTTP_404_NOT_FOUND)

