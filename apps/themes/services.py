import posixpath

from django.db import transaction

from apps.tenancy.models import Store

from .models import StoreTheme, Theme, ThemeFile

KEEP = ".keep"

DEFAULT_FILES = {
    "layout/theme.liquid": (
        "<!doctype html>\n<html>\n  <head>\n    <meta charset=\"utf-8\" />\n"
        "    <title>{{ store.name }}</title>\n  </head>\n  <body>\n"
        "    {% render 'header' %}\n    {% render 'index' %}\n  </body>\n</html>\n"
    ),
    "header.liquid": "<header><h1>{{ store.name }}</h1></header>\n",
    "index.liquid": "<main><h2>Welcome to {{ store.name }}</h2></main>\n",
}


class ThemePathError(ValueError):
    pass


def clean_path(path):
    path = (path or "").strip().strip("/")
    parts = path.split("/") if path else []
    if not parts or any(p in ("", ".", "..") for p in parts) or "\\" in path:
        raise ThemePathError("Invalid path.")
    name = parts[-1]
    if name != KEEP and not name.endswith(".liquid"):
        raise ThemePathError("Only Liquid files (.liquid) are allowed.")
    if len(path) > 500:
        raise ThemePathError("Path is too long.")
    return path


def clean_folder(path):
    path = (path or "").strip().strip("/")
    parts = path.split("/") if path else []
    if not parts or any(p in ("", ".", "..") for p in parts):
        raise ThemePathError("Invalid path.")
    return path


def resolve_store(request):
    store_id = request.headers.get("X-Store-Id") or request.query_params.get("store")
    if store_id:
        try:
            return Store.objects.filter(pk=store_id).first()
        except Exception:
            return None
    return Store.objects.order_by("created_at").first()


def _make_file(theme, path, content):
    return ThemeFile(
        theme=theme,
        file_path=path,
        file_name=posixpath.basename(path),
        directory=posixpath.dirname(path)[:100],
        file_type="keep" if path.endswith(KEEP) else "liquid",
        content=content,
        file_size=len(content.encode()),
    )


@transaction.atomic
def get_store_theme(store):
    """Return the store's own editable theme, seeding defaults on first use."""
    link = (
        StoreTheme.objects.select_related("theme")
        .filter(store=store, is_active=True)
        .first()
    )
    if link:
        return link.theme
    theme = Theme.objects.create(
        name=f"{store.store_name} theme",
        slug=f"store-{store.pk}",
        category="custom",
    )
    StoreTheme.objects.create(store=store, theme=theme, installed_version=theme.version)
    ThemeFile.objects.bulk_create(
        [_make_file(theme, p, c) for p, c in DEFAULT_FILES.items()]
    )
    return theme


def save_file(theme, path, content):
    path = clean_path(path)
    content = content or ""
    obj, created = ThemeFile.objects.get_or_create(
        theme=theme,
        file_path=path,
        defaults={
            "file_name": posixpath.basename(path),
            "directory": posixpath.dirname(path)[:100],
            "file_type": "keep" if path.endswith(KEEP) else "liquid",
            "content": content,
            "file_size": len(content.encode()),
        },
    )
    if not created:
        obj.content = content
        obj.file_size = len(content.encode())
        obj.save(update_fields=["content", "file_size", "updated_at"])
    return obj


@transaction.atomic
def delete_path(theme, path):
    path = (path or "").strip().strip("/")
    if not path:
        raise ThemePathError("Invalid path.")
    qs = theme.files.filter(file_path=path) | theme.files.filter(file_path__startswith=path + "/")
    if qs.filter(is_required=True).exists():
        raise ThemePathError("This file is required and cannot be deleted.")
    count, _ = qs.delete()
    return count


@transaction.atomic
def rename_path(theme, source, target):
    source = (source or "").strip().strip("/")
    target = clean_folder(target)
    if not source:
        raise ThemePathError("Invalid path.")
    exact = theme.files.filter(file_path=source).first()
    if exact:
        target = clean_path(target)
        files = [exact]
    else:
        files = list(theme.files.filter(file_path__startswith=source + "/"))
        if not files:
            raise ThemePathError("Nothing to rename.")
    mapping = {f: target + f.file_path[len(source):] for f in files}
    new_paths = set(mapping.values())
    clash = theme.files.filter(file_path__in=new_paths).exclude(pk__in=[f.pk for f in files])
    if clash.exists():
        raise ThemePathError("An item with that name already exists.")
    for f, new in mapping.items():
        f.file_path = new
        f.file_name = posixpath.basename(new)
        f.directory = posixpath.dirname(new)[:100]
        f.save(update_fields=["file_path", "file_name", "directory", "updated_at"])
