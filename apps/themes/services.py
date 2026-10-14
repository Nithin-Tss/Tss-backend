"""
Store themes.

Every store edits its own copy of a theme: the first time a store's theme
is used, the starter theme (apps/themes/starter/) - or the shared theme the
store had installed - is copied into a private Theme row whose slug is
"store-<store id>". Code edits from the editor change only that copy.
Section settings (hero text, slides, ...) live in StoreTheme.custom_data.
"""
import hashlib
import json
import re
import time
from pathlib import Path
from types import SimpleNamespace

from django.db import IntegrityError, transaction
from rest_framework import exceptions

from . import section_sync as sync
from .models import StoreTheme, Theme, ThemeFile
from .schemas import PAGES, resolve, validate_custom_data

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
}
MAX_FILE_BYTES = 256 * 1024
MAX_FILES = 500
_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")

# Starter files that have newer versions. Each store has its own copy of the
# theme; a copy whose content still matches one of these earlier versions
# (SHA-256) was never edited, so it is replaced with the current starter file.
# Edited copies are left alone. Bump STARTER_REVISION when adding entries.
STARTER_REVISION = 13
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
    },
    "sections/product-carousel.liquid": {"0609acb6e9af090ad850a2f416005ac19ae7716ba1acb730c063864e7e6776d4"},
    "sections/rich-text.liquid": {
        "37ec79026e20ea886f79c150c660ffdfb6ca04b4614bb4878a6d263e1a58b10b",
        "8c115bfae9de1cfa75a8931aaafa91c3781837d743e9340440e22bbc95a6ebe8",  # revision 11: image shape, fit and focus
        "1a5b5ca920bb489f2047302eda3da0179b9b5b270c26e4b0a4d693f4afc61746",  # revision 12: before image description
    },
    "snippets/product-card.liquid": {
        "bc624bf8346fce0c29de1419f3d32ad1f66f052fe75ec9830685f219aee43eff",
        "386b5a2d330ef931de50ce9fa6f66cb49f0bae00b23582dfd4e27b6a84ad3723",
        "4af9942dd8108371e0d9225c91686e3b55a7aa04fad897345fbee7d6a1875dd3",
    },
    # Revision 4: "Sold out" / "In stock" / "Only a few left" on the product page
    # Revision 5: "Product information" accordion under the description
    "sections/main-product.liquid": {
        "18d5dd4556bc252eab73fafd20965a871816b44843a55adf743ae341493d1729",
        "3f5327f9d977c78b2e4e56eb89489be0ef544d06e258d91a2d8e3ca9d40f6211",
    },
    # Revision 7: announcement bar, header (logo, menu) and footer follow the Theme settings
    "layout/theme.liquid": {"5d3ad0d605d0aa39aeedaa75491d8bfa746cf9f41d6a65b508aa3651c869b8b9"},
    "snippets/header.liquid": {"6e36e4b3429052522d9242b9f5cbb83c98ae509b028f38bc0381169ff4c9dec1"},
    "snippets/footer.liquid": {"46998966bcd74dab088da530528ad25f6fac556a3cd7d2224bcdb728e6bf3573"},
    "snippets/styles.liquid": {
        "83a3d9accc2af4aa68a6a58ad84c8eb662acc3b6e422adb71458afc2d80b0079",
        "1c26147431b1ab810a18a33cb68cbc8955a685afa106874eed0ff2775d143f8e",  # revision 8: centred/right-aligned slide and hero text
    },
    # Revision 3: "Sold out" badge, "Load more", sort and pages on the all-products page
    # Revision 6: the collection's image and description above its products
    "sections/main-collection.liquid": {
        "80571d6bf2f85bfe5f4dc13210c1a575a1a0c57bc4a6b8bddaeb954241d294b5",
        "878a1da3a5ee2d1bd4e8d09dfd5c44f2509a75905e0177782b3e1f06ca018567",
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
        _add_synced_sections(current.theme)
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
    _add_synced_sections(installed.theme)
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
            file.content = new
            file.file_size = len(new.encode("utf-8"))
            file.save(update_fields=["content", "file_size", "updated_at"])

    theme.theme_data = {**theme_data, "starter_revision": STARTER_REVISION}
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


def _dump(value):
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


def settings_files(store_theme):
    """{path: JSON text} of the customizer's settings, with defaults filled in."""
    data = resolve(store_theme.custom_data)
    files = {SETTINGS_FILE: _dump(data["settings"])}
    for path, page in TEMPLATE_FILES.items():
        files[path] = _dump(data["templates"][page])
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


def _settings_conflict(path):
    """A path that is, or holds, a settings file: those can't be deleted, moved or replaced."""
    path = path.strip("/")
    if any(p == path or p.startswith(path + "/") for p in SETTINGS_FILES):
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
        page = TEMPLATE_FILES[path]
        if set(parsed) - {"order", "sections"}:
            raise exceptions.ValidationError({"content": f"{path} may only contain \"order\" and \"sections\"."})
        merged["templates"][page] = parsed

    try:
        store_theme.custom_data = validate_custom_data(merged)
    except exceptions.ValidationError as error:
        # Say which file the problem is in
        raise exceptions.ValidationError({"content": f"{path}: " + " ".join(str(m) for m in _messages(error.detail))})
    store_theme.save(update_fields=["custom_data", "updated_at"])
    return SimpleNamespace(file_path=path, file_size=len(content.encode("utf-8")))


def _messages(detail):
    if isinstance(detail, dict):
        return [m for value in detail.values() for m in _messages(value)]
    if isinstance(detail, (list, tuple)):
        return [m for value in detail for m in _messages(value)]
    return [detail]


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


def save_file(store_theme, path, content, base_version=None, client="", started_at=None):
    if isinstance(path, str) and path.strip().strip("/") == IMAGES_FILE:
        raise exceptions.ValidationError({"content": "assets/images.json lists your uploaded pictures and can't be edited."})

    if isinstance(path, str) and path.strip().strip("/") in SETTINGS_FILES:
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

    if path in sync.PATH_TO_SECTION:
        return save_synced_code(store_theme, sync.PATH_TO_SECTION[path], content, base_version=base_version, client=client,
                                started_at=started_at)

    theme = store_theme.theme
    existing = theme.files.filter(file_path=path).first()

    if existing:
        existing.content = content
        existing.file_size = len(content.encode("utf-8"))
        existing.save(update_fields=["content", "file_size", "updated_at"])
        return existing

    if theme.files.count() >= MAX_FILES:
        raise exceptions.ValidationError({"path": f"A theme can have at most {MAX_FILES} files."})

    new = _new_file(theme, path, content)
    new.save()
    return new


def _matching(theme, path):
    """The file at `path`, or every file inside the folder `path`."""
    files = list(theme.files.filter(file_path=path))
    return files or list(theme.files.filter(file_path__startswith=path + "/"))


@transaction.atomic
def delete_path(store_theme, path):
    path = (path or "").strip().strip("/")
    if path:
        _settings_conflict(path)
    files = _matching(store_theme.theme, path) if path else []

    if not files:
        raise exceptions.NotFound("No such file or folder.")

    required = [f.file_path for f in files if f.file_path in REQUIRED_FILES]
    if required:
        raise exceptions.ValidationError(f"{required[0]} is required by the theme and can't be deleted.")

    ThemeFile.objects.filter(pk__in=[f.pk for f in files]).delete()
    return len(files)


@transaction.atomic
def rename_path(store_theme, source, target):
    source = (source or "").strip().strip("/")
    theme = store_theme.theme
    if source:
        _settings_conflict(source)
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

# The starter's footer snippet before the footer became a section (sections/footer.liquid)
_ORIGINAL_FOOTER_SNIPPET = "dfef84bf0c7a8ad1f4be1895742e3df4b586303f40cdec1f6f667ee3ee34d426"


def _add_synced_sections(theme):
    """
    Themes made before the footer was a section get sections/footer.liquid - unless the
    store changed its footer snippet, which then keeps working as it is. Checked once.
    """
    data = theme.theme_data or {}
    if data.get("synced_sections_added"):
        return
    files = dict(
        theme.files.filter(file_path__in=["sections/footer.liquid", "snippets/footer.liquid"]).values_list("file_path", "content")
    )
    if "sections/footer.liquid" not in files:
        snippet = files.get("snippets/footer.liquid")
        if snippet is None or hashlib.sha256(snippet.encode("utf-8")).hexdigest() == _ORIGINAL_FOOTER_SNIPPET:
            _new_file(theme, "sections/footer.liquid", starter_files()["sections/footer.liquid"]).save()
    theme.theme_data = {**data, "synced_sections_added": True}
    theme.save(update_fields=["theme_data", "updated_at"])


def _seed(name, theme_settings, store_name):
    """A store's first values for a synced section: what it had set before (the footer's theme settings)."""
    if name != "footer":
        return None
    s = theme_settings
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


def _with_section(custom_data, name, values):
    return {**custom_data, "sections": {**(custom_data.get("sections") or {}), name: values}}


def synced_section(store_theme, name, files=None):
    """
    {path, content, schema, spec, values, version} of a synced section in this store's theme,
    or None (the theme has no such file, or its code is invalid: the old footer is used).
    A store's first read fills in its values (from its earlier footer settings) and saves them.
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
        conflict = (f"The code editor changed the footer at {_when(state['updated_at'])}. Your change here was saved on top; "
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
        conflict = (f"The customizer changed the footer at {_when(state['updated_at'])}. Your code was saved on top; "
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
