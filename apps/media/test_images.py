"""Image checks and cleaning (apps/media/images.py), through every upload path."""
from io import BytesIO

from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image
from rest_framework.test import APITestCase

from apps.core.tests.test_flow import StoreClientMixin
from apps.media.images import clean_image
from apps.media.models import MediaAsset
from apps.media.tests import PNG, make_image
from apps.themes.models import ThemeImage

NOT_SUPPORTED = "Not a supported image. Use JPG, PNG or WEBP."
GPS = {1: "N", 2: (12.0, 58.0, 0.0), 3: "E", 4: (77.0, 35.0, 0.0)}


def exif_with_gps(orientation=1):
    exif = Image.Exif()
    exif[0x0112] = orientation
    exif[0x010F] = "PhoneMaker"  # camera make
    exif[0x8825] = GPS
    return exif.tobytes()


def open_image(data):
    return Image.open(BytesIO(data))


class CleanImageTests(APITestCase):
    def reject(self, data):
        from rest_framework.exceptions import ValidationError

        with self.assertRaises(ValidationError) as caught:
            clean_image(data)
        return str(caught.exception.detail["image"])

    def test_the_first_bytes_alone_are_not_enough(self):
        header_only = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
        self.assertEqual(self.reject(header_only), NOT_SUPPORTED)
        self.assertEqual(self.reject(b"\xff\xd8\xff" + b"<script>alert(1)</script>"), NOT_SUPPORTED)
        self.assertEqual(self.reject(b"RIFF\x00\x00\x00\x00WEBP" + b"junk" * 20), NOT_SUPPORTED)

    def test_a_cut_off_picture_is_refused(self):
        real = make_image("PNG", size=(64, 64))
        self.assertEqual(self.reject(real[: len(real) // 2]), NOT_SUPPORTED)

    def test_svg_gif_and_text_are_refused(self):
        self.assertEqual(self.reject(b'<svg xmlns="http://www.w3.org/2000/svg"><script>1</script></svg>'), NOT_SUPPORTED)
        self.assertEqual(self.reject(make_image("GIF")), NOT_SUPPORTED)
        self.assertEqual(self.reject(b"plain text"), NOT_SUPPORTED)

    def test_more_than_25_million_pixels_is_refused_with_a_clear_message(self):
        big = make_image("PNG", size=(6000, 5000), mode="L")  # a few KB as a file, 30 million pixels once opened
        self.assertLess(len(big), 1024 * 1024)
        self.assertIn("25 million pixels", self.reject(big))

    def test_a_normal_size_is_kept(self):
        data, ext, content_type = clean_image(make_image("PNG", size=(1200, 900)))
        self.assertEqual((ext, content_type, open_image(data).size), ("png", "image/png", (1200, 900)))

    def test_every_format_keeps_its_format(self):
        for fmt, ext, content_type in (("JPEG", "jpg", "image/jpeg"), ("PNG", "png", "image/png"),
                                       ("WEBP", "webp", "image/webp")):
            data, got_ext, got_type = clean_image(make_image(fmt))
            self.assertEqual((got_ext, got_type, open_image(data).format), (ext, content_type, fmt))

    def test_gps_and_camera_data_are_removed(self):
        for fmt in ("JPEG", "WEBP"):
            source = make_image(fmt, exif=exif_with_gps())
            self.assertEqual(open_image(source).getexif().get_ifd(0x8825).get(1), "N", "test picture must carry GPS")
            data, _, _ = clean_image(source)
            exif = open_image(data).getexif()
            self.assertEqual((len(exif), dict(exif.get_ifd(0x8825))), (0, {}), fmt)
            self.assertNotIn(b"PhoneMaker", data)
            self.assertNotIn(b"Exif", data)

    def test_png_text_and_exif_chunks_are_removed(self):
        from PIL.PngImagePlugin import PngInfo

        info = PngInfo()
        info.add_text("Comment", "taken at 12.97N 77.59E")
        source = make_image("PNG", pnginfo=info, exif=exif_with_gps())
        self.assertIn(b"12.97N", source)
        data, _, _ = clean_image(source)
        self.assertNotIn(b"12.97N", data)
        self.assertEqual(len(open_image(data).getexif()), 0)

    def test_phone_rotation_is_applied(self):
        source = make_image("JPEG", size=(80, 40), exif=exif_with_gps(orientation=6))  # "turn 90 degrees"
        data, _, _ = clean_image(source)
        image = open_image(data)
        self.assertEqual(image.size, (40, 80))
        self.assertNotIn(0x0112, image.getexif())

    def test_an_animated_picture_keeps_all_its_frames(self):
        frames = [Image.new("RGB", (16, 16), colour) for colour in ((255, 0, 0), (0, 255, 0), (0, 0, 255))]
        buffer = BytesIO()
        frames[0].save(buffer, format="WEBP", save_all=True, append_images=frames[1:], duration=80, loop=0)
        data, _, _ = clean_image(buffer.getvalue())
        self.assertEqual(getattr(open_image(data), "n_frames", 1), 3)


class EveryUploadPathUsesItTests(StoreClientMixin, APITestCase):
    """Product photos, theme pictures and the media library all clean what they keep."""

    PATHS = ("/api/v1/catalog/products/images/", "/api/v1/themes/images/", "/api/v1/media/")

    def post(self, path, content, name="photo.jpg"):
        return self.client.post(path, {"image": SimpleUploadedFile(name, content)}, format="multipart")

    def stored_bytes(self):
        asset = MediaAsset.objects.first()
        image = ThemeImage.objects.first()
        return bytes((asset or image).data)

    def test_each_path_refuses_a_fake_image(self):
        self.sign_up("fake@example.com", "Fake Shop")
        fake = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
        for path in self.PATHS:
            r = self.post(path, fake, "fake.png")
            self.assertEqual(r.status_code, 400, path)
            self.assertEqual(r.json(), {"image": NOT_SUPPORTED}, path)
        self.assertFalse(MediaAsset.objects.exists() or ThemeImage.objects.exists())

    def test_each_path_refuses_a_picture_with_too_many_pixels(self):
        self.sign_up("bomb@example.com", "Bomb Shop")
        big = make_image("PNG", size=(6000, 5000), mode="L")
        for path in self.PATHS:
            r = self.post(path, big, "big.png")
            self.assertEqual(r.status_code, 400, path)
            self.assertIn("25 million pixels", str(r.json()), path)

    def test_each_path_keeps_a_picture_without_gps_and_upright(self):
        self.sign_up("gps@example.com", "Gps Shop")
        source = make_image("JPEG", size=(80, 40), exif=exif_with_gps(orientation=6))
        for path in self.PATHS:
            MediaAsset.objects.all().delete()
            ThemeImage.objects.all().delete()
            r = self.post(path, source)
            self.assertEqual(r.status_code, 201, (path, r.content))
            image = open_image(self.stored_bytes())
            self.assertEqual(image.size, (40, 80), path)
            self.assertEqual((len(image.getexif()), image.format), (0, "JPEG"), path)

    def test_the_media_library_size_and_type_are_those_of_the_cleaned_file(self):
        self.sign_up("size@example.com", "Size Shop")
        source = make_image("JPEG", size=(80, 40), exif=exif_with_gps())
        asset = self.post("/api/v1/media/", source).json()
        stored = bytes(MediaAsset.objects.get().data)
        self.assertEqual((asset["mime_type"], asset["file_size"]), ("image/jpeg", len(stored)))
        self.assertNotEqual(stored, source)

    def test_a_normal_picture_still_uploads(self):
        self.sign_up("ok@example.com", "Ok Shop")
        for path in self.PATHS:
            self.assertEqual(self.post(path, PNG, "a.png").status_code, 201, path)


XMP = (
    b'<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?><x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF '
    b'xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"><rdf:Description xmlns:exif="http://ns.adobe.com/exif/1.0/" '
    b'exif:GPSLatitude="12,58.0N" exif:GPSLongitude="77,35.0E"/></rdf:RDF></x:xmpmeta><?xpacket end="w"?>'
)


def noisy_image(fmt, size, quality):
    """Random pixels compress badly: a way to get a file of a known, large size."""
    import random

    pixels = random.Random(1).randbytes(size[0] * size[1] * 3)
    buffer = BytesIO()
    Image.frombytes("RGB", size, pixels).save(buffer, format=fmt, quality=quality)
    return buffer.getvalue()


class NoHiddenMetadataTests(StoreClientMixin, APITestCase):
    """EXIF, XMP and every other kind of metadata (GPS above all) is never written to the kept file."""

    def source(self, fmt):
        data = make_image(fmt, exif=exif_with_gps(), xmp=XMP)
        for needle in (b"GPS", b"xmp", b"xpacket"):  # the test picture really carries what must disappear
            self.assertIn(needle, data, (fmt, needle))
        return data

    def test_xmp_and_exif_gps_are_not_in_the_kept_bytes(self):
        for fmt in ("JPEG", "WEBP"):
            data, _, _ = clean_image(self.source(fmt))
            for needle in (b"gps", b"xmp", b"exif", b"xpacket", b"phonemaker"):
                self.assertNotIn(needle, data.lower(), (fmt, needle))

    def test_xmp_and_exif_gps_are_not_in_a_picture_uploaded_through_each_path(self):
        self.sign_up("xmp@example.com", "Xmp Shop")
        for path in EveryUploadPathUsesItTests.PATHS:
            MediaAsset.objects.all().delete()
            ThemeImage.objects.all().delete()
            for fmt, name in (("JPEG", "a.jpg"), ("WEBP", "a.webp")):
                r = self.client.post(path, {"image": SimpleUploadedFile(name, self.source(fmt))}, format="multipart")
                self.assertEqual(r.status_code, 201, (path, fmt, r.content))
            kept = [bytes(row.data).lower() for row in (*MediaAsset.objects.all(), *ThemeImage.objects.all())]
            self.assertEqual(len(kept), 2, path)
            for data in kept:
                for needle in (b"gps", b"xmp", b"exif"):
                    self.assertNotIn(needle, data, (path, needle))

    def test_only_colour_and_animation_details_reach_the_encoder(self):
        from unittest import mock

        from apps.media.images import KEPT_INFO

        seen = []
        original = Image.Image.save

        def spy(image, *args, **kwargs):
            seen.append(set(image.info))
            return original(image, *args, **kwargs)

        for fmt in ("JPEG", "WEBP", "PNG"):
            with mock.patch.object(Image.Image, "save", spy):
                clean_image(make_image(fmt, exif=exif_with_gps(), **({"xmp": XMP} if fmt != "PNG" else {})))
        self.assertTrue(seen)
        for keys in seen:
            self.assertLessEqual(keys, set(KEPT_INFO))


class SizeLimitTests(StoreClientMixin, APITestCase):
    """The 1MB limit holds for the uploaded file and for the cleaned file that is kept."""

    def test_a_file_that_grows_when_saved_again_is_squeezed_back_under_the_limit(self):
        from apps.media.images import MAX_IMAGE_SIZE

        source = noisy_image("JPEG", (1600, 1600), 30)  # an upload under 1MB ...
        self.assertLess(len(source), MAX_IMAGE_SIZE)
        buffer = BytesIO()
        open_image(source).save(buffer, format="JPEG", quality=92, optimize=True)
        self.assertGreater(len(buffer.getvalue()), MAX_IMAGE_SIZE)  # ... that would be over it at high quality
        data, _, content_type = clean_image(source)
        self.assertLessEqual(len(data), MAX_IMAGE_SIZE)
        self.assertEqual((content_type, open_image(data).size), ("image/jpeg", (1600, 1600)))

    def test_the_media_library_keeps_a_file_within_the_limit(self):
        from apps.media.images import MAX_IMAGE_SIZE

        self.sign_up("size2@example.com", "Size Shop Two")
        source = noisy_image("JPEG", (1600, 1600), 30)
        r = self.client.post("/api/v1/media/", {"image": SimpleUploadedFile("big.jpg", source)}, format="multipart")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertLessEqual(r.json()["file_size"], MAX_IMAGE_SIZE)
        self.assertLessEqual(len(bytes(MediaAsset.objects.get().data)), MAX_IMAGE_SIZE)

    def test_a_file_that_cannot_fit_is_refused_with_the_usual_message(self):
        from rest_framework.exceptions import ValidationError

        with self.assertRaises(ValidationError) as caught:
            clean_image(noisy_image("JPEG", (2000, 2000), 30))  # too much detail to fit even at lower quality
        self.assertEqual(caught.exception.detail["image"], "Image is too large. Maximum size is 1MB per image.")

    def test_an_animated_webp_keeps_each_frames_display_time(self):
        frames = [Image.new("RGB", (16, 16), colour) for colour in ((255, 0, 0), (0, 255, 0), (0, 0, 255))]
        buffer = BytesIO()
        frames[0].save(buffer, format="WEBP", save_all=True, append_images=frames[1:], duration=[50, 200, 80], loop=3)
        image = open_image(clean_image(buffer.getvalue())[0])
        times = []
        for index in range(image.n_frames):
            image.seek(index)
            image.load()
            times.append(image.info["duration"])
        self.assertEqual((times, image.info["loop"]), ([50, 200, 80], 3))


class UploadHardeningTests(StoreClientMixin, APITestCase):
    def test_product_photos_count_towards_the_store_quota(self):
        from django.test import override_settings

        self.sign_up("quota@example.com", "Quota Shop")
        photo = make_image("PNG")
        with override_settings(MEDIA_STORE_QUOTA_BYTES=len(photo) + 10):
            first = self.client.post("/api/v1/catalog/products/images/",
                                     {"image": SimpleUploadedFile("a.png", photo)}, format="multipart")
            second = self.client.post("/api/v1/media/", {"image": SimpleUploadedFile("b.png", photo)},
                                      format="multipart")
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 400)
        self.assertIn("media library is full", str(second.json()))

    def test_stored_name_has_no_folders_or_control_characters(self):
        from apps.media.services import safe_name

        self.assertEqual(safe_name("../../etc/pass\x00wd.png"), "passwd.png")
        self.assertEqual(safe_name("C:\\x\\y.png"), "y.png")
        self.assertEqual(safe_name("\x01"), "image")

    def test_public_picture_is_served_nosniff_and_sandboxed(self):
        self.sign_up("sniff@example.com", "Sniff Shop")
        r = self.client.post("/api/v1/media/", {"image": SimpleUploadedFile("a.png", make_image("PNG"))},
                             format="multipart")
        url = "/s/_img/" + r.json()["id"]
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["X-Content-Type-Options"], "nosniff")
        self.assertIn("sandbox", resp["Content-Security-Policy"])

    def test_chunked_multipart_upload_is_refused(self):
        r = self.client.generic("POST", "/api/v1/media/", b"x", content_type="multipart/form-data; boundary=b",
                                CONTENT_LENGTH="")
        self.assertEqual(r.status_code, 411)
