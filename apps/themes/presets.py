"""
Ready-made themes (presets/<slug>.json) a store can add to its library.

A preset is a complete design written in Liquid, not just colours:
  presets/<slug>.json       name, description, categories, swatches, the theme-wide settings
                            (colours, fonts, buttons, header...) and the page layouts (sections)
  presets/<slug>/**.liquid  the design's own Liquid files, at the same paths as in a theme. They are added
                            to the starter's files, and replace the starter's where the path is the same:
                            layout/theme.liquid (loads the design's snippets/theme-styles.liquid),
                            sections/hero.liquid (its own markup), and so on.

Adding a preset to a store creates a new theme in the store's library from the starter plus
these. It never changes a theme the store already has (see services.create_theme): the first
theme is published, later ones are drafts the owner can edit, preview and publish.

Rules for a preset's Liquid files: not the page layouts (templates/index.liquid, product, ...):
those come from "templates" in the JSON, which the customizer owns.
"""
import json
import re
from pathlib import Path

from rest_framework import exceptions

from . import section_sync as sync
from .schemas import validate_custom_data
from .services import starter_files

PRESETS_DIR = Path(__file__).resolve().parent / "presets"


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


def public(preset):
    return {key: preset[key] for key in ("slug", "name", "description", "categories", "swatches")}


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


def build(slug):
    """-> (files, custom_data, theme_data) of a new theme made from the preset."""
    preset = get_preset(slug)
    files = starter_files()
    files.update(preset_files(slug))
    custom_data = validate_custom_data({"settings": preset["settings"], "templates": preset["templates"]})
    return files, custom_data, {"preset": slug}
