from io import StringIO
from unittest import mock

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import override_settings
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

    def test_other_store_cannot_rename_a_picture(self):
        self.sign_up("a2@example.com", "Shop A2")
        asset = self.upload().json()
        self.sign_up("b2@example.com", "Shop B2")
        self.assertEqual(self.client.patch(f"/api/v1/media/{asset['id']}/", {"name": "x"}, format="json").status_code, 404)
        self.assertEqual(MediaAsset.objects.get().original_name, "banner.png")

    @override_settings(MEDIA_STORE_QUOTA_BYTES=100)
    def test_storage_quota_is_enforced_per_store(self):
        self.sign_up("quota@example.com", "Quota Shop")
        self.assertEqual(self.upload().status_code, 201)  # 40 bytes
        self.assertEqual(self.upload().status_code, 201)  # 80 bytes
        full = self.upload()  # 120 > 100
        self.assertEqual(full.status_code, 400)
        self.assertIn("full", str(full.json()))
        self.assertEqual(MediaAsset.objects.count(), 2)
        # another store has its own allowance
        self.sign_up("quota2@example.com", "Quota Shop 2")
        self.assertEqual(self.upload().status_code, 201)

    def test_uploads_are_rate_limited(self):
        from rest_framework.throttling import ScopedRateThrottle

        cache.clear()
        self.sign_up("rate@example.com", "Rate Shop")
        old = ScopedRateThrottle.THROTTLE_RATES
        ScopedRateThrottle.THROTTLE_RATES = {**old, "media_upload": "2/min"}
        try:
            codes = [self.upload().status_code for _ in range(3)]
        finally:
            ScopedRateThrottle.THROTTLE_RATES = old
        self.assertEqual(codes, [201, 201, 429])

    def test_requires_sign_in(self):
        self.assertIn(self.client.get("/api/v1/media/").status_code, (401, 403))


@override_settings(
    MEDIA_S3_BUCKET="tss-media",
    MEDIA_S3_ENDPOINT_URL="https://s3.example.com",
    MEDIA_S3_PUBLIC_URL="https://cdn.example.com",
)
class MediaBucketStorageTest(StoreClientMixin, APITestCase):
    """With a bucket configured, bytes go there instead of the database."""

    def setUp(self):
        self.objects = {}
        patches = [
            mock.patch("apps.media.storage.put", side_effect=lambda key, data, ctype: self.objects.__setitem__(key, data)),
            mock.patch("apps.media.storage.delete", side_effect=lambda key: self.objects.pop(key, None)),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def upload(self):
        return self.client.post("/api/v1/media/", {"image": png()}, format="multipart")

    def test_upload_goes_to_the_bucket_and_delete_removes_it(self):
        self.sign_up("bucket@example.com", "Bucket Shop")

        created = self.upload()
        self.assertEqual(created.status_code, 201, created.content)
        asset = MediaAsset.objects.get()
        self.assertIsNone(asset.data)
        self.assertEqual(asset.file_size, 40)
        self.assertRegex(asset.storage_key, r"^uploads/[^/]+/[0-9a-f]{32}\.png$")
        self.assertEqual(self.objects, {asset.storage_key: PNG})
        self.assertEqual(created.json()["url"], f"https://cdn.example.com/{asset.storage_key}")

        # The older /s/_img/<id> address redirects to the bucket
        old = self.client.get(f"/s/_img/{asset.id}")
        self.assertEqual(old.status_code, 302)
        self.assertEqual(old["Location"], f"https://cdn.example.com/{asset.storage_key}")

        self.ok("delete", f"/api/v1/media/{asset.id}/", expect=204)
        self.assertEqual(self.objects, {})

    def test_a_failed_bucket_write_saves_nothing(self):
        self.sign_up("fail@example.com", "Fail Shop")
        with mock.patch("apps.media.storage.put", side_effect=OSError("down")):
            self.assertEqual(self.upload().status_code, 400)
        self.assertFalse(MediaAsset.objects.exists())

    def test_move_command_copies_database_pictures(self):
        self.sign_up("move@example.com", "Move Shop")
        with override_settings(MEDIA_S3_BUCKET=""):
            self.upload()
        asset = MediaAsset.objects.get()
        self.assertIsNotNone(asset.data)

        call_command("move_media_to_storage", stdout=StringIO())
        asset.refresh_from_db()
        self.assertIsNone(asset.data)
        self.assertEqual(self.objects[asset.storage_key], PNG)
        self.assertEqual(self.client.get(f"/s/_img/{asset.id}").status_code, 302)
