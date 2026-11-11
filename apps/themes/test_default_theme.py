"""
The default theme file tree (default_provisioning), its cutoff, and the Edit-code rules that came with it.

    python manage.py test apps.themes.test_default_theme --settings=config.test_settings
"""
import json
from datetime import datetime, timedelta, timezone as dt_timezone
from io import StringIO

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.core.tests.test_flow import StoreClientMixin
from apps.tenancy.models import Store

from . import services
from .default_provisioning import MARK, cutoff, default_files, is_reserved, provision_default_files
from .models import StoreTheme, ThemeFile

IST = dt_timezone(timedelta(hours=5, minutes=30))
CUTOFF = datetime(2026, 11, 11, tzinfo=IST)
ON = {"DEFAULT_TREE_ENABLED": True, "DEFAULT_TREE_CUTOFF": CUTOFF}
OFF = {"DEFAULT_TREE_ENABLED": False, "DEFAULT_TREE_CUTOFF": CUTOFF}
# Virtual or mapped paths backed by custom_data: never written as theme files
VIRTUAL = (
    "config/settings_data.json", "assets/images.json", "templates/index.json", "templates/product.json",
    "templates/collection.json", "templates/blog.json", "templates/article.json", "templates/page.json",
    "templates/page.contact.json",
)


def make_store(name, created_at):
    store = Store.objects.create(store_name=name, store_slug=name.lower().replace(" ", "-"), status="active")
    Store.objects.filter(pk=store.pk).update(created_at=created_at)  # auto_now_add ignores the value on create
    store.refresh_from_db()
    return store


def files_of(store_theme):
    return dict(store_theme.theme.files.values_list("file_path", "content"))


class ProvisioningTest(TestCase):
    def setUp(self):
        services.clear_theme_caches()

    def open_theme(self, name, created_at, **settings):
        """A store created at `created_at` that opens its theme now (lazily, after the cutoff)."""
        store = make_store(name, created_at)
        with override_settings(**settings):
            return services.get_store_theme(store)

    def reference_files(self):
        """The files a theme gets today, without the default tree."""
        if not hasattr(self, "_reference"):
            self._reference = files_of(self.open_theme("Reference", CUTOFF + timedelta(days=1), **OFF))
        return self._reference

    def reference_paths(self):
        return set(self.reference_files())

    def assert_untouched(self, store_theme):
        self.assertEqual(set(files_of(store_theme)), self.reference_paths())
        self.assertNotIn(MARK, store_theme.theme.theme_data)

    def assert_full_tree(self, store_theme):
        files, reference = files_of(store_theme), self.reference_files()
        for path, content in default_files().items():
            if is_reserved(store_theme, path):
                self.assertNotIn(path, files)
            elif path in reference:
                self.assertEqual(files[path], reference[path], f"{path} must keep the copy it has without the tree")
            else:
                self.assertEqual(files[path], content, path)
        # nothing else changed: only default files were added
        self.assertEqual({p: c for p, c in files.items() if p in reference}, reference)
        self.assertEqual(store_theme.theme.theme_data[MARK], 1)

    # 1. before the cutoff: nothing, also when the theme is first opened after the cutoff
    def test_store_created_before_the_cutoff_gets_nothing(self):
        self.assert_untouched(self.open_theme("Old Shop", CUTOFF - timedelta(days=30), **ON))

    # 2. the boundary, in India Standard Time
    def test_boundary_is_midnight_ist(self):
        self.assert_untouched(self.open_theme("Last Old", datetime(2026, 11, 10, 23, 59, 59, tzinfo=IST), **ON))
        self.assert_full_tree(self.open_theme("First New", datetime(2026, 11, 11, 0, 0, 0, tzinfo=IST), **ON))
        # the same instant written in UTC
        self.assert_full_tree(self.open_theme("Utc New", datetime(2026, 11, 10, 18, 30, tzinfo=dt_timezone.utc), **ON))

    # 3. after the cutoff: the full tree
    def test_store_created_after_the_cutoff_gets_the_full_tree(self):
        theme = self.open_theme("New Shop", CUTOFF + timedelta(days=2), **ON)
        self.assert_full_tree(theme)
        for path in ("blocks/_card.liquid", "locales/en.default.json", "config/settings_schema.json",
                     "sections/header-group.json", "templates/cart.json", "assets/base.css", "layout/password.liquid"):
            self.assertIn(path, files_of(theme))

    # 4. the kill switch
    def test_disabled_gives_nothing_to_anyone(self):
        self.assert_untouched(self.open_theme("Off Shop", CUTOFF + timedelta(days=2), **OFF))
        with override_settings(DEFAULT_TREE_CUTOFF=CUTOFF):  # the setting left out: off
            if hasattr(settings, "DEFAULT_TREE_ENABLED"):
                del settings.DEFAULT_TREE_ENABLED
            self.assert_untouched(services.get_store_theme(make_store("Unset Shop", CUTOFF + timedelta(days=2))))

    # 5. idempotent, never overwrites, stores are isolated
    def test_idempotent_and_never_overwrites_and_isolated(self):
        a = self.open_theme("Shop A", CUTOFF + timedelta(days=1), **ON)
        b = self.open_theme("Shop B", CUTOFF + timedelta(days=1), **ON)
        self.assertNotEqual(a.theme_id, b.theme_id)
        a.theme.files.filter(file_path="blocks/text.liquid").update(content="mine")
        before = files_of(a)
        with override_settings(**ON):
            self.assertEqual(provision_default_files(a), [])
            services.clear_theme_caches()
            a = services.get_store_theme(a.store)
        self.assertEqual(files_of(a), before)
        self.assertEqual(files_of(b)["blocks/text.liquid"], default_files()["blocks/text.liquid"])

    # 6. colliding paths are never written
    def test_virtual_paths_are_never_written(self):
        theme = self.open_theme("Collide Shop", CUTOFF + timedelta(days=1), **ON)
        rows = set(theme.theme.files.values_list("file_path", flat=True))
        for path in VIRTUAL:
            self.assertNotIn(path, rows)
        # the code editor still shows the customizer's versions of them
        code = services.code_files(theme)
        self.assertEqual(code["config/settings_data.json"], services.settings_files(theme)["config/settings_data.json"])
        self.assertNotIn('"current": "Default"', code["config/settings_data.json"])

    def test_every_default_file_passes_the_editor_rules(self):
        from .theme_paths import check_new_path
        for path, content in default_files().items():
            self.assertEqual(services.clean_path(path), path)
            check_new_path(path)
            if path.endswith(".json"):
                json.loads(content)

    def test_cutoff_must_be_timezone_aware(self):
        with override_settings(DEFAULT_TREE_CUTOFF=datetime(2026, 11, 11)):
            with self.assertRaises(ImproperlyConfigured):
                cutoff()
        with override_settings(DEFAULT_TREE_CUTOFF="2026-11-11T00:00:00+05:30"):
            self.assertEqual(cutoff(), CUTOFF)

    def test_command_backfills_only_new_stores(self):
        self.reference_files()  # made now, so the runs below see it too (a new store opened while off)
        old =self.open_theme("Cmd Old", CUTOFF - timedelta(days=1), **OFF)
        new = self.open_theme("Cmd New", CUTOFF + timedelta(days=1), **OFF)  # opened while the switch was off
        out = StringIO()
        with override_settings(**ON):
            call_command("provision_default_theme", "--dry-run", stdout=out)
        self.assertIn("Cmd New", out.getvalue())
        self.assertNotIn("Cmd Old", out.getvalue())
        self.assertIn("2 store(s) would change", out.getvalue())  # Cmd New and Reference
        self.assertNotIn("blocks/text.liquid", files_of(new))  # a dry run writes nothing

        with override_settings(**ON):
            call_command("provision_default_theme", stdout=StringIO())
        new.theme.refresh_from_db()
        old.theme.refresh_from_db()
        self.assert_full_tree(new)
        self.assert_untouched(old)

        # a second run leaves the store alone, even after it deletes a default file
        new.theme.files.filter(file_path="blocks/button.liquid").delete()
        out = StringIO()
        with override_settings(**ON):
            call_command("provision_default_theme", stdout=out)
        self.assertIn("0 store(s) changed", out.getvalue())
        self.assertNotIn("blocks/button.liquid", files_of(new))

    def test_command_does_nothing_while_disabled(self):
        self.open_theme("Cmd Off", CUTOFF + timedelta(days=1), **OFF)
        out = StringIO()
        with override_settings(**OFF):
            call_command("provision_default_theme", stdout=out)
        self.assertIn("nothing to do", out.getvalue())


@override_settings(DEFAULT_TREE_ENABLED=True, DEFAULT_TREE_CUTOFF=datetime(2000, 1, 1, tzinfo=IST))
class EditCodeTest(StoreClientMixin, APITestCase):
    """Path rules, JSON checks, required files and reset, through the code editor's API."""

    def setUp(self):
        services.clear_theme_caches()
        self.token, self.store = self.sign_up("tree@example.com", "Tree Store")
        self.files()  # the theme is installed on first use

    def files(self):
        return {f["path"]: f["content"] for f in self.ok("get", "/api/v1/themes/files/")["files"]}

    def save(self, path, content="x", expect=200):
        return self.ok("post", "/api/v1/themes/files/", {"path": path, "content": content}, expect=expect)

    def reset(self, path, expect=200):
        return self.ok("post", "/api/v1/themes/files/", {"path": path, "reset": True}, expect=expect)

    def theme(self):
        return StoreTheme.objects.select_related("theme").get(store_id=self.store["storeId"], is_active=True)

    def test_new_store_has_the_tree_in_the_code_editor(self):
        files = self.files()
        self.assertIn("blocks/_card.liquid", files)
        self.assertIn("locales/en.default.json", files)

    # 7. path validation
    def test_traversal_and_absolute_paths_are_refused(self):
        for path in ("../x.liquid", "sections/../layout/theme.liquid", "C:/x.liquid", "/etc/passwd", "sections/.hidden.liquid"):
            self.save(path, expect=400)

    def test_new_files_only_in_theme_folders(self):
        self.save("custom/x.liquid", expect=400)
        self.save("x.liquid", expect=400)
        self.save("blocks/new.liquid")
        self.save("locales/fr.json", "{}")

    def test_existing_file_outside_the_folders_still_reads_and_saves(self):
        services._new_file(self.theme().theme, "custom/old.liquid", "old").save()
        self.assertEqual(self.files()["custom/old.liquid"], "old")
        self.save("custom/old.liquid", "new")
        self.assertEqual(self.files()["custom/old.liquid"], "new")
        rename = "/api/v1/themes/files/rename/"
        self.ok("post", rename, {"source": "custom/old.liquid", "target": "other/x.liquid"}, expect=400)
        self.ok("post", rename, {"source": "custom/old.liquid", "target": "snippets/old.liquid"})
        services._new_file(self.theme().theme, "custom2/a.liquid", "a").save()
        self.ok("post", rename, {"source": "custom2", "target": "misc"}, expect=400)
        self.ok("post", rename, {"source": "custom2", "target": "snippets/more"})

    def test_leading_underscore_only_for_blocks(self):
        self.save("blocks/_private.liquid")
        for path in ("sections/_x.liquid", "snippets/_x.liquid", "blocks/sub/_x.liquid", "_x.liquid", "blocks/__x.liquid"):
            self.save(path, expect=400)

    def test_plain_json_is_validated_on_save(self):
        for path in ("locales/en.default.json", "config/settings_schema.json", "sections/header-group.json",
                     "templates/cart.json"):
            r = self.save(path, "{not json", expect=400)
            self.assertIn("not valid JSON", r["content"][0] if isinstance(r["content"], list) else r["content"])
            self.save(path, json.dumps({"ok": True}))
        # the customizer's settings files keep their own checks
        self.save("templates/index.json", "{not json", expect=400)
        self.save("config/settings_data.json", "[]", expect=400)

    def test_required_files_cannot_be_deleted_or_moved(self):
        self.ok("delete", "/api/v1/themes/files/?path=config/settings_schema.json", expect=400)
        self.ok("delete", "/api/v1/themes/files/?path=layout/theme.liquid", expect=400)
        self.ok("post", "/api/v1/themes/files/rename/",
                {"source": "config/settings_schema.json", "target": "config/schema.json"}, expect=400)

    def test_reset_restores_the_default(self):
        self.save("blocks/text.liquid", "edited")
        self.reset("blocks/text.liquid")
        self.assertEqual(self.files()["blocks/text.liquid"], default_files()["blocks/text.liquid"])
        # a deleted default file comes back
        self.ok("delete", "/api/v1/themes/files/?path=blocks/button.liquid")
        self.reset("blocks/button.liquid")
        self.assertEqual(self.files()["blocks/button.liquid"], default_files()["blocks/button.liquid"])
        # a path the starter owns goes back to the starter's copy (the one the theme was installed with)
        path = "snippets/theme-css.liquid"
        self.save(path, "edited")
        self.reset(path)
        self.assertEqual(self.files()[path], services.starter_files()[path])

    def test_reset_refuses_virtual_and_unknown_paths(self):
        for path in VIRTUAL:
            self.reset(path, expect=400)
        self.reset("snippets/nope.liquid", expect=404)
        self.reset("../x.liquid", expect=400)

    def test_reset_needs_the_same_permissions_as_save(self):
        self.save("blocks/text.liquid", "edited")
        self.client.cookies.clear()
        self.client.credentials(HTTP_X_STORE_ID=self.store["storeId"])  # signed out
        r = self.client.post("/api/v1/themes/files/", {"path": "blocks/text.liquid", "reset": True}, format="json")
        self.assertIn(r.status_code, (401, 403))
        self.use(self.token, self.store["storeId"])
        self.assertEqual(self.files()["blocks/text.liquid"], "edited")


@override_settings(**ON)
class OlderStoresTest(StoreClientMixin, APITestCase):
    """Stores created before the cutoff: same files, same storefront."""

    def setUp(self):
        services.clear_theme_caches()
        with override_settings(**OFF):
            self.token, self.store = self.sign_up("old@example.com", "Old Store")
        Store.objects.filter(pk=self.store["storeId"]).update(created_at=CUTOFF - timedelta(days=60))

    def home(self):
        r = self.client.get(f"/s/{self.store['storeSlug']}/")
        self.assertEqual(r.status_code, 200)
        return r.content

    def theme(self):
        return StoreTheme.objects.select_related("theme").get(store_id=self.store["storeId"], is_active=True)

    # 8. the storefront HTML of an existing store doesn't change
    def test_storefront_html_is_byte_identical(self):
        with override_settings(**OFF):
            before = self.home()
            files_before = files_of(self.theme())
        services.clear_theme_caches()
        after = self.home()  # switch on, past the cutoff: the store is still older than the cutoff
        self.assertEqual(before, after)
        self.assertEqual(files_of(self.theme()), files_before)
        self.assertNotIn(MARK, self.theme().theme.theme_data)

    def test_extra_files_do_not_change_the_storefront(self):
        before = self.home()
        row = self.theme()
        ThemeFile.objects.bulk_create([
            services._new_file(row.theme, path, content)
            for path, content in default_files().items()
            if path not in files_of(row) and not is_reserved(row, path)
        ])
        services.clear_theme_caches()
        self.assertEqual(self.home(), before)

    def test_reset_has_no_default_tree_for_older_stores(self):
        self.ok("post", "/api/v1/themes/files/", {"path": "blocks/text.liquid", "reset": True}, expect=404)
        self.ok("post", "/api/v1/themes/files/", {"path": "snippets/theme-css.liquid", "reset": True})

    # 9. config/settings_schema.json is required, and older themes don't have it
    def test_theme_without_settings_schema_still_works(self):
        files = {f["path"] for f in self.ok("get", "/api/v1/themes/files/")["files"]}
        self.assertNotIn("config/settings_schema.json", files)
        self.home()
        # the starter refill still sees the theme as complete: a file the store deleted stays deleted
        self.ok("delete", "/api/v1/themes/files/?path=sections/featured-posts.liquid")
        services.clear_theme_caches()
        files = {f["path"] for f in self.ok("get", "/api/v1/themes/files/")["files"]}
        self.assertNotIn("sections/featured-posts.liquid", files)
        self.assertNotIn("config/settings_schema.json", files)
