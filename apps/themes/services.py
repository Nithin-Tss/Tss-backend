"""
Store themes.

Every store edits its own copy of a theme: the first time a store's theme
is used, the starter theme (apps/themes/starter/) - or the shared theme the
store had installed - is copied into a private Theme row whose slug is
"store-<store id>". Code edits from the editor change only that copy.
Section settings (hero text, slides, ...) live in StoreTheme.custom_data.
"""
import copy
import hashlib
import json
import re
import time
from pathlib import Path
from types import SimpleNamespace

from django.db import IntegrityError, transaction
from liquid import Environment
from liquid.exceptions import LiquidError
from rest_framework import exceptions

from . import section_sync as sync
from django.utils.text import slugify

from .models import StorePage, StoreTheme, Theme, ThemeFile
from .schemas import (
    MAX_PAGE_TEMPLATES, PAGE_TEMPLATE, PAGES, SECTIONS as SECTION_SCHEMAS, STARTER_PAGE_TEMPLATES,
    default_template, resolve, validate_custom_data,
)

STARTER_DIR = Path(__file__).resolve().parent / "starter"
STARTER_VERSION = "1.0.0"

KEEP = ".keep"  # marks an empty folder (used by the code editor)
REQUIRED_FILES = {
    "layout/theme.liquid",
    "templates/index.liquid",
    "templates/product.liquid",
    "templates/collection.liquid",
    # Blog pages: a theme copied before they existed gets the starter's missing files once
    "templates/blog.liquid",
    "templates/article.liquid",
    # Pages (About us, ...)
    "templates/page.liquid",
    # Wishlist: one snippet with all its markup, the page and the script
    "snippets/wishlist.liquid",
}
MAX_FILE_BYTES = 256 * 1024
MAX_FILES = 500
_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")

# Starter files that have newer versions. Each store has its own copy of the
# theme; a copy whose content still matches one of these earlier versions
# (SHA-256) was never edited, so it is replaced with the current starter file.
# Edited copies are left alone. Bump STARTER_REVISION when adding entries.
STARTER_REVISION = 20
STARTER_UPGRADES = {
    "sections/hero.liquid": {
        "c713c5559e80c118413fad28450cbf01bbfbf5b145377320eb8b06ef73dc2c3d",
        "fd2985d83d849f0cf76976dda632fb6d3747cb551e84d2d8dab86f87585618a3",  # revision 13: image description, no unused button settings
    },
    "sections/slideshow.liquid": {
        "1676de060a502cce01bded76ca923306ffb596e2b5e4a824630b2398e5b1fb58",
        "32a46aa7269245969967e3c388440384f79a8c57e40060332a6a9ec047cc8732",  # revision 9: slide text options
        "67d575481eee66766ed8c904813a8ccf965c8d288376881e60e24a51cecab125",
        "8a3471bb606877eda22c385605c975c942aaa2b1b5bbfcbb9afbd57af24b029c",  # revision 12: slide alignment follows text alignment
        "06e6537da8a47e1d8e62fbc1615ca4636aee391c7627342f20707c95fc630313",  # revision 11: slide image fit and focus
        "004beb6dad2713d62967443126f69483bc99da33e548b62e325090c65ad57032",  # revision 10: slide layout, overlay, padding, video
        "a831a51ab4d6a82271abf32b7238911be9d920471b2b2490dc13dc1b83ad4fe6",  # revision 12: before slideshow accessibility and swipe
    },
    "sections/product-grid.liquid": {
        "b5bc74b7ad19eb53aa7935de4d44ecd407b7748445ba5a73ab65ada18b24e184",
        "197e7ad9e76dda5adcb0b762a69623d0ebb407baf1e3391edd794d26fa41275f",
        "d98591d946fbc257e35e491777052a64ddea4602a59047da771ad6245d2fbe59",  # revision 18: pass wishlist state to cards
        "32c98d43a2af2295fe65fa4034b5d96b987721004ccb72386511164b4502c2a5",  # revision 19: before the wishlist snippet
        "e6f1c4afcbd4615ea14a2fc5567c6c6af98ba5abdc1205314609b8e19ac9d08c",  # revision 19: same, with CRLF line endings
    },
    "sections/product-carousel.liquid": {
        "0609acb6e9af090ad850a2f416005ac19ae7716ba1acb730c063864e7e6776d4",
        "a79be796dde0feb9dd8fceda3a0b4e00c8c1cc3469e6066e02c506dfa8d88055",  # revision 16: no empty box for shoppers, pause on focus
        "684361444547f8952a62f6f5f86c2ddd3e4c86c56e60b717ff38ad3e148d3897",  # revision 18: pass wishlist state to cards
        "6c204389405b4509ce727a77be94de76318c5830102786803fb5c78be5a105bd",  # revision 19: before the wishlist snippet
        "4ec5499f34fe94154eb984801cdb0dd50045de966a963bcab0bb04961e994604",  # revision 19: same, with CRLF line endings
    },
    "sections/rich-text.liquid": {
        "37ec79026e20ea886f79c150c660ffdfb6ca04b4614bb4878a6d263e1a58b10b",
        "8c115bfae9de1cfa75a8931aaafa91c3781837d743e9340440e22bbc95a6ebe8",  # revision 11: image shape, fit and focus
        "1a5b5ca920bb489f2047302eda3da0179b9b5b270c26e4b0a4d693f4afc61746",  # revision 12: before image description
    },
    "snippets/product-card.liquid": {
        "bc624bf8346fce0c29de1419f3d32ad1f66f052fe75ec9830685f219aee43eff",
        "386b5a2d330ef931de50ce9fa6f66cb49f0bae00b23582dfd4e27b6a84ad3723",
        "4af9942dd8108371e0d9225c91686e3b55a7aa04fad897345fbee7d6a1875dd3",
        "1930541f426b1a8fb7c816b9c63b2afbf84379aa249d4871556299ba0e8f9dce",  # revision 15: price shown inline, no 'price' snippet
        "ec61086466c120f8ad0463a727c98777b07d9155bb9f167bd1224b51132fb4b7",  # revision 17: wishlist actions
        "211c4b936c465237301a2a6a4373ff684ab6995e6ad769adbdc2e185ee8077e3",  # revision 18: wishlist toggle state
        "78fc98475a47063470123fffaa31a6058ccfe5c25ff7420137ea708fad314a83",  # revision 19: before the wishlist snippet
        "3197e89478b76a5ece309e65af5d1990856e32f29d8229f3ad04bd38b17b50a5",  # revision 19: same, with CRLF line endings
        "b2d26d3b6c5b6958d032ef6c8d98250b02ae9575e9f2a4115bbe14272b035e3e",  # revision 20: the heart was on unless a section turned it off
        "4be9e7feb8659b7f7f792207a15d7adf0924e9a0795770fb541fd81ad81d0360",  # revision 20: same, with CRLF line endings
    },
    # Revision 4: "Sold out" / "In stock" / "Only a few left" on the product page
    # Revision 5: "Product information" accordion under the description
    "sections/main-product.liquid": {
        "18d5dd4556bc252eab73fafd20965a871816b44843a55adf743ae341493d1729",
        "3f5327f9d977c78b2e4e56eb89489be0ef544d06e258d91a2d8e3ca9d40f6211",
        "6c8de76dd784aabf1a7e13c29a4a30eed779f80e8b2824ace5b232109ffad098",  # revision 15: price shown inline, no 'price' snippet
        "e365056fcc5eb05b563e29fca7ab581c8158c4521faedfdc4acbe1701c887552",  # revision 16: option price/SKU fix, low-stock setting
        "e50ac85c4dd9c6e69b590632b6b0e2333fd9dff7521c7493e5fca6a8a39f97e5",  # revision 17: wishlist action
        "533aa3c35440fbb925de41f7e9fc62f01286439a5252bb4194ee449144e74817",  # revision 18: wishlist toggle state
        "d4c0b99cd99aeaf5718b6e828d101e4a83e3aa80e56dcee06afbc9c10fdc6432",  # revision 19: before the wishlist snippet
        "81382355092a70c8283ccde62122814ddd4b657663747c81de2003833709c464",  # revision 19: same, with CRLF line endings
    },
    # Revision 19: the header's wishlist heart is a "Wishlist icon" block that renders snippets/wishlist.liquid
    "sections/header.liquid": {
        "0447235775798d7cf5839634cfea58c3437e0571c024a24c300ac2f866a85d8b",  # revision 19: before the wishlist snippet
        "5fb084f88335fd7971a07f17eb38a52fa5d81749e4ea625a1678b23f088b5a19",  # revision 19: same, with CRLF line endings
    },
    # Revision 7: announcement bar, header (logo, menu) and footer follow the Theme settings
    "layout/theme.liquid": {"5d3ad0d605d0aa39aeedaa75491d8bfa746cf9f41d6a65b508aa3651c869b8b9"},
    "snippets/header.liquid": {"6e36e4b3429052522d9242b9f5cbb83c98ae509b028f38bc0381169ff4c9dec1"},
    "snippets/footer.liquid": {"46998966bcd74dab088da530528ad25f6fac556a3cd7d2224bcdb728e6bf3573"},
    "snippets/styles.liquid": {
        "83a3d9accc2af4aa68a6a58ad84c8eb662acc3b6e422adb71458afc2d80b0079",
        "1c26147431b1ab810a18a33cb68cbc8955a685afa106874eed0ff2775d143f8e",  # revision 8: centred/right-aligned slide and hero text
    },
    # Revision 14: "Read more" link on blog post cards
    "snippets/article-card.liquid": {"d2911b0825b5f87470be4b1118e6f31617ce75c1a327d18dc2a58d52b97e38f9"},
    # Revision 3: "Sold out" badge, "Load more", sort and pages on the all-products page
    # Revision 6: the collection's image and description above its products
    "sections/main-collection.liquid": {
        "80571d6bf2f85bfe5f4dc13210c1a575a1a0c57bc4a6b8bddaeb954241d294b5",
        "878a1da3a5ee2d1bd4e8d09dfd5c44f2509a75905e0177782b3e1f06ca018567",
        "0c7227f63f577546d1926e69940b5e949b3a440f24621438231d33147d3445a8",  # revision 16: escaped pagination links, sort only with 2+ products
        "a67252e6fa4f8a542b9581022bf8037ed5facf20b03603aaaf9114ee53d82035",  # revision 18: pass wishlist state to cards
        "920955c03a592c48c8eb8abda3a32656231eff12dc1caa4acb59a3096eb1c306",  # revision 19: before the wishlist snippet
        "f5741f0851fa01f20743d4d6c0cd8080b19ac1b31f1589af909cfc8a1043a866",  # revision 19: same, with CRLF line endings
    },
}


def starter_files():
    return {
        path.relative_to(STARTER_DIR).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(STARTER_DIR.rglob("*.liquid"))
    }


def private_slug(store):
    return f"store-{store.store_id}"


def _install(store, files, custom_data):
    theme, created = Theme.objects.get_or_create(
        slug=private_slug(store),
        defaults={
            "name": f"{store.store_name} theme",
            "description": "This store's own copy of its theme.",
            "category": "store",
            "version": STARTER_VERSION,
            "theme_data": {"store_id": str(store.store_id)},
        },
    )

    if created:
        ThemeFile.objects.bulk_create(
            [_new_file(theme, path, content) for path, content in files.items()]
        )

    StoreTheme.objects.filter(store=store, is_active=True).update(is_active=False)

    return StoreTheme.objects.create(
        store=store,
        theme=theme,
        installed_version=theme.version,
        custom_data=custom_data,
        is_active=True,
    )


def _add_missing_starter_files(theme):
    """
    Themes created before the starter theme existed lack its templates,
    sections and snippets. Add only the missing ones; never overwrite.
    """
    have = set(theme.files.values_list("file_path", flat=True))

    if REQUIRED_FILES <= have:
        return

    ThemeFile.objects.bulk_create(
        [
            _new_file(theme, path, content)
            for path, content in starter_files().items()
            if path not in have
        ]
    )


def get_store_theme(store):
    """The store's active theme, installing a private copy on first use."""
    current = (
        StoreTheme.objects.select_related("theme")
        .filter(store=store, is_active=True)
        .first()
    )

    if current and current.theme.slug == private_slug(store):
        _add_missing_starter_files(current.theme)
        _upgrade_untouched_starter_files(current.theme)
        _add_synced_sections(current.theme, current.custom_data)
        _add_wishlist_blocks(current)
        _offer_wishlist_icon_block(current.theme)
        _keep_wishlist_hearts(current)
        _add_page_templates(current)
        return current

    if current:
        files = {f.file_path: f.content for f in current.theme.files.all()}
        custom_data = current.custom_data
    else:
        files, custom_data = starter_files(), {}

    try:
        with transaction.atomic():
            installed = _install(store, files, custom_data)
    except IntegrityError:
        # Another request installed it at the same moment.
        installed = StoreTheme.objects.select_related("theme").get(store=store, is_active=True)

    _add_missing_starter_files(installed.theme)
    _upgrade_untouched_starter_files(installed.theme)
    _add_synced_sections(installed.theme, installed.custom_data)
    _add_wishlist_blocks(installed)
    _offer_wishlist_icon_block(installed.theme)
    _keep_wishlist_hearts(installed, new_store=current is None)
    _add_page_templates(installed)
    return installed


def _upgrade_untouched_starter_files(theme):
    """Bring never-edited starter files up to date (see STARTER_UPGRADES). Runs once per revision."""
    theme_data = theme.theme_data or {}
    if theme_data.get("starter_revision", 1) >= STARTER_REVISION:
        return

    current = starter_files()
    for file in theme.files.filter(file_path__in=STARTER_UPGRADES):
        digest = hashlib.sha256(file.content.encode("utf-8")).hexdigest()
        new = current.get(file.file_path)
        if new is not None and digest in STARTER_UPGRADES[file.file_path] and file.content != new:
            section = sync.PATH_TO_SECTION.get(file.file_path)
            if section:
                # A synced section (the header): keep the replaced file in its history, like any other change
                sync.record_version(theme, section, file.content, "upgrade")
                sync.invalidate(theme.pk, file.file_path)
            file.content = new
            file.file_size = len(new.encode("utf-8"))
            file.save(update_fields=["content", "file_size", "updated_at"])

    theme.theme_data = {**(theme.theme_data or {}), "starter_revision": STARTER_REVISION}
    theme.save(update_fields=["theme_data", "updated_at"])


def _add_wishlist_blocks(store_theme):
    """
    Once per store: the header's heart and the product page's heart became blocks ("Wishlist icon",
    "Wishlist heart"). A store that showed them keeps them, so nothing disappears. Only where its files
    render the blocks: a file the owner edited keeps its own markup and its own settings.
    """
    theme = store_theme.theme
    if (theme.theme_data or {}).get("wishlist_blocks"):
        return
    header_path, product_path = "sections/header.liquid", "sections/main-product.liquid"
    files = dict(theme.files.filter(file_path__in=[header_path, product_path]).values_list("file_path", "content"))

    saved = (store_theme.custom_data.get("sections") or {}).get("header")
    if saved is not None and "{% render 'wishlist', mode: 'icon'" in files.get(header_path, ""):
        settings = dict(saved.get("settings") or {})
        blocks = [b for b in saved.get("blocks") or [] if isinstance(b, dict)]
        if settings.pop("show_wishlist", True) and not any(b.get("type") == "wishlist" for b in blocks):
            blocks.append({"type": "wishlist", "settings": {"label": "Wishlist", "show_count": True}})
            try:
                save_section_values(store_theme, "header", {"settings": settings, "blocks": blocks}, client="upgrade")
            except exceptions.ValidationError:
                pass  # the header code is not valid right now: it keeps working as it is

    if "section.wishlist_heart" in files.get(product_path, ""):
        store_theme.refresh_from_db(fields=["custom_data"])
        templates = copy.deepcopy(store_theme.custom_data.get("templates") or {})
        changed = False
        for section in ((templates.get("product") or {}).get("sections") or {}).values():
            if section.get("type") == "main-product" and not any(
                b.get("type") == "wishlist_heart" for b in section.get("blocks") or []
            ):
                section["blocks"] = [*(section.get("blocks") or []), {"type": "wishlist_heart", "settings": {}}]
                changed = True
        if changed:
            store_theme.custom_data = {**store_theme.custom_data, "templates": templates}
            store_theme.save(update_fields=["custom_data", "updated_at"])

    theme.theme_data = {**(theme.theme_data or {}), "wishlist_blocks": 1}
    theme.save(update_fields=["theme_data", "updated_at"])


def _offer_wishlist_icon_block(theme):
    """
    Once per store: a header the owner edited, or one that came from an older starter, may not offer the
    "Wishlist icon" block, so it is missing from "Add block". It is added by inserting only: the block type
    into the schema's "blocks" list, and the loop that renders it unless the header has one already
    (see section_sync.add_wishlist_block). Nothing else in the header changes, the replaced code is kept
    in the header's history, and a header the insert cannot be made into safely is left exactly as it is.
    Done once: an owner who later removes the block type from the code does not get it back.
    """
    if (theme.theme_data or {}).get("wishlist_icon_offered"):
        return
    file = theme.files.filter(file_path="sections/header.liquid").first()
    patched = sync.add_wishlist_block(file.content) if file else None
    if patched is not None:
        sync.record_version(theme, "header", file.content, "upgrade")
        file.content = patched
        file.file_size = len(patched.encode("utf-8"))
        file.save(update_fields=["content", "file_size", "updated_at"])
        sync.invalidate(theme.pk, file.file_path)
    theme.theme_data = {**(theme.theme_data or {}), "wishlist_icon_offered": 1}
    theme.save(update_fields=["theme_data", "updated_at"])


# Sections that render product cards: their "Show wishlist heart" setting
CARD_SECTIONS = ("product-grid", "product-carousel", "main-collection")


def _keep_wishlist_hearts(store_theme, new_store=False):
    """
    Once per store: "Show wishlist heart" is now off unless the owner turns it on, and the product page
    has no "Wishlist heart" block unless the owner adds it. A store that exists already showed both, so
    it keeps them: show_wishlist = true is saved into every card section that never saved the setting,
    and the product page gets its block.

    Telling the stores apart:
    - a NEW store (its theme is created just now) has nothing to keep: it starts with the hearts off;
    - an existing store's section that has no "show_wishlist" in its saved settings never saved the
      setting, so it has been showing the heart (it was on by default): true is saved. A section that
      has the key, true or false, is the owner's choice and is left alone;
    - a page the owner never customized (no saved template) shows its default sections, hearts
      included: those default sections are saved as they are, with the heart on.
    Runs inside get_store_theme, i.e. before any request reads the settings, so the customizer never
    sees (and saves back) the new default for a section that was showing the heart.
    """
    theme = store_theme.theme
    if (theme.theme_data or {}).get("wishlist_hearts"):
        return
    if not new_store:
        product_file = theme.files.filter(file_path="sections/main-product.liquid").values_list("content", flat=True).first()
        renders_block = "section.wishlist_heart" in (product_file or "")
        templates = copy.deepcopy(store_theme.custom_data.get("templates") or {})
        changed = False
        for page in ("index", "collection", "product"):
            if page not in templates:
                templates[page] = {**default_template(page), "_default": True}
                changed = True
        for page in templates.values():
            was_default = page.pop("_default", False)
            for section in (page.get("sections") or {}).values():
                kind = section.get("type")
                if kind in CARD_SECTIONS:
                    if not isinstance(section.get("settings"), dict):
                        section["settings"] = {}
                    if "show_wishlist" not in section["settings"]:
                        section["settings"]["show_wishlist"] = True
                        changed = True
                elif kind == "main-product" and was_default and renders_block:
                    section["blocks"] = [*(section.get("blocks") or []), {"type": "wishlist_heart", "settings": {}}]
        if changed:
            store_theme.custom_data = {**store_theme.custom_data, "templates": templates}
            store_theme.save(update_fields=["custom_data", "updated_at"])
    theme.theme_data = {**(theme.theme_data or {}), "wishlist_hearts": 1}
    theme.save(update_fields=["theme_data", "updated_at"])


def theme_files(store_theme):
    """The theme's Liquid files (what the renderer loads)."""
    return {f.file_path: f.content for f in store_theme.theme.files.order_by("file_path")}


# ------------------------------------------------- settings as code
#
# The customizer's choices (store-wide settings, and each page's sections and
# blocks) are also shown in the code editor as JSON files, like Shopify's
# config/settings_data.json and templates/*.json. They are not stored as theme
# files: they are generated from StoreTheme.custom_data when the file list is
# read, and saving one validates it and writes it back to custom_data. So an
# edit in the customizer shows up in the code editor, and an edit in the code
# shows up in the customizer, from one source of truth.

SETTINGS_FILE = "config/settings_data.json"
TEMPLATE_FILES = {f"templates/{page}.json": page for page in PAGES}
SETTINGS_FILES = (SETTINGS_FILE, *TEMPLATE_FILES, "assets/images.json")
# Alternate page templates (page.contact, ...) have a settings file and a Liquid file each
PAGE_TEMPLATE_JSON = re.compile(r"^templates/(page\.[a-z0-9]+(?:-[a-z0-9]+)*)\.json$")
PAGE_TEMPLATE_LIQUID = re.compile(r"^templates/(page\.[a-z0-9]+(?:-[a-z0-9]+)*)\.liquid$")
PAGE_TEMPLATES_REVISION = 1
DEFAULT_PAGE_LIQUID = (
    "{%- comment -%}\n"
    "  Page template. `sections` is the HTML of this template's sections;\n"
    "  `page` is the page being shown. Add your own Liquid around {{ sections }}.\n"
    "{%- endcomment -%}\n"
    "{{ sections }}\n"
)


def page_template_keys(custom_data):
    """The alternate page templates the store has: ["page.about", "page.contact", ...]."""
    return sorted(key for key in resolve(custom_data)["templates"] if key not in PAGES)


def settings_paths(store_theme):
    """Every settings file of the theme, including the alternate page templates'."""
    return (*SETTINGS_FILES, *(f"templates/{key}.json" for key in page_template_keys(store_theme.custom_data)))


def _is_settings_path(path):
    return path in SETTINGS_FILES or bool(PAGE_TEMPLATE_JSON.match(path))


def _dump(value):
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


def settings_files(store_theme):
    """{path: JSON text} of the customizer's settings, with defaults filled in."""
    data = resolve(store_theme.custom_data)
    files = {SETTINGS_FILE: _dump(data["settings"])}
    for path, page in TEMPLATE_FILES.items():
        files[path] = _dump(data["templates"][page])
    for key in page_template_keys(store_theme.custom_data):
        files[f"templates/{key}.json"] = _dump(data["templates"][key])
    return files


IMAGES_FILE = "assets/images.json"


def images_file(store_theme):
    """The pictures uploaded in the customizer (stored in the database), listed as read-only JSON."""
    from .models import ThemeImage

    used = _dump(store_theme.custom_data)
    images = []
    for image in ThemeImage.objects.filter(store_id=store_theme.store_id).order_by("-created_at"):
        key = str(image.id)
        images.append({
            "id": key,
            "url": f"/s/_img/{key}",
            "type": image.content_type,
            "bytes": image.size,
            "uploaded": image.created_at.isoformat(timespec="seconds"),
            "in_use": key in used,
        })
    return _dump({
        "_note": "Pictures uploaded in the customizer. They are stored in the database; use the url in a Liquid file "
                 "or pick the picture in the customizer. This file is read-only.",
        "images": images,
    })


def code_files(store_theme):
    """Everything the code editor lists: the Liquid files, the settings files and the uploaded images."""
    return {**theme_files(store_theme), **settings_files(store_theme), IMAGES_FILE: images_file(store_theme)}


def _settings_conflict(store_theme, path):
    """A path that is, or holds, a settings file: those can't be deleted, moved or replaced."""
    path = path.strip("/")
    if any(p == path or p.startswith(path + "/") for p in settings_paths(store_theme)):
        raise exceptions.ValidationError(
            "Settings files are managed by the customizer. Edit what is inside them instead of deleting or renaming them."
        )


def _parse_json(path, content):
    try:
        return json.loads(content)
    except json.JSONDecodeError as error:
        raise exceptions.ValidationError(
            {"content": f"{path} is not valid JSON: {error.msg} (line {error.lineno}, column {error.colno})."}
        )


def _save_settings_file(store_theme, path, content):
    parsed = _parse_json(path, content)
    if not isinstance(parsed, dict):
        raise exceptions.ValidationError({"content": f"{path} must contain a JSON object."})

    merged = resolve(store_theme.custom_data)
    if path == SETTINGS_FILE:
        merged["settings"] = parsed
    else:
        page = TEMPLATE_FILES.get(path) or PAGE_TEMPLATE_JSON.match(path).group(1)
        if set(parsed) - {"order", "sections"}:
            raise exceptions.ValidationError({"content": f"{path} may only contain \"order\" and \"sections\"."})
        merged["templates"][page] = parsed

    before = resolve(store_theme.custom_data)["templates"]
    try:
        store_theme.custom_data = validate_custom_data(merged)
    except exceptions.ValidationError as error:
        # Say which file the problem is in
        raise exceptions.ValidationError({"content": f"{path}: " + " ".join(str(m) for m in _messages(error.detail))})
    store_theme.save(update_fields=["custom_data", "updated_at"])
    if PAGE_TEMPLATE_JSON.match(path):
        _ensure_page_file(store_theme.theme, page)
    templates_changed(store_theme, before, source="code")
    return SimpleNamespace(file_path=path, file_size=len(content.encode("utf-8")))


def _messages(detail):
    if isinstance(detail, dict):
        return [m for value in detail.values() for m in _messages(value)]
    if isinstance(detail, (list, tuple)):
        return [m for value in detail for m in _messages(value)]
    return [detail]


# ------------------------------------------------- alternate page templates (page.contact, ...)
#
# A page template is templates/page.<name>.liquid (the markup) plus its sections in
# custom_data["templates"]["page.<name>"] (shown as templates/page.<name>.json). Each store page
# picks one (StorePage.template; empty = the base Page template).

def _ensure_page_file(theme, key, content=DEFAULT_PAGE_LIQUID):
    path = f"templates/{key}.liquid"
    if not theme.files.filter(file_path=path).exists():
        _new_file(theme, path, content).save()


def _ensure_page_data(store_theme, key, template=None):
    """The template's sections in custom_data (the Page template's defaults when none are given)."""
    templates = dict(store_theme.custom_data.get("templates") or {})
    if key in templates:
        return
    templates[key] = template if template is not None else default_template(key)
    store_theme.custom_data = {**store_theme.custom_data, "templates": templates}
    store_theme.save(update_fields=["custom_data", "updated_at"])


def _forget_page_template(store_theme, key):
    """Its file is gone: drop its sections and send its pages back to the base Page template."""
    templates = dict(store_theme.custom_data.get("templates") or {})
    if key in templates:
        templates.pop(key)
        store_theme.custom_data = {**store_theme.custom_data, "templates": templates}
        store_theme.save(update_fields=["custom_data", "updated_at"])
    StorePage.objects.filter(store_id=store_theme.store_id, template=key).update(template="")


def template_label(key):
    return "Page" if key == "page" else key.split(".", 1)[1].replace("-", " ").capitalize()


def page_templates(store_theme):
    """[{key, label, path, liquid_path}] for the base Page template and every alternate one."""
    keys = ["page", *page_template_keys(store_theme.custom_data)]
    return [
        {"key": key, "label": template_label(key), "path": f"templates/{key}.json", "liquid_path": f"templates/{key}.liquid"}
        for key in keys
    ]


def _make_page_template(store_theme, key, header=""):
    """templates/<key>.liquid (the base Page's Liquid, under `header`) and the base Page's sections."""
    theme = store_theme.theme
    base = theme.files.filter(file_path="templates/page.liquid").values_list("content", flat=True).first()
    with transaction.atomic():
        _ensure_page_file(theme, key, header + (base if base is not None else DEFAULT_PAGE_LIQUID))
        _ensure_page_data(store_theme, key, copy.deepcopy(resolve(store_theme.custom_data)["templates"]["page"]))
    return key


def _template_exists(store_theme, key):
    return key in page_template_keys(store_theme.custom_data) or         store_theme.theme.files.filter(file_path=f"templates/{key}.liquid").exists()


def create_page_template(store_theme, name):
    """A new page template that starts as a copy of the base Page template (its Liquid and sections)."""
    slug = slugify(name or "")[:30].strip("-")
    if not slug or not PAGE_TEMPLATE.match(f"page.{slug}"):
        raise exceptions.ValidationError({"name": "Give the template a name (letters and numbers)."})
    key = f"page.{slug}"
    if _template_exists(store_theme, key):
        raise exceptions.ValidationError({"name": "A page template with that name already exists."})
    if len(page_template_keys(store_theme.custom_data)) >= MAX_PAGE_TEMPLATES:
        raise exceptions.ValidationError({"name": f"A theme can have at most {MAX_PAGE_TEMPLATES} page templates."})
    return _make_page_template(store_theme, key)


def create_template_for_page(store_theme, page):
    """
    A template of its own for a new store page: templates/page.<handle>.liquid (and .json) appear in
    the code editor and the customizer's page list. Returns its key, or "" when the theme is at its limit.
    """
    if len(page_template_keys(store_theme.custom_data)) >= MAX_PAGE_TEMPLATES:
        return ""
    base = slugify(page.handle or page.title or "")[:26].strip("-") or "page"
    slug, n = base, 2
    while not PAGE_TEMPLATE.match(f"page.{slug}") or _template_exists(store_theme, f"page.{slug}"):
        slug, n = f"{base}-{n}", n + 1
    key = f"page.{slug}"
    title = (page.title or "").replace("-%}", "").replace("{%", "").replace("%}", "")
    header = "\n".join([
        "{%- comment -%}",
        f"  Template of the page \"{title}\" (/pages/{page.handle}). Edit the Liquid below, or its sections",
        f"  in templates/{key}.json or the theme editor.",
        "{%- endcomment -%}",
        "",
    ])
    return _make_page_template(store_theme, key, header)


def _add_page_templates(store_theme):
    """
    The starter's page templates (About, Contact, FAQ, Policy) for themes that don't have them yet.
    Runs once per theme: a template the store deletes later is not brought back.
    """
    theme = store_theme.theme
    if (theme.theme_data or {}).get("page_templates", 0) >= PAGE_TEMPLATES_REVISION:
        return
    with transaction.atomic():
        locked = Theme.objects.select_for_update().get(pk=theme.pk)
        data = locked.theme_data or {}
        if data.get("page_templates", 0) < PAGE_TEMPLATES_REVISION:
            starter = starter_files()
            have = set(locked.files.values_list("file_path", flat=True))
            ThemeFile.objects.bulk_create([
                _new_file(locked, f"templates/{key}.liquid", starter[f"templates/{key}.liquid"])
                for key in STARTER_PAGE_TEMPLATES
                if f"templates/{key}.liquid" not in have and f"templates/{key}.liquid" in starter
            ])
            for key, template in STARTER_PAGE_TEMPLATES.items():
                _ensure_page_data(store_theme, key, copy.deepcopy(template))
            locked.theme_data = {**data, "page_templates": PAGE_TEMPLATES_REVISION}
            locked.save(update_fields=["theme_data", "updated_at"])
    theme.theme_data = locked.theme_data


# ---------------------------------------------------------------- files


def clean_path(path):
    if not isinstance(path, str):
        raise exceptions.ValidationError({"path": "Path is required."})

    path = path.strip().strip("/")
    parts = path.split("/")
    names_ok = all(_SEGMENT.match(p) for p in parts[:-1]) and (
        parts[-1] == KEEP or _SEGMENT.match(parts[-1])
    )

    if not path or len(path) > 500 or not names_ok:
        raise exceptions.ValidationError(
            {"path": "Use letters, numbers, dots, dashes and underscores, separated by /."}
        )
    if ".." in parts:
        raise exceptions.ValidationError({"path": "Path may not contain '..'."})

    name = parts[-1]
    if name != KEEP and not name.lower().endswith(".liquid"):
        raise exceptions.ValidationError({"path": "Only Liquid files (.liquid) are allowed."})

    return path


def _new_file(theme, path, content):
    directory, _, name = path.rpartition("/")
    return ThemeFile(
        theme=theme,
        file_path=path,
        file_name=name,
        directory=(directory.split("/")[0] if directory else "")[:100],
        file_type="keep" if name == KEEP else "liquid",
        content=content,
        is_required=path in REQUIRED_FILES,
        file_size=len(content.encode("utf-8")),
    )


# Product list code (the card every list renders, and the Product grid section): code that doesn't
# compile is refused (with its line), so the saved version keeps the grid, carousel and collection page working
CHECKED_FILES = {"snippets/product-card.liquid", "sections/product-grid.liquid"}


def _check_liquid(content):
    try:
        Environment().from_string(content)
    except LiquidError as error:
        token = getattr(error, "token", None)
        line = sync.line_of(content, token.start_index) if token is not None and token.start_index is not None else None
        message = f"Liquid error: {getattr(error, 'message', None) or error}"
        raise exceptions.ValidationError({"content": f"Line {line}: {message}" if line else message, "line": line})


def save_file(store_theme, path, content, base_version=None, client="", started_at=None):
    if isinstance(path, str) and path.strip().strip("/") == IMAGES_FILE:
        raise exceptions.ValidationError({"content": "assets/images.json lists your uploaded pictures and can't be edited."})

    if isinstance(path, str) and _is_settings_path(path.strip().strip("/")):
        path = path.strip().strip("/")
        if not isinstance(content, str):
            raise exceptions.ValidationError({"content": "Content must be text."})
        if len(content.encode("utf-8")) > MAX_FILE_BYTES:
            raise exceptions.ValidationError({"content": "File is larger than 256 KB."})
        return _save_settings_file(store_theme, path, content)

    path = clean_path(path)

    if not isinstance(content, str):
        raise exceptions.ValidationError({"content": "Content must be text."})
    if len(content.encode("utf-8")) > MAX_FILE_BYTES:
        raise exceptions.ValidationError({"content": "File is larger than 256 KB."})

    synced_name = sync.PATH_TO_SECTION.get(path)
    if synced_name and not (sync.SYNCED_FILES[synced_name]["kind"] == "template" and sync.is_legacy_template(content)):
        return save_synced_code(store_theme, sync.PATH_TO_SECTION[path], content, base_version=base_version, client=client,
                                started_at=started_at)

    page_template = PAGE_TEMPLATE_LIQUID.match(path)
    if path in CHECKED_FILES or page_template:
        _check_liquid(content)

    theme = store_theme.theme
    existing = theme.files.filter(file_path=path).first()

    if existing:
        existing.content = content
        existing.file_size = len(content.encode("utf-8"))
        existing.save(update_fields=["content", "file_size", "updated_at"])
        return existing

    if theme.files.count() >= MAX_FILES:
        raise exceptions.ValidationError({"path": f"A theme can have at most {MAX_FILES} files."})

    if page_template and page_template.group(1) not in page_template_keys(store_theme.custom_data) \
            and len(page_template_keys(store_theme.custom_data)) >= MAX_PAGE_TEMPLATES:
        raise exceptions.ValidationError({"path": f"A theme can have at most {MAX_PAGE_TEMPLATES} page templates."})

    new = _new_file(theme, path, content)
    new.save()
    if page_template:
        # A new templates/page.<name>.liquid is a new template in the customizer's page list
        _ensure_page_data(store_theme, page_template.group(1))
    return new


def _matching(theme, path):
    """The file at `path`, or every file inside the folder `path`."""
    files = list(theme.files.filter(file_path=path))
    return files or list(theme.files.filter(file_path__startswith=path + "/"))


@transaction.atomic
def delete_path(store_theme, path):
    path = (path or "").strip().strip("/")
    if path:
        _settings_conflict(store_theme, path)
    files = _matching(store_theme.theme, path) if path else []

    if not files:
        raise exceptions.NotFound("No such file or folder.")

    required = [f.file_path for f in files if f.file_path in REQUIRED_FILES]
    if required:
        raise exceptions.ValidationError(f"{required[0]} is required by the theme and can't be deleted.")

    removed_templates = [m.group(1) for f in files if (m := PAGE_TEMPLATE_LIQUID.match(f.file_path))]
    ThemeFile.objects.filter(pk__in=[f.pk for f in files]).delete()
    for key in removed_templates:
        _forget_page_template(store_theme, key)
    return len(files)


@transaction.atomic
def rename_path(store_theme, source, target):
    source = (source or "").strip().strip("/")
    theme = store_theme.theme
    if source:
        _settings_conflict(store_theme, source)
    files = _matching(theme, source) if source else []

    if not files:
        raise exceptions.NotFound("No such file or folder.")

    is_folder = files[0].file_path != source
    target = target.strip().strip("/") if isinstance(target, str) else target
    # A folder target is checked through a file name inside it.
    clean_path(f"{target}/{KEEP}" if is_folder else target)

    moves = {
        f.file_path: target + f.file_path[len(source):] if is_folder else target
        for f in files
    }

    if any(old in REQUIRED_FILES for old in moves):
        raise exceptions.ValidationError("Required theme files can't be moved or renamed.")
    if any(PAGE_TEMPLATE_LIQUID.match(old) or PAGE_TEMPLATE_LIQUID.match(new) for old, new in moves.items()):
        raise exceptions.ValidationError(
            "Page templates can't be renamed or moved. Create a new template and delete this one."
        )
    if theme.files.filter(file_path__in=list(moves.values())).exclude(pk__in=[f.pk for f in files]).exists():
        raise exceptions.ValidationError({"target": "A file with that name already exists."})

    for f in files:
        updated = _new_file(theme, moves[f.file_path], f.content)
        f.file_path, f.file_name, f.directory, f.file_type = (
            updated.file_path, updated.file_name, updated.directory, updated.file_type,
        )
        f.save(update_fields=["file_path", "file_name", "directory", "file_type", "updated_at"])

    return moves
    
def generate_css_variables(theme_data):
    tokens = theme_data.get("tokens", {})

    colors = tokens.get("colors", {})
    typography = tokens.get("typography", {})
    buttons = tokens.get("buttons", {})

    css_variables = []

    # Colors
    for name, value in colors.items():
        css_variables.append(f"--color-{name}: {value};")

    # Typography
    if typography.get("heading_font"):
        css_variables.append(
            f"--font-heading: {typography['heading_font']};"
        )

    if typography.get("body_font"):
        css_variables.append(
            f"--font-body: {typography['body_font']};"
        )

    # Buttons
    if buttons.get("radius"):
        css_variables.append(
            f"--button-radius: {buttons['radius']};"
        )

    return ":root {\n    " + "\n    ".join(css_variables) + "\n}"


# ------------------------------------------------- synced sections (code <-> customizer, see section_sync)

def _checked_sections(data):
    """{name: ORIGINALS_REVISION it was checked with}. Older themes stored a list (revision 1)."""
    checked = data.get("synced_sections")
    if isinstance(checked, dict):
        return checked
    if isinstance(checked, list):
        return {name: 1 for name in checked}
    return {"footer": 1} if data.get("synced_sections_added") else {}


def _add_synced_sections(theme, custom_data=None):
    """
    Themes made before a section became synced (sections/<name>.liquid) get its file -
    unless the store changed the snippet it replaces, which then keeps working as it is.
    A synced template (templates/index.liquid) that is still the starter's {{ sections }}
    file gets one {% section %} line per section of the store's page, in its order.
    Each is checked once per ORIGINALS_REVISION: when more starter versions are known to be
    untouched, themes checked before are checked again.
    """
    if not [n for n in sync.SYNCED_FILES if _checked_sections(theme.theme_data or {}).get(n, 0) < sync.ORIGINALS_REVISION]:
        return
    with transaction.atomic():
        # One request at a time: two at once would both add the file
        locked = Theme.objects.select_for_update().get(pk=theme.pk)
        data = locked.theme_data or {}
        checked = _checked_sections(data)
        todo = [n for n in sync.SYNCED_FILES if checked.get(n, 0) < sync.ORIGINALS_REVISION]
        if todo:
            paths = []
            for name in todo:
                config = sync.SYNCED_FILES[name]
                paths.append(config["path"])
                if config["kind"] == "section":
                    paths.append(f"snippets/{config['snippet']}.liquid")
                    if name == "header":
                        paths.append("snippets/styles.liquid")
            files = dict(locked.files.filter(file_path__in=paths).values_list("file_path", "content"))
            for name in todo:
                config = sync.SYNCED_FILES[name]
                if config["kind"] == "template":
                    _convert_template(locked, name, files.get(config["path"]), custom_data)
                    continue
                if config["path"] in files:
                    if name == "header":
                        file = locked.files.get(file_path=config["path"])
                        upgraded = _add_header_wishlist_setting(file.content)
                        if upgraded is not None:
                            sync.record_version(locked, name, file.content, "upgrade")
                            file.content = upgraded
                            file.file_size = len(upgraded.encode("utf-8"))
                            file.save(update_fields=["content", "file_size", "updated_at"])
                            sync.invalidate(locked.pk, config["path"])
                    if name == "header":
                        _upgrade_header_wishlist_styles(locked, files.get("snippets/styles.liquid"))
                    continue
                snippet = files.get(f"snippets/{config['snippet']}.liquid")
                if snippet is None or sync.is_original_snippet(name, snippet):
                    _new_file(locked, config["path"], starter_files()[config["path"]]).save()
                if name == "header":
                    _upgrade_header_wishlist_styles(locked, files.get("snippets/styles.liquid"))
            locked.theme_data = {
                **(locked.theme_data or data),
                "synced_sections": {**checked, **{n: sync.ORIGINALS_REVISION for n in todo}},
            }
            locked.save(update_fields=["theme_data", "updated_at"])
    theme.theme_data = locked.theme_data


def _upgrade_header_wishlist_styles(theme, content):
    if content is None:
        return
    upgraded = _add_header_wishlist_styles(content)
    if upgraded is None:
        return
    file = theme.files.get(file_path="snippets/styles.liquid")
    file.content = upgraded
    file.file_size = len(upgraded.encode("utf-8"))
    file.save(update_fields=["content", "file_size", "updated_at"])


def _add_header_wishlist_styles(content):
    """Upgrade wishlist icon sizing or add responsive styles in a standard theme stylesheet."""
    old_icon_rule = (
        "  .site-header__wishlist svg { width: 22px; height: 22px; fill: none; stroke: currentColor; "
        "stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round; }"
    )
    icon_rule = (
        "  .site-header__wishlist svg { width: 20px; height: 20px; fill: none; stroke: currentColor; "
        "stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; }"
    )
    if old_icon_rule in content:
        return content.replace(old_icon_rule, icon_rule, 1)
    if ".site-header__wishlist" in content:
        return None
    anchor = "  .site-header__nav a:hover { opacity: 1; }"
    if content.count(anchor) != 1:
        return None
    newline = "\r\n" if "\r\n" in content else "\n"
    styles = newline.join((
        "  .site-header__actions { display: flex; align-items: center; margin-left: auto; }",
        "  .site-header__wishlist {",
        "    display: inline-flex; align-items: center; justify-content: center; width: 40px; height: 40px;",
        "    padding: 0; border: 0; border-radius: 50%; background: transparent; color: inherit;",
        "    cursor: pointer; opacity: .8; transition: opacity .15s, background-color .15s;",
        "  }",
        "  .site-header__wishlist:hover { opacity: 1; background: var(--soft); }",
        "  .site-header__wishlist:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }",
        icon_rule,
        '  .site-header[data-layout="center"] .site-header__actions { margin-left: 0; }',
        "  @media (max-width: 640px) {",
        "    .site-header__inner { flex-wrap: wrap; }",
        "    .site-header__nav { order: 3; flex: 1 0 100%; justify-content: center; gap: 14px; }",
        "    .site-header__actions { order: 2; }",
        '    .site-header[data-layout="center"] .site-header__nav { order: 2; flex: initial; }',
        '    .site-header[data-layout="center"] .site-header__actions { order: 3; }',
        "  }",
    ))
    return content.replace(anchor, anchor + newline + styles, 1)


def _add_header_wishlist_setting(content):
    """Upgrade the wishlist icon and add its setting to standard synced headers."""
    if "{% render 'wishlist'" in content:
        return None  # already the newer header: its heart is the "Wishlist icon" block
    old_heart = (
        'M20.8 4.6a5.5 5.5 0 0 0-7.8 0L12 5.7l-1.1-1.1a5.5 5.5 0 0 0-7.8 '
        '7.8l1.1 1.1L12 21l7.8-7.5 1.1-1.1a5.5 5.5 0 0 0-.1-7.8Z'
    )
    heart = (
        'M20.84 4.61a5.5 5.5 0 0 0-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 0 0-7.78 '
        '7.78l1.06 1.06L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 0 0 0-7.78Z'
    )
    has_old_heart = old_heart in content
    if has_old_heart:
        content = content.replace(old_heart, heart, 1)
    if "show_wishlist" in content:
        upgraded = re.sub(
            r'<button class="site-header__wishlist" type="button" aria-label="Wishlist" title="Wishlist">(.*?)</button>',
            r'<a class="site-header__wishlist" href="{{ shop.url }}/wishlist/" aria-label="Wishlist" title="Wishlist">\1</a>',
            content,
            count=1,
            flags=re.DOTALL,
        )
        if "data-wishlist-count" not in upgraded:
            upgraded = re.sub(
                r'(<a class="site-header__wishlist"[^>]*>.*?</svg>)(\s*)(</a>)',
                r'\1\2<span class="site-header__wishlist-count" data-wishlist-count aria-live="polite">{{ wishlist_count }}</span>\2\3',
                upgraded,
                count=1,
                flags=re.DOTALL,
            )
        return upgraded if upgraded != content else (content if has_old_heart else None)

    _, schema_text, schema_start = sync.split(content)
    if schema_text is None:
        return None
    newline = "\r\n" if "\r\n" in content else "\n"
    markup_anchor = f'    {{%- endif -%}}{newline}  </div>{newline}</header>'
    schema_anchor = re.compile(
        r'^([ \t]*\{"type": "checkbox", "id": "show_nav", "label": "Show the navigation menu", '
        r'"default": (?:true|false)\},)[ \t]*\r?$',
        re.MULTILINE,
    )
    markup_prefix = content[:schema_start]
    if markup_prefix.count(markup_anchor) != 1 or len(schema_anchor.findall(schema_text)) != 1:
        return None

    wishlist_markup = newline.join((
        '    {%- if s.show_wishlist -%}',
        '      <div class="site-header__actions">',
        '        <a class="site-header__wishlist" href="{{ shop.url }}/wishlist/" aria-label="Wishlist" title="Wishlist">',
        '          <svg viewBox="0 0 24 24" aria-hidden="true">',
        f'            <path d="{heart}" />',
        '          </svg>',
        '          <span class="site-header__wishlist-count" data-wishlist-count aria-live="polite">{{ wishlist_count }}</span>',
        '        </a>',
        '      </div>',
        '    {%- endif -%}',
    )) + newline
    upgraded_markup = markup_prefix.replace(markup_anchor, wishlist_markup + markup_anchor, 1)
    wishlist_setting = (
        '{"type": "checkbox", "id": "show_wishlist", "label": "Show Wishlist", "default": true},'
    )
    upgraded_schema = schema_anchor.sub(
        lambda match: match.group(1) + newline + "    " + wishlist_setting,
        schema_text,
        count=1,
    )
    return upgraded_markup + upgraded_schema + content[schema_start + len(schema_text):]


def _convert_template(theme, name, content, custom_data):
    """An untouched starter {{ sections }} template becomes one section line per section, in the store's order."""
    config = sync.SYNCED_FILES[name]
    if content is None or not sync.is_original_snippet(name, content):
        return
    entries = sync.template_entries(resolve(custom_data or {})["templates"][config["page"]])
    new = sync.template_from_entries(entries, "\r\n" if "\r\n" in content else "\n")
    sync.record_version(theme, name, content, "upgrade")
    theme.files.filter(file_path=config["path"]).update(content=new, file_size=len(new.encode("utf-8")))


def _seed_header(s, store_name):
    """The header's Theme settings (Header and navigation) as the header section's values."""
    blocks = []
    for line in str(s.get("header_menu") or "").splitlines():
        label, _, url = line.partition("|")
        label, url = label.strip(), url.strip() or label.strip()
        if label:
            blocks.append({"type": "link", "settings": {"label": label, "url": url}})
    # The header has always shown the wishlist heart: it is now a block the owner can remove
    blocks.append({"type": "wishlist", "settings": {"label": "Wishlist", "show_count": True}})
    return {
        "settings": {
            "logo_text": s.get("header_logo_text", ""), "logo_image": s.get("header_logo_url", ""),
            "logo_height": s.get("header_logo_height", 36), "layout": s.get("header_layout", "left"),
            "show_nav": s.get("header_show_nav", True), "collections": s.get("header_collections", 0),
            "sticky": s.get("header_sticky", True), "border": s.get("header_border", True),
            "background": s.get("header_bg", ""), "text_color": s.get("header_text", ""),
        },
        "blocks": blocks,
    }


def _seed_footer(s, store_name):
    """The footer's Theme settings as the footer section's values."""
    blocks = []
    if s.get("footer_text"):
        blocks.append({"type": "text", "settings": {"heading": store_name, "text": s["footer_text"]}})
    if s.get("footer_show_links", True):
        blocks.append({"type": "links", "settings": {"heading": "", "menu": s.get("footer_menu") or "Home | /\nShop all | /collections/all"}})
    social = {k: s.get(f"footer_{k}", "") for k in ("instagram", "facebook", "x", "tiktok", "youtube", "pinterest", "linkedin")}
    if any(social.values()):
        blocks.append({"type": "social", "settings": social})
    return {
        "settings": {"copyright": s.get("footer_copyright", ""), "background": s.get("footer_bg", ""),
                     "text_color": s.get("footer_text_color", "")},
        "blocks": blocks,
    }


# A store's first values for a synced section: what it had set before in Theme settings,
# so nothing is lost when a section becomes synced. One entry per section.
SEEDS = {"header": _seed_header, "footer": _seed_footer}


def _seed(name, theme_settings, store_name):
    seed = SEEDS.get(name)
    return seed(theme_settings, store_name) if seed else None


def _with_section(custom_data, name, values):
    return {**custom_data, "sections": {**(custom_data.get("sections") or {}), name: values}}


def synced_section(store_theme, name, files=None):
    """
    {path, content, schema, spec, values, version} of a synced section in this store's theme,
    or None (the theme has no such file, or its code is invalid: the old snippet is used).
    A store's first read fills in its values (from its earlier Theme settings) and saves them.
    """
    path = sync.SYNCED_SECTIONS[name]["path"]
    theme = store_theme.theme
    if files is not None:
        content = files.get(path)
    else:
        content = theme.files.filter(file_path=path).values_list("content", flat=True).first()
    if content is None:
        return None
    try:
        schema = sync.parse_cached(theme.pk, name, content)
    except sync.SectionCodeError:
        return None
    spec = sync.spec_of(name, schema)
    saved = (store_theme.custom_data.get("sections") or {}).get(name)
    if saved is None:
        seed = _seed(name, resolve(store_theme.custom_data)["settings"], store_theme.store.store_name)
        values = sync.clean_values(spec, seed) if seed else sync.defaults_of(spec, schema)
        store_theme.custom_data = _with_section(store_theme.custom_data, name, values)
        store_theme.save(update_fields=["custom_data", "updated_at"])
    else:
        values = sync.clean_values(spec, saved)
    return {"path": path, "content": content, "schema": schema, "spec": spec, "values": values,
            **sync.section_state(theme, name)}


def synced_sections(store_theme, files=None):
    found = {}
    for name in sync.SYNCED_SECTIONS:
        section = synced_section(store_theme, name, files)
        if section:
            found[name] = section
    return found


def _label(name):
    return sync.SYNCED_FILES[name]["label"].lower()


def _when(ts):
    return time.strftime("%H:%M:%S", time.localtime(ts)) if ts else "earlier"


def _publish(store_theme, name, source, version, values, spec, code, client, started_at=None):
    sync.EVENTS.publish(store_theme.store_id, {
        "type": "section", "section": name, "source": source, "client": client or "",
        "version": version, "values": values, "spec": spec, "code": code, "at": time.time() * 1000,
        # When the change was made in the browser (ms), for the "change made -> preview updated" times
        "started_at": started_at,
    })


@transaction.atomic
def save_section_values(store_theme, name, raw, client="", base_version=None, started_at=None):
    """
    Blocks -> Code: the customizer saved the section's values. They are stored, and
    written into the file's schema (defaults and block order) - only those lines change.
    The newest change wins; if the code editor changed the file since, the reply says so.
    """
    current = synced_section(store_theme, name)
    if current is None:
        raise exceptions.ValidationError({"sections": f"This theme has no {sync.SYNCED_SECTIONS[name]['path']} to save into."})
    try:
        values = sync.clean_values(current["spec"], raw, strict=True)
    except ValueError as error:
        raise exceptions.ValidationError({"sections": str(error)})

    theme = Theme.objects.select_for_update().get(pk=store_theme.theme_id)
    file = theme.files.get(file_path=current["path"])
    state = sync.section_state(theme, name)
    conflict = None
    if base_version is not None and base_version != state["version"] and state["source"] in ("code", "restore"):
        conflict = (f"The code editor changed the {_label(name)} at {_when(state['updated_at'])}. Your change here was saved on top; "
                    "the code editor's version is kept in its History.")

    code = sync.write_values(file.content, values)
    version = state["version"]
    if code != file.content:
        version = sync.record_version(theme, name, file.content, "customizer")
        file.content = code
        file.file_size = len(code.encode("utf-8"))
        file.save(update_fields=["content", "file_size", "updated_at"])
        theme.save(update_fields=["theme_data", "updated_at"])
        sync.invalidate(theme.pk, current["path"])

    store_theme.custom_data = _with_section(store_theme.custom_data, name, values)
    store_theme.save(update_fields=["custom_data", "updated_at"])
    _publish(store_theme, name, "customizer", version, values, current["spec"], code, client, started_at)
    return {"version": version, "values": values, "conflict": conflict}


@transaction.atomic
def save_synced_code(store_theme, name, content, base_version=None, client="", source="code", started_at=None):
    if sync.SYNCED_FILES[name]["kind"] == "template":
        return save_template_code(store_theme, name, content, base_version, client, source, started_at)
    return _save_section_code(store_theme, name, content, base_version, client, source, started_at)


def _save_section_code(store_theme, name, content, base_version=None, client="", source="code", started_at=None):
    """
    Code -> Blocks: the section's file was saved in Edit code (or restored). Its code is
    checked first - invalid Liquid or schema JSON is refused with its line, and the saved
    version keeps working. Settings and blocks are rebuilt: new ones appear, removed ones
    go, ids that stay keep their values.
    """
    path = sync.SYNCED_SECTIONS[name]["path"]
    try:
        new_schema = sync.parse(content, path)
    except sync.SectionCodeError as error:
        raise exceptions.ValidationError({"content": str(error), "line": error.line})

    theme = Theme.objects.select_for_update().get(pk=store_theme.theme_id)
    file = theme.files.filter(file_path=path).first()
    old_schema = None
    if file is not None:
        try:
            old_schema = sync.parse(file.content, path)
        except sync.SectionCodeError:
            old_schema = None

    state = sync.section_state(theme, name)
    conflict = None
    if base_version is not None and base_version != state["version"] and state["source"] == "customizer":
        conflict = (f"The customizer changed the {_label(name)} at {_when(state['updated_at'])}. Your code was saved on top; "
                    "the customizer's version is kept in History.")

    saved = (store_theme.custom_data.get("sections") or {}).get(name)
    values = sync.merge_for_new_code(old_schema, new_schema, saved, name)
    version = sync.record_version(theme, name, file.content if file else None, source)
    if file is None:
        file = _new_file(theme, path, content)
        file.save()
    else:
        file.content = content
        file.file_size = len(content.encode("utf-8"))
        file.save(update_fields=["content", "file_size", "updated_at"])
    theme.save(update_fields=["theme_data", "updated_at"])
    sync.invalidate(theme.pk, path)

    store_theme.custom_data = _with_section(store_theme.custom_data, name, values)
    store_theme.save(update_fields=["custom_data", "updated_at"])
    spec = sync.spec_of(name, new_schema)
    _publish(store_theme, name, source, version, values, spec, content, client, started_at)
    return SimpleNamespace(file_path=path, file_size=file.file_size,
                           sync={"section": name, "version": version, "conflict": conflict, "values": values, "spec": spec})


def restore_section(store_theme, name, index, client=""):
    """Put back a version from the section's history (it becomes the newest version)."""
    content = sync.history_content(store_theme.theme, name, index)
    if content is None:
        raise exceptions.NotFound("That version is no longer kept.")
    return save_synced_code(store_theme, name, content, client=client, source="restore")


# ------------------------------------------------- synced page templates (templates/index.liquid)

def synced_template(store_theme, name, files=None):
    """{path, content, version, ..., template} of a synced template, or None (no file, or a {{ sections }} one)."""
    config = sync.SYNCED_FILES[name]
    theme = store_theme.theme
    if files is not None:
        content = files.get(config["path"])
    else:
        content = theme.files.filter(file_path=config["path"]).values_list("content", flat=True).first()
    if content is None or sync.is_legacy_template(content):
        return None
    return {"path": config["path"], "content": content, **sync.section_state(theme, name),
            "template": resolve(store_theme.custom_data)["templates"][config["page"]]}


def synced_files(store_theme):
    """Everything synced with the customizer in this theme (for the customizer and the code editor)."""
    found = {}
    for name, section in synced_sections(store_theme).items():
        found[name] = {"kind": "section", "path": section["path"], "label": sync.SYNCED_FILES[name]["label"],
                       "spec": section["spec"], "values": section["values"], "version": section["version"]}
    for name in sync.SYNCED_TEMPLATES:
        template = synced_template(store_theme, name)
        if template:
            found[name] = {"kind": "template", "path": template["path"], "label": sync.SYNCED_FILES[name]["label"],
                           "page": sync.SYNCED_FILES[name]["page"], "template": template["template"],
                           "version": template["version"]}
    return found


def _publish_template(store_theme, name, source, version, template, code, client, started_at=None):
    sync.EVENTS.publish(store_theme.store_id, {
        "type": "section", "kind": "template", "section": name, "page": sync.SYNCED_FILES[name]["page"],
        "source": source, "client": client or "",
        "version": version, "template": template, "code": code, "at": time.time() * 1000, "started_at": started_at,
    })


def _remember_removed(theme, name, before, after):
    """Sections taken off the page are kept aside, so putting their line back restores their values."""
    gone = {sid: section for sid, section in before["sections"].items() if sid not in after["sections"]}
    if gone:
        removed = sync.removed_sections(theme, name)
        removed.update(gone)
        sync.set_removed_sections(theme, name, removed)


@transaction.atomic
def templates_changed(store_theme, before, client="", base_versions=None, started_at=None, source="customizer"):
    """
    Blocks -> Code: the page's sections were saved (customizer, or templates/<page>.json).
    If their order, list or hidden state changed, the template's section lines are rewritten -
    only those lines. The newest change wins; if Edit code changed the file since, the reply says so.
    Returns {name: {"version", "conflict"}} for the templates whose file changed.
    """
    results = {}
    base_versions = base_versions or {}
    for name, config in sync.SYNCED_TEMPLATES.items():
        page = config["page"]
        after = resolve(store_theme.custom_data)["templates"][page]
        if sync.template_entries(before[page]) == sync.template_entries(after):
            continue
        theme = Theme.objects.select_for_update().get(pk=store_theme.theme_id)
        file = theme.files.filter(file_path=config["path"]).first()
        if file is None or sync.is_legacy_template(file.content):
            continue
        _remember_removed(theme, name, before[page], after)
        try:
            code = sync.write_template(file.content, sync.template_entries(after))
        except sync.SectionCodeError:
            continue  # the file's own lines can't be read: it keeps them until it is fixed in Edit code
        state = sync.section_state(theme, name)
        base = base_versions.get(name)
        conflict = None
        if base is not None and base != state["version"] and state["source"] in ("code", "restore"):
            conflict = (f"The code editor changed the {_label(name)} at {_when(state['updated_at'])}. Your change here was saved "
                        "on top; the code editor's version is kept in its History.")
        version = state["version"]
        if code != file.content:
            version = sync.record_version(theme, name, file.content, source)
            file.content = code
            file.file_size = len(code.encode("utf-8"))
            file.save(update_fields=["content", "file_size", "updated_at"])
        theme.save(update_fields=["theme_data", "updated_at"])
        _publish_template(store_theme, name, source, version, after, code, client, started_at)
        results[name] = {"version": version, "conflict": conflict}
    return results


@transaction.atomic
def save_template_code(store_theme, name, content, base_version=None, client="", source="code", started_at=None):
    """
    Code -> Blocks: templates/<page>.liquid was saved in Edit code (or restored). Its section
    lines become the page's sections, in that order: known ids keep their values, a new id
    with a type is a new section, and a section whose line is gone is kept aside (its line
    coming back restores it). Invalid lines or Liquid are refused with their line number.
    """
    config = sync.SYNCED_FILES[name]
    path, page = config["path"], config["page"]
    try:
        tags = sync.parse_template(content, path)
    except sync.SectionCodeError as error:
        raise exceptions.ValidationError({"content": str(error), "line": error.line})

    theme = Theme.objects.select_for_update().get(pk=store_theme.theme_id)
    store_theme.refresh_from_db(fields=["custom_data"])
    current = resolve(store_theme.custom_data)["templates"][page]
    removed = sync.removed_sections(theme, name)

    sections = {}
    for tag in tags:
        sid, kind = tag["id"], tag["type"]
        known = current["sections"].get(sid) or removed.get(sid)
        if known is not None:
            if kind and kind != known["type"]:
                raise exceptions.ValidationError({
                    "content": f"Line {tag['line']}: \"{sid}\" is a {SECTION_SCHEMAS[known['type']]['name']} section. "
                               "To add a different one, give it a new id.", "line": tag["line"]})
            section = copy.deepcopy(known)
        elif not kind:
            raise exceptions.ValidationError({
                "content": f"Line {tag['line']}: there is no section \"{sid}\". To add one, give it a type, "
                           f"e.g. {{% section '{sid}', type: 'rich-text' %}}.", "line": tag["line"]})
        elif kind not in SECTION_SCHEMAS or page not in SECTION_SCHEMAS[kind]["pages"]:
            usable = ", ".join(t for t, sc in SECTION_SCHEMAS.items() if page in sc["pages"])
            raise exceptions.ValidationError({
                "content": f"Line {tag['line']}: \"{kind}\" can't be used on this page. Types: {usable}.", "line": tag["line"]})
        else:
            section = {"type": kind, "settings": {}, **({"blocks": []} if "blocks" in SECTION_SCHEMAS[kind] else {})}
        section["hidden"] = tag["hidden"]
        sections[sid] = section

    merged = resolve(store_theme.custom_data)
    merged.pop("sections", None)
    merged["templates"][page] = {"order": [t["id"] for t in tags], "sections": sections}
    try:
        clean = validate_custom_data(merged)
    except exceptions.ValidationError as error:
        raise exceptions.ValidationError({"content": f"{path}: " + " ".join(str(m) for m in _messages(error.detail))})
    if store_theme.custom_data.get("sections"):
        clean["sections"] = store_theme.custom_data["sections"]

    state = sync.section_state(theme, name)
    conflict = None
    if base_version is not None and base_version != state["version"] and state["source"] == "customizer":
        conflict = (f"The customizer changed the {_label(name)} at {_when(state['updated_at'])}. Your code was saved on top; "
                    "the customizer's version is kept in History.")

    after = resolve(clean)["templates"][page]
    file = theme.files.filter(file_path=path).first()
    version = sync.record_version(theme, name, file.content if file else None, source)
    _remember_removed(theme, name, current, after)
    # Sections that are back on the page no longer wait aside
    sync.set_removed_sections(theme, name, {k: v for k, v in sync.removed_sections(theme, name).items() if k not in sections})
    if file is None:
        file = _new_file(theme, path, content)
        file.save()
    else:
        file.content = content
        file.file_size = len(content.encode("utf-8"))
        file.save(update_fields=["content", "file_size", "updated_at"])
    theme.save(update_fields=["theme_data", "updated_at"])

    store_theme.custom_data = clean
    store_theme.save(update_fields=["custom_data", "updated_at"])
    _publish_template(store_theme, name, source, version, after, content, client, started_at)
    return SimpleNamespace(file_path=path, file_size=file.file_size,
                           sync={"section": name, "kind": "template", "version": version, "conflict": conflict, "template": after})
