"""
Store themes.

Every store edits its own copy of a theme: the first time a store's theme
is used, the starter theme (apps/themes/starter/) - or the shared theme the
store had installed - is copied into a private Theme row whose slug is
"store-<store id>". Code edits from the editor change only that copy.
Section settings (hero text, slides, ...) live in StoreTheme.custom_data.
"""
import re
from pathlib import Path

from django.db import IntegrityError, transaction
from rest_framework import exceptions

from .models import StoreTheme, Theme, ThemeFile

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
    return installed


def theme_files(store_theme):
    return {f.file_path: f.content for f in store_theme.theme.files.order_by("file_path")}


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
