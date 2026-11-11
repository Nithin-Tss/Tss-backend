"""
Where the code editor may create files (on top of the shape checks in services.clean_path).

- A new file, or the target of a rename, goes in one of THEME_FOLDERS. Files a theme already has
  keep working wherever they are: they can still be read and saved.
- A leading "_" is allowed only for a file directly inside blocks/ (a private block, like _card.liquid).
"""
import re

from rest_framework import exceptions

THEME_FOLDERS = ("assets", "blocks", "config", "layout", "locales", "sections", "snippets", "templates")

_PRIVATE_BLOCK = re.compile(r"^blocks/_[A-Za-z0-9][A-Za-z0-9._-]{0,98}$")


def is_private_block(path):
    return bool(_PRIVATE_BLOCK.match(path))


def check_new_path(path):
    """Refuse creating (or renaming to) a file outside the theme folders."""
    folder, slash, _ = path.partition("/")
    if not slash or folder not in THEME_FOLDERS:
        raise exceptions.ValidationError(
            {"path": f"New files go in one of these folders: {', '.join(THEME_FOLDERS)}."}
        )
