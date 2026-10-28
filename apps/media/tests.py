from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APITestCase

from apps.core.tests.test_flow import StoreClientMixin
from apps.media.models import MediaAsset
from apps.themes.models import ThemeImage

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def png(name="banner.png", content=PNG):
    return SimpleUploadedFile(name, content)


class MediaAssetApiTest(StoreClientMixin, APITestCase):
    def upload(self, **kwargs):
        return self.client.post("/api/v1/media/", {"image": png(**kwargs)}, format="multipart")

    def test_upload_list_rename_and_delete(self):
        self.sign_up("media@example.com", "Media Shop")

        created = self.upload()
        self.assertEqual(created.status_code, 201, created.content)
        asset = created.json()
        self.assertEqual((asset["name"], asset["mime_type"], asset["file_size"]), ("banner.png", "image/png", 40))
        self.assertNotIn("data", asset)  # the list never carries the bytes
        self.assertEqual(bytes(MediaAsset.objects.get().data), PNG)
        self.assertIn("/s/_img/", asset["url"])
        self.assertEqual(self.client.get(asset["url"].split("testserver", 1)[-1]).status_code, 200)

        self.assertEqual([row["id"] for row in self.ok("get", "/api/v1/media/")], [asset["id"]])
        self.assertEqual(len(self.ok("get", "/api/v1/media/?q=banner")), 1)
        self.assertEqual(self.ok("get", "/api/v1/media/?q=nothing"), [])

        renamed = self.ok("patch", f"/api/v1/media/{asset['id']}/", {"name": "Hero", "alt_text": "A red shirt"})
        self.assertEqual((renamed["name"], renamed["alt_text"]), ("Hero", "A red shirt"))
        # What the file is cannot be edited
        self.ok("patch", f"/api/v1/media/{asset['id']}/", {"file_size": 1, "mime_type": "text/plain"})
        self.assertEqual(MediaAsset.objects.get().mime_type, "image/png")

        self.ok("delete", f"/api/v1/media/{asset['id']}/", expect=204)
        self.assertFalse(MediaAsset.objects.exists())
        self.assertFalse(ThemeImage.objects.exists())

    def test_upload_rules(self):
        self.sign_up("rules@example.com", "Rules Shop")
        self.assertEqual(self.upload(content=b"plain text").status_code, 400)
        self.assertEqual(self.upload(content=PNG + b"\x00" * (1024 * 1024)).status_code, 400)
        self.assertEqual(self.client.post("/api/v1/media/", {}, format="multipart").status_code, 400)
        self.assertFalse(MediaAsset.objects.exists())

    def test_a_picture_in_use_is_not_deleted(self):
        self.sign_up("inuse@example.com", "Use Shop")
        asset = self.upload().json()
        image_id = asset["url"].rsplit("/", 1)[-1]
        from apps.themes.services import get_store_theme
        from apps.tenancy.models import Store

        store = Store.objects.get()
        assignment = get_store_theme(store)
        assignment.custom_data = {"settings": {"logo": f"/s/_img/{image_id}"}}
        assignment.save(update_fields=["custom_data"])

        refused = self.client.delete(f"/api/v1/media/{asset['id']}/")
        self.assertEqual(refused.status_code, 400)
        self.assertIn("used in your online store", str(refused.json()))
        self.assertTrue(MediaAsset.objects.exists())

        assignment.custom_data = {}
        assignment.save(update_fields=["custom_data"])
        self.assertEqual(self.client.delete(f"/api/v1/media/{asset['id']}/").status_code, 204)

    def test_each_store_sees_only_its_own_pictures(self):
        self.sign_up("a@example.com", "Shop A")
        asset = self.upload().json()
        self.sign_up("b@example.com", "Shop B")
        self.upload(name="b.png")

        self.assertEqual([row["name"] for row in self.ok("get", "/api/v1/media/")], ["b.png"])
        self.assertEqual(self.client.get(f"/api/v1/media/{asset['id']}/").status_code, 404)
        self.assertEqual(self.client.delete(f"/api/v1/media/{asset['id']}/").status_code, 404)

    def test_requires_sign_in(self):
        self.assertIn(self.client.get("/api/v1/media/").status_code, (401, 403))
