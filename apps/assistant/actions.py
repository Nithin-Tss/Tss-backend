"""
The buttons the assistant may offer. A path comes from this table only: the model never supplies one,
and the guide file names a key (never a path). Each button needs the permission the page itself needs
(Tss-shop/src/App.jsx, RequirePermission), so a member without it gets the steps but no button.
"""
import re
from dataclasses import dataclass
from urllib.parse import urlencode

from . import themecode

MAX_ACTIONS = 3


@dataclass(frozen=True)
class Action:
    label: str
    path: str
    permission: str


ACTIONS = {
    "online_store": Action("Open Online Store", "/admin/online-store", "themes.view"),
    "customize": Action("Open Customize", "/admin/online-store/customize", "themes.manage"),
    "code": Action("Open Code editor", "/admin/online-store/code", "themes.manage"),
    "add_product": Action("Add product", "/admin/online-store/products/new", "products.manage"),
    "products": Action("Open Products", "/admin/online-store/products", "products.view"),
    "discounts": Action("Open Discounts", "/admin/online-store/discounts", "discounts.view"),
    "menus": Action("Open Menus", "/admin/online-store/content/menus", "themes.manage"),
    "pages": Action("Open Pages", "/admin/online-store/pages", "themes.manage"),
}


def build_actions(keys, permissions):
    """[{"label", "path"}] for the known keys the member may open, in order, without repeats."""
    result, seen = [], set()
    for key in keys:
        action = ACTIONS.get(key) if isinstance(key, str) else None
        if action is None or key in seen or action.permission not in permissions:
            continue
        seen.add(key)
        result.append({"label": action.label, "path": action.path})
    return result[:MAX_ACTIONS]


CODE_PATH = "/admin/online-store/code"
CODE_PERMISSION = "themes.manage"


def code_file_actions(paths, theme_id, permissions):
    """
    "Open <file> in Code editor" buttons for files the theme tool returned in this request. A path is
    checked again here, and the button needs the permission the code editor page needs.
    """
    if CODE_PERMISSION not in permissions:
        return []
    result = []
    for path in paths:
        if not themecode.valid_path(path):
            continue
        params = {"file": path}
        if theme_id and re.fullmatch(r"[0-9a-f-]{36}", str(theme_id)):
            params["theme"] = str(theme_id)
        result.append({"label": f"Open {path.rsplit('/', 1)[-1]} in Code editor", "path": f"{CODE_PATH}?{urlencode(params)}"})
    return result[:2]
