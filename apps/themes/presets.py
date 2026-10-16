"""
Ready-made themes a store can pick from (presets/<slug>.json) and keep editing.

A preset is the theme-wide look (colours, fonts, buttons) plus the sections of a page
(the home page's layout). It can also carry code: Liquid files in presets/<slug>/, laid out
like the theme (presets/bold/snippets/styles.liquid replaces the store's snippets/styles.liquid).
Applying one writes all of it into the store's own theme through the same paths as the
customizer and the code editor: settings are validated, files are saved with their usual checks
and history, and the page's section lines in the code editor follow. The store's own content
(logo, menus, social links, currency, announcement, header and footer values, other pages) is
kept. The code a preset replaced can be brought back with undo_preset (one step).

Rules for a preset's files: no templates/index.liquid, product, collection, ... (the page layouts
come from "templates" in the JSON); everything else is fine.
"""
import copy
import json
import re
from pathlib import Path

from django.db import transaction
from rest_framework import exceptions

from . import section_sync as sync
from .schemas import THEME_SETTINGS, resolve, validate_custom_data
from .services import delete_path, save_file, starter_files, templates_changed

PRESETS_DIR = Path(__file__).resolve().parent / "presets"
DEFAULT_PRESET = "classic"
BACKUP_KEY = "preset_backup"

# What a preset sets for the whole store; the rest of the settings are the store's own
_LOOK_GROUPS = {"colors", "typography", "buttons"}


def _look_keys():
    return {key for key, spec in THEME_SETTINGS.items() if spec.get("group") in _LOOK_GROUPS}


def _normal(text):
    return text.replace("\r\n", "\n")


def _load(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    data["slug"] = path.stem
    return data


def list_presets():
    return [_load(path) for path in sorted(PRESETS_DIR.glob("*.json"))]


def get_preset(slug):
    if not isinstance(slug, str) or not re.fullmatch(r"[a-z0-9-]+", slug) or not (PRESETS_DIR / f"{slug}.json").is_file():
        raise exceptions.NotFound("That theme doesn't exist.")
    return _load(PRESETS_DIR / f"{slug}.json")


def preset_files(slug):
    """{theme path: content} of the Liquid files a preset replaces (none for most)."""
    folder = PRESETS_DIR / slug
    return {
        path.relative_to(folder).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(folder.rglob("*.liquid"))
    } if folder.is_dir() else {}


def check_files(files):
    """Raise for a preset file the theme can't take (see the module's rules)."""
    for path in files:
        name = sync.PATH_TO_SECTION.get(path)
        if name and sync.SYNCED_FILES[name]["kind"] == "template":
            raise ValueError(f"{path}: page layouts come from the preset's JSON, not a file.")


def applied_slug(store_theme):
    return (store_theme.theme.theme_data or {}).get("preset") or DEFAULT_PRESET


def public(preset):
    return {key: preset[key] for key in ("slug", "name", "description", "categories", "swatches")}


def previous_name(store_theme):
    """The theme undo would go back to (a name), or None when there's nothing to undo."""
    backup = (store_theme.theme.theme_data or {}).get(BACKUP_KEY)
    if not backup:
        return None
    try:
        return get_preset(backup.get("preset") or DEFAULT_PRESET)["name"]
    except exceptions.NotFound:
        return "your previous theme"


def _current(theme, path):
    file = theme.files.filter(file_path=path).first()
    return file.content if file else None


def _write(store_theme, path, content):
    """Save `content` at `path` (None deletes it) the way the code editor would; unchanged files are left alone."""
    now = _current(store_theme.theme, path)
    if content is None:
        if now is not None:
            delete_path(store_theme, path)
    elif now is None or _normal(now) != _normal(content):
        save_file(store_theme, path, content, client="preset")


def _code_to_undo(theme, previous, wanted):
    """
    Files the previous preset put in the store that nobody has edited since: they go back to the
    starter's version (or go away), unless the new preset replaces them anyway.
    """
    starter = starter_files()
    back = {}
    for path, content in preset_files(previous).items():
        now = _current(theme, path)
        if path not in wanted and now is not None and _normal(now) == _normal(content):
            back[path] = starter.get(path)
    return back


@transaction.atomic
def apply_preset(store_theme, slug, client=""):
    preset = get_preset(slug)
    theme = store_theme.theme
    original = copy.deepcopy(store_theme.custom_data or {})

    wanted = preset_files(slug)
    previous = (theme.theme_data or {}).get("preset")
    changes = {**(_code_to_undo(theme, previous, wanted) if previous and previous != slug else {}), **wanted}
    old_files = {path: _current(theme, path) for path in changes}
    old_preset = previous

    for path, content in changes.items():
        _write(store_theme, path, content)

    # Saving a header or footer file re-derives its values from its code; the store's stay
    store_theme.refresh_from_db(fields=["custom_data"])
    custom = store_theme.custom_data or {}
    sections = original.get("sections")

    settings = {k: v for k, v in (original.get("settings") or {}).items() if k not in _look_keys()}
    settings.update(preset["settings"])
    templates = {**(original.get("templates") or {}), **copy.deepcopy(preset["templates"])}

    clean = validate_custom_data({"settings": settings, "templates": templates})
    if isinstance(sections, dict):
        clean["sections"] = sections

    before = resolve(custom)["templates"]
    store_theme.custom_data = clean
    store_theme.save(update_fields=["custom_data", "updated_at"])

    theme.refresh_from_db()
    theme.theme_data = {
        **(theme.theme_data or {}),
        "preset": preset["slug"],
        BACKUP_KEY: {"preset": old_preset, "custom_data": original, "files": old_files},
    }
    theme.save(update_fields=["theme_data", "updated_at"])

    templates_changed(store_theme, before, client=client)
    return preset


@transaction.atomic
def undo_preset(store_theme, client=""):
    theme = store_theme.theme
    theme.refresh_from_db()
    backup = (theme.theme_data or {}).get(BACKUP_KEY)
    if not backup:
        raise exceptions.NotFound("There's nothing to undo.")

    for path, content in backup["files"].items():
        _write(store_theme, path, content)

    store_theme.refresh_from_db(fields=["custom_data"])
    before = resolve(store_theme.custom_data or {})["templates"]
    store_theme.custom_data = backup["custom_data"]
    store_theme.save(update_fields=["custom_data", "updated_at"])

    theme.refresh_from_db()
    theme_data = {k: v for k, v in (theme.theme_data or {}).items() if k not in ("preset", BACKUP_KEY)}
    if backup.get("preset"):
        theme_data["preset"] = backup["preset"]
    theme.theme_data = theme_data
    theme.save(update_fields=["theme_data", "updated_at"])

    templates_changed(store_theme, before, client=client)
    return backup.get("preset") or DEFAULT_PRESET
