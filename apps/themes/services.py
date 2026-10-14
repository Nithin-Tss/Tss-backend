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
from pathlib import Path
from types import SimpleNamespace

from django.db import IntegrityError, transaction
from rest_framework import exceptions

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
}
MAX_FILE_BYTES = 256 * 1024
MAX_FILES = 500
_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")

# Starter files that have newer versions. Each store has its own copy of the
# theme; a copy whose content still matches one of these earlier versions
# (SHA-256) was never edited, so it is replaced with the current starter file.
# Edited copies are left alone. Bump STARTER_REVISION when adding entries.
STARTER_REVISION = 8
STARTER_UPGRADES = {
    "sections/hero.liquid": {"c713c5559e80c118413fad28450cbf01bbfbf5b145377320eb8b06ef73dc2c3d"},
    "sections/slideshow.liquid": {"1676de060a502cce01bded76ca923306ffb596e2b5e4a824630b2398e5b1fb58"},
    "sections/product-grid.liquid": {
        "b5bc74b7ad19eb53aa7935de4d44ecd407b7748445ba5a73ab65ada18b24e184",
        "197e7ad9e76dda5adcb0b762a69623d0ebb407baf1e3391edd794d26fa41275f",
    },
    "sections/product-carousel.liquid": {"0609acb6e9af090ad850a2f416005ac19ae7716ba1acb730c063864e7e6776d4"},
    "sections/rich-text.liquid": {"37ec79026e20ea886f79c150c660ffdfb6ca04b4614bb4878a6d263e1a58b10b"},
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
SETTINGS_FILES = (SETTINGS_FILE, *TEMPLATE_FILES)


def _dump(value):
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


def settings_files(store_theme):
    """{path: JSON text} of the customizer's settings, with defaults filled in."""
    data = resolve(store_theme.custom_data)
    files = {SETTINGS_FILE: _dump(data["settings"])}
    for path, page in TEMPLATE_FILES.items():
        files[path] = _dump(data["templates"][page])
    return files


def code_files(store_theme):
    """Everything the code editor lists: the Liquid files plus the settings files."""
    return {**theme_files(store_theme), **settings_files(store_theme)}


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


def save_file(store_theme, path, content):
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
