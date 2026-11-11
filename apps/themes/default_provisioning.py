"""
The default theme file tree (apps/themes/default_theme/) for new stores.

A store gets the tree once, when its first theme is installed from the starter (services.get_store_theme),
and only when settings.DEFAULT_TREE_ENABLED is on and the store was created at or after
settings.DEFAULT_TREE_CUTOFF. Files are only added, never overwritten: paths the theme already has (the
starter's, or the store's own) and the customizer's settings files (config/settings_data.json,
templates/<page>.json, assets/images.json) are skipped. Those stay in default_theme/ as reference copies.

The storefront does not use these files yet: the renderer still renders the starter's Liquid templates.
"""
from datetime import datetime, timedelta, timezone as dt_timezone
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.utils import timezone
from rest_framework import exceptions

from . import services
from .models import Theme, ThemeFile

DEFAULT_DIR = Path(__file__).resolve().parent / "default_theme"
# 2026-11-11 00:00 India Standard Time (2026-11-10 18:30 UTC)
DEFAULT_CUTOFF = datetime(2026, 11, 11, tzinfo=dt_timezone(timedelta(hours=5, minutes=30)))
# theme_data key set on a theme that got the tree (lets "reset to default" use default_theme/)
MARK = "default_tree"
TREE_VERSION = 1


def default_files():
    """{path: content} of every file in default_theme/, reference copies included."""
    return {
        path.relative_to(DEFAULT_DIR).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(DEFAULT_DIR.rglob("*"))
        if path.is_file()
    }


def cutoff():
    value = getattr(settings, "DEFAULT_TREE_CUTOFF", DEFAULT_CUTOFF)
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime) or timezone.is_naive(value):
        raise ImproperlyConfigured(
            "DEFAULT_TREE_CUTOFF must be a timezone-aware datetime, e.g. 2026-11-11T00:00:00+05:30."
        )
    return value


def is_eligible(store):
    """True only while the feature is on, for a store created at or after the cutoff."""
    if not getattr(settings, "DEFAULT_TREE_ENABLED", False):
        return False
    created = store.created_at
    if created is None or timezone.is_naive(created):
        return False
    return created >= cutoff()


def is_reserved(store_theme, path):
    """Settings files and the images list: generated from the database, never written as theme files."""
    return (
        path in services.settings_paths(store_theme)
        or bool(services.PAGE_TEMPLATE_JSON.match(path))
        or path == services.IMAGES_FILE
    )


def missing_files(store_theme):
    """{path: content} of the default files this theme doesn't have yet, reserved paths left out."""
    have = set(store_theme.theme.files.values_list("file_path", flat=True))
    return {
        path: content
        for path, content in default_files().items()
        if path not in have and not is_reserved(store_theme, path)
    }


def provision_default_files(store_theme):
    """Add the default tree's missing files to an eligible store's theme. Returns the paths added."""
    if not is_eligible(store_theme.store):
        return []
    with transaction.atomic():
        theme = Theme.objects.select_for_update().get(pk=store_theme.theme_id)
        added = missing_files(store_theme)
        if not added and (theme.theme_data or {}).get(MARK) == TREE_VERSION:
            return []
        ThemeFile.objects.bulk_create([services._new_file(theme, path, content) for path, content in added.items()])
        theme.theme_data = {**(theme.theme_data or {}), MARK: TREE_VERSION}
        theme.save(update_fields=["theme_data", "updated_at"])
    store_theme.theme.theme_data = theme.theme_data
    return sorted(added)


def reset_file(store_theme, path):
    """
    Put one file back to its default, saved like an edit from the code editor: the starter's copy for paths
    the starter owns (that is the copy the theme was installed with), otherwise default_theme/'s copy in
    themes that got the default tree. Settings files and paths with no default are refused.
    """
    path = services.clean_path(path)
    if is_reserved(store_theme, path):
        raise exceptions.ValidationError({"path": "Settings files come from the customizer and can't be reset."})
    content = services.starter_files().get(path)
    if content is None and (store_theme.theme.theme_data or {}).get(MARK):
        content = default_files().get(path)
    if content is None:
        raise exceptions.NotFound("This file has no default version to reset to.")
    return services.save_file(store_theme, path, content)
