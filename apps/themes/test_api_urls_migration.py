"""Migration 0018: saved theme files that call the storefront's JSON endpoints under /s/ move to /api/v1/storefront/."""
import hashlib
import importlib
from pathlib import Path

from django.apps import apps as django_apps
from django.test import TestCase

from .api_urls_rewrite import REWRITES, rewrite
from .models import Theme, ThemeFile
from .services import STARTER_UPGRADES, starter_files

migration = importlib.import_module("apps.themes.migrations.0018_storefront_api_v1_urls")


def _previous(path):
    """The starter file as it was before revision 64 (the rewrite undone)."""
    text = starter_files()[path]
    for old, new in REWRITES:
        text = text.replace(new, old)
    return text


class StorefrontApiUrlsMigrationTest(TestCase):
    def setUp(self):
        self.theme = Theme.objects.create(name="T", slug="t-api-urls")

    def add(self, path, content):
        return ThemeFile.objects.create(
            theme=self.theme, file_path=path, file_name=Path(path).name, directory=path.split("/")[0],
            file_type="liquid", content=content, file_size=len(content),
        )

    def test_the_starter_no_longer_calls_the_old_addresses(self):
        for path, content in starter_files().items():
            self.assertEqual(rewrite(content), content, path)

    def test_an_edited_copy_is_rewritten(self):
        edited = _previous("snippets/cart.liquid") + "\n<!-- mine -->\n"
        file = self.add("snippets/cart.liquid", edited)
        migration.forwards(django_apps, None)
        file.refresh_from_db()
        self.assertEqual(file.content, starter_files()["snippets/cart.liquid"] + "\n<!-- mine -->\n")
        self.assertNotIn("{{ shop.url }}/cart\" data-cart-session", file.content)
        self.assertIn('data-cart-base="{{ shop.api_url }}/cart"', file.content)
        self.assertIn('var base = "{{ shop.url }}";', file.content)

    def test_an_owners_own_file_with_the_old_calls_is_rewritten(self):
        mine = '<form action="{{ shop.url }}/newsletter" method="post"></form><script>navigator.sendBeacon("{{ shop.url }}/_track", b)</script>'
        file = self.add("sections/my-signup.liquid", mine)
        migration.forwards(django_apps, None)
        file.refresh_from_db()
        self.assertEqual(
            file.content,
            '<form action="{{ shop.api_url }}/newsletter/" method="post"></form>'
            '<script>navigator.sendBeacon("{{ shop.api_url }}/track/", b)</script>',
        )

    def test_an_untouched_copy_is_left_for_the_starter_upgrade(self):
        for path in ("snippets/cart.liquid", "layout/theme.liquid", "snippets/wishlist.liquid",
                     "snippets/product-card-blocks.liquid", "sections/main-product.liquid", "sections/newsletter.liquid"):
            with self.subTest(path=path):
                previous = _previous(path)
                self.assertIn(hashlib.sha256(previous.encode("utf-8")).hexdigest(), STARTER_UPGRADES[path])
                self.assertIn(hashlib.sha256(previous.replace("\n", "\r\n").encode("utf-8")).hexdigest(), STARTER_UPGRADES[path])
                file = self.add(path, previous)
                migration.forwards(django_apps, None)
                file.refresh_from_db()
                self.assertEqual(file.content, previous)
