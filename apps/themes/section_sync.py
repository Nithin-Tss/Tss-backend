"""
Two-way sync between a section's Liquid code and its saved values.

A synced section is one Liquid file with a {% schema %} block (settings and
blocks, Shopify style). Its values live in ONE place per store:

    StoreTheme.custom_data["sections"][name] = {"settings": {...}, "blocks": [...]}

and the Liquid reads only section.settings and block.settings.

    Code -> Blocks   saving the file in Edit code re-reads its schema: new settings
                     and blocks appear in the customizer, removed ones go, and ids
                     that stay keep their saved values. Invalid Liquid or schema JSON
                     is refused with its line number; the previous version keeps working.
    Blocks -> Code   saving values from the customizer writes them back into the
                     schema ("default" of each setting, and "default.blocks" for the
                     blocks and their order), touching only those lines.

Every change bumps the section's version, keeps the last HISTORY_LIMIT versions
of the file (Restore), and is pushed to open editors as an event (EVENTS).

Reusable: a section takes part when it is listed in SYNCED_SECTIONS (the
header and the footer for now). Everything else - saving, history, events,
timings, the customizer panel and the code editor - follows that list.
"""
import hashlib
import json
import logging
import re
import threading
import time
from collections import deque

from liquid import Environment
from liquid.exceptions import LiquidError
from markupsafe import Markup, escape

logger = logging.getLogger(__name__)

# Each synced section:
#   path       its Liquid file
#   label      its name in the customizer
#   snippet    the layout's {% render '<snippet>' %} it takes the place of
#   originals  SHA-256 of every starter version of that snippet (each with LF and with CRLF line
#              endings): a theme whose snippet still matches one gets the section; a store that
#              edited its snippet keeps its own. Bump ORIGINALS_REVISION when adding hashes, so
#              themes checked before are checked again
#   wrapper    True: the editor wraps it in a <div> to select it in the preview. False: its root
#              element carries {{ section.attrs }} instead (a <div> around a sticky header would
#              stop it sticking)
#   context    extra data its Liquid uses besides shop and settings ("collections")
#   position   where it shows in the customizer's sections list
SYNCED_SECTIONS = {
    "header": {
        "path": "sections/header.liquid", "label": "Header", "snippet": "header",
        "originals": {
            # current starter (a590775)
            "87b238125f2423a74a44f72907f6c497819e57a91cf2c93aa1d55a96a652e53d",  # LF
            "7abacf47fa6285eae383b52aae6d89225e54e00be5f583da939ec1b7d359df37",  # CRLF
            # first starter, before the header followed Theme settings (2df94aa)
            "6e36e4b3429052522d9242b9f5cbb83c98ae509b028f38bc0381169ff4c9dec1",  # LF
            "6728b0814ac2020a9800f3ee8d48a900b48fb68c6c4dff4cbbabea80c397604a",  # CRLF
        },
        "wrapper": False, "context": ("collections",), "position": "top",
    },
    "footer": {
        "path": "sections/footer.liquid", "label": "Footer", "snippet": "footer",
        "originals": {
            # current starter (a590775)
            "dfef84bf0c7a8ad1f4be1895742e3df4b586303f40cdec1f6f667ee3ee34d426",  # LF
            "6ae26729a6ebc257f8ceeef600fdaad077c1458664a1f5b15039931491237e5a",  # CRLF
            # first starter (2df94aa)
            "46998966bcd74dab088da530528ad25f6fac556a3cd7d2224bcdb728e6bf3573",  # LF
            "692f9ef663eec69e8af4b5add5f078b301a653313b91e8c9aec82aa75f748773",  # CRLF
        },
        "wrapper": True, "context": (), "position": "bottom",
    },
}
PATH_TO_SECTION = {spec["path"]: name for name, spec in SYNCED_SECTIONS.items()}

# Revision 2: older starter snippets and CRLF line endings count as untouched
ORIGINALS_REVISION = 2


def is_original_snippet(name, content):
    """True when `content` is one of the starter's own versions of the section's snippet (never edited)."""
    originals = SYNCED_SECTIONS[name]["originals"]
    for text in (content, content.replace("\r\n", "\n")):
        if hashlib.sha256(text.encode("utf-8")).hexdigest() in originals:
            return True
    return False

# What the customizer and the code editor time ("change made -> preview updated"), per section
TIMING_STEPS = ("sidebar -> preview", "code save -> preview", "sidebar -> code", "code save -> blocks")
TIMING_KINDS = {f"{name}: {step}" for name in SYNCED_SECTIONS for step in TIMING_STEPS}

HISTORY_LIMIT = 20
# Quick edits from the customizer (typing) update the last saved version instead of adding new ones
COALESCE_SECONDS = 30
MAX_BLOCKS = 16
MAX_TEXT = 5000

_SCHEMA = re.compile(r"{%-?\s*schema\s*-?%}(.*?){%-?\s*endschema\s*-?%}", re.S)
_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
_ID = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_SAFE_URL = re.compile(r"^(https?://|mailto:|tel:|/(?!/)|#)", re.I)


class SectionCodeError(Exception):
    """Invalid Liquid or schema: the message names the problem, `line` where it is."""

    def __init__(self, message, line=None):
        super().__init__(message)
        self.message = message
        self.line = line

    def __str__(self):
        return f"Line {self.line}: {self.message}" if self.line else self.message


def line_of(text, index):
    return text.count("\n", 0, max(0, index)) + 1


# ======================================================================= parse and validate

def split(content):
    """(markup without the schema block, schema JSON text, offset of that JSON in content)."""
    match = _SCHEMA.search(content)
    if not match:
        return content, None, None
    # Keep the line numbers of the markup after the schema: blank lines in its place
    blank = "\n" * content.count("\n", match.start(), match.end())
    return content[: match.start()] + blank + content[match.end():], match.group(1), match.start(1)


SETTING_TYPES = {"text", "textarea", "richtext", "url", "image", "checkbox", "select", "radio", "color", "range", "number", "menu"}
UI_ONLY_TYPES = {"header", "paragraph"}


def _check_settings(settings, where, content, start):
    if not isinstance(settings, list):
        raise SectionCodeError(f'{where} "settings" must be a list.', line_of(content, start))
    seen = set()
    for i, s in enumerate(settings):
        if not isinstance(s, dict):
            raise SectionCodeError(f"{where} setting {i + 1} must be an object.", line_of(content, start))
        kind = s.get("type")
        if kind in UI_ONLY_TYPES:
            continue
        sid = s.get("id")
        line = _line_of_id(content, start, sid)
        if kind not in SETTING_TYPES:
            raise SectionCodeError(f'{where} setting "{sid}": unknown type "{kind}". Use one of: {", ".join(sorted(SETTING_TYPES))}.', line)
        if not isinstance(sid, str) or not _ID.match(sid):
            raise SectionCodeError(f"{where} setting {i + 1}: \"id\" must be lower-case letters, numbers and _.", line)
        if sid in seen:
            raise SectionCodeError(f'{where}: the id "{sid}" is used twice.', line)
        seen.add(sid)
        if kind in ("select", "radio"):
            options = s.get("options")
            if not isinstance(options, list) or not options or not all(isinstance(o, dict) and "value" in o for o in options):
                raise SectionCodeError(f'{where} setting "{sid}": "options" must be a list of {{"value", "label"}}.', line)
        if kind == "range" and not all(isinstance(s.get(k), (int, float)) for k in ("min", "max")):
            raise SectionCodeError(f'{where} setting "{sid}": a range needs "min" and "max".', line)


def _line_of_id(content, start, sid):
    if isinstance(sid, str):
        found = content.find(f'"{sid}"', start)
        if found >= 0:
            return line_of(content, found)
    return line_of(content, start)


def parse(content, path="section"):
    """The section's schema (dict). Raises SectionCodeError for invalid Liquid or schema."""
    markup, schema_text, start = split(content)
    if schema_text is None:
        raise SectionCodeError(f"{path} needs a {{% schema %}} ... {{% endschema %}} block with its settings.", None)
    try:
        schema = json.loads(schema_text)
    except json.JSONDecodeError as error:
        raise SectionCodeError(f"The schema is not valid JSON: {error.msg} (column {error.colno}).",
                               line_of(content, start) + error.lineno - 1)
    if not isinstance(schema, dict):
        raise SectionCodeError("The schema must be a JSON object.", line_of(content, start))

    _check_settings(schema.get("settings", []), "Section", content, start)
    blocks = schema.get("blocks", [])
    if not isinstance(blocks, list):
        raise SectionCodeError('The schema\'s "blocks" must be a list.', line_of(content, start))
    types = set()
    for block in blocks:
        if not isinstance(block, dict) or not isinstance(block.get("type"), str) or not _ID.match(block["type"]):
            raise SectionCodeError('Every block needs a "type" of lower-case letters, numbers and _.', line_of(content, start))
        if block["type"] in types:
            raise SectionCodeError(f'The block type "{block["type"]}" is defined twice.', _line_of_id(content, start, block["type"]))
        types.add(block["type"])
        _check_settings(block.get("settings", []), f'Block "{block["type"]}"', content, start)
    max_blocks = schema.get("max_blocks", MAX_BLOCKS)
    if not isinstance(max_blocks, int) or not 0 <= max_blocks <= 50:
        raise SectionCodeError('"max_blocks" must be a whole number from 0 to 50.', line_of(content, start))

    # The Liquid itself must compile (custom filters are only looked up when rendering)
    try:
        Environment().from_string(markup)
    except LiquidError as error:
        token = getattr(error, "token", None)
        line = line_of(markup, token.start_index) if token is not None and token.start_index is not None else None
        raise SectionCodeError(f"Liquid error: {getattr(error, 'message', None) or error}", line)
    return schema


# ======================================================================= schema -> customizer options

def _field(s):
    kind, label = s["type"], s.get("label") or s["id"].replace("_", " ").capitalize()
    base = {"label": label, "group": "content"}
    if s.get("info"):
        base["help"] = s["info"]
    if kind in ("text", "textarea", "richtext", "url", "image", "menu"):
        return {**base, "type": kind, "default": str(s.get("default", "")), "max": MAX_TEXT}
    if kind == "checkbox":
        return {**base, "type": "checkbox", "default": bool(s.get("default", False))}
    if kind in ("select", "radio"):
        values = [str(o["value"]) for o in s["options"]]
        default = str(s.get("default", values[0]))
        spec = {**base, "type": "select", "options": values, "default": default if default in values else values[0],
                "labels": {str(o["value"]): str(o.get("label", o["value"])) for o in s["options"]}}
        if kind == "radio":
            spec["ui"] = "segmented"
        return spec
    if kind == "color":
        return {**base, "type": "color_optional", "default": str(s.get("default", "") or "")}
    if kind == "range":
        lo, hi = s["min"], s["max"]
        default = s.get("default", lo)
        return {**base, "type": "range", "min": lo, "max": hi, "step": s.get("step", 1), "unit": s.get("unit", ""),
                "default": min(hi, max(lo, default)) if isinstance(default, (int, float)) else lo}
    return {**base, "type": "number", "min": s.get("min", -999999), "max": s.get("max", 999999),
            "default": s.get("default", 0) if isinstance(s.get("default", 0), (int, float)) else 0}


def _fields(settings):
    return {s["id"]: _field(s) for s in settings or [] if s.get("type") not in UI_ONLY_TYPES}


def spec_of(name, schema):
    """The section in the customizer's option format (what GroupedFields and SectionBlocks show)."""
    return {
        "name": schema.get("name") or SYNCED_SECTIONS[name]["label"],
        "pages": [],
        "required": True,
        "synced": True,
        "settings": _fields(schema.get("settings")),
        "blocks": {b["type"]: {"name": b.get("name") or b["type"].capitalize(), "settings": _fields(b.get("settings"))}
                   for b in schema.get("blocks", [])},
        "max_blocks": schema.get("max_blocks", MAX_BLOCKS),
    }


# ======================================================================= values

def _clean(spec, value, strict, where):
    kind = spec["type"]
    if kind == "checkbox":
        return bool(value) if isinstance(value, bool) else spec["default"]
    if kind in ("range", "number"):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return spec["default"]
        return min(spec["max"], max(spec["min"], value))
    if kind == "select":
        return value if value in spec["options"] else spec["default"]
    if not isinstance(value, str):
        return spec["default"]
    value = value[:MAX_TEXT]
    if kind == "color_optional":
        return value.upper() if _COLOR.match(value) else ("" if value == "" else spec["default"])
    if kind in ("url", "image") and value.strip() and not _SAFE_URL.match(value.strip()):
        # javascript:, data: and other schemes are never stored or rendered
        if strict:
            raise ValueError(f"{where}: links must start with https://, http://, mailto:, tel:, / or #.")
        return ""
    if kind == "menu":
        lines = []
        for line in value.splitlines():
            label, _, url = line.partition("|")
            url = url.strip()
            if url and not _SAFE_URL.match(url):
                if strict:
                    raise ValueError(f"{where}: the link \"{url}\" must start with https://, http://, mailto:, tel:, / or #.")
                continue
            lines.append(line)
        return "\n".join(lines)
    return value


def _clean_settings(fields, raw, strict, where):
    raw = raw if isinstance(raw, dict) else {}
    return {key: _clean(spec, raw[key], strict, f"{where} “{spec['label']}”") if key in raw else spec["default"]
            for key, spec in fields.items()}


def defaults_of(spec, schema):
    blocks = [b for b in ((schema.get("default") or {}).get("blocks") or []) if isinstance(b, dict)]
    return {
        "settings": {k: f["default"] for k, f in spec["settings"].items()},
        "blocks": clean_blocks(spec, blocks, strict=False),
    }


def clean_blocks(spec, blocks, strict):
    out = []
    for block in blocks if isinstance(blocks, list) else []:
        if not isinstance(block, dict) or block.get("type") not in spec["blocks"]:
            continue
        bspec = spec["blocks"][block["type"]]
        out.append({
            "type": block["type"],
            "hidden": bool(block.get("hidden", False)),
            "settings": _clean_settings(bspec["settings"], block.get("settings"), strict, bspec["name"]),
        })
        if len(out) >= spec["max_blocks"]:
            break
    return out


def clean_values(spec, raw, strict=False):
    """Saved values with every option filled in; unknown ones dropped. strict: refuse unsafe links."""
    raw = raw if isinstance(raw, dict) else {}
    return {
        "settings": _clean_settings(spec["settings"], raw.get("settings"), strict, spec["name"]),
        "blocks": clean_blocks(spec, raw.get("blocks"), strict),
    }


def merge_for_new_code(old_schema, new_schema, saved, name):
    """
    Values after the code changed: ids that stay (with the same type) keep their saved
    value, new ones start at their default. If the code changed its default blocks
    (added, removed, reordered or edited them), those are the blocks now.
    """
    new_spec = spec_of(name, new_schema)
    if not old_schema:
        return clean_values(new_spec, saved) if saved else defaults_of(new_spec, new_schema)
    old_spec = spec_of(name, old_schema)

    def same_type(old_fields, new_fields, key):
        return key in old_fields and old_fields[key]["type"] == new_fields[key]["type"]

    settings = {}
    saved_settings = (saved or {}).get("settings") or {}
    for key, field in new_spec["settings"].items():
        if key in saved_settings and same_type(old_spec["settings"], new_spec["settings"], key):
            settings[key] = _clean(field, saved_settings[key], False, key)
        else:
            settings[key] = field["default"]

    old_default = (old_schema.get("default") or {}).get("blocks")
    new_default = (new_schema.get("default") or {}).get("blocks")
    if json.dumps(old_default, sort_keys=True) != json.dumps(new_default, sort_keys=True):
        blocks = clean_blocks(new_spec, new_default or [], strict=False)
    else:
        blocks = clean_blocks(new_spec, (saved or {}).get("blocks") or [], strict=False)
    return {"settings": settings, "blocks": blocks}


# ======================================================================= Blocks -> Code (minimal edits)
#
# A small JSON reader that remembers where every value is in the text, so a
# value can be replaced without re-formatting anything else.

class _Node:
    __slots__ = ("start", "end", "value", "members", "items")

    def __init__(self, start, end, value, members=None, items=None):
        self.start, self.end, self.value, self.members, self.items = start, end, value, members, items


_WS = re.compile(r"\s*")
_NUMBER = re.compile(r"-?(0|[1-9]\d*)(\.\d+)?([eE][+-]?\d+)?")


def _ws(text, i):
    return _WS.match(text, i).end()


def _string_end(text, i):
    j = i + 1
    while text[j] != '"':
        j += 2 if text[j] == "\\" else 1
    return j + 1


def _scan(text, i):
    i = _ws(text, i)
    ch = text[i]
    if ch == "{":
        members, j = {}, _ws(text, i + 1)
        if text[j] == "}":
            return _Node(i, j + 1, {}, members={}), j + 1
        while True:
            key_end = _string_end(text, j)
            key = json.loads(text[j:key_end])
            j = _ws(text, key_end) + 1  # past ':'
            node, j = _scan(text, j)
            members[key] = node
            j = _ws(text, j)
            if text[j] == "}":
                return _Node(i, j + 1, {k: n.value for k, n in members.items()}, members=members), j + 1
            j = _ws(text, j + 1)  # past ','
    if ch == "[":
        items, j = [], _ws(text, i + 1)
        if text[j] == "]":
            return _Node(i, j + 1, [], items=[]), j + 1
        while True:
            node, j = _scan(text, j)
            items.append(node)
            j = _ws(text, j)
            if text[j] == "]":
                return _Node(i, j + 1, [n.value for n in items], items=items), j + 1
            j = _ws(text, j + 1)
    if ch == '"':
        end = _string_end(text, i)
        return _Node(i, end, json.loads(text[i:end])), end
    for literal, value in (("true", True), ("false", False), ("null", None)):
        if text.startswith(literal, i):
            return _Node(i, i + len(literal), value), i + len(literal)
    match = _NUMBER.match(text, i)
    return _Node(i, match.end(), json.loads(match.group(0))), match.end()


def _dump(value):
    return json.dumps(value, ensure_ascii=False)


def _indent_before(text, index):
    line_start = text.rfind("\n", 0, index) + 1
    return re.match(r"[ \t]*", text[line_start:]).group(0)


def write_values(content, values):
    """
    content with the schema's defaults set to `values`: each setting's "default" and the
    section's "default": {"blocks": [...]}. Only those values change; every other line,
    the formatting and comments stay as they were. Returns content unchanged if equal.
    """
    match = _SCHEMA.search(content)
    if not match:
        return content
    offset = match.start(1)
    text = match.group(1)
    root, _ = _scan(text, 0)
    edits = []  # (start, end, replacement) within text

    def set_defaults(settings_node, current):
        if not settings_node or settings_node.items is None:
            return
        for item in settings_node.items:
            if item.members is None or "id" not in item.members or item.value.get("id") not in current:
                continue
            value = current[item.value["id"]]
            if "default" in item.members:
                if item.members["default"].value != value:
                    edits.append((item.members["default"].start, item.members["default"].end, _dump(value)))
            elif value not in ("", None, False):
                after = item.members["id"].end
                edits.append((after, after, f', "default": {_dump(value)}'))

    set_defaults(root.members.get("settings"), values.get("settings", {}))

    blocks = [
        {"type": b["type"], **({"hidden": True} if b.get("hidden") else {}), "settings": b["settings"]}
        for b in values.get("blocks", [])
    ]
    default = root.members.get("default")
    if default is not None and default.members is not None and "blocks" in default.members:
        node = default.members["blocks"]
        if node.value != blocks:
            edits.append((node.start, node.end, _blocks_text(blocks, _indent_before(text, node.start))))
    elif default is not None and default.members is not None:
        # "default": {} without blocks: add them
        inner = default.start + 1
        edits.append((inner, inner, f'"blocks": {_blocks_text(blocks, _indent_before(text, default.start))}' + (", " if default.members else "")))
    else:
        # No "default" yet: add it as the schema's last entry, indented like the others
        first = min((n.start for n in root.members.values()), default=root.start)
        indent = _indent_before(text, first) or "  "
        before = text[: root.end - 1].rstrip()
        comma = "," if root.members else ""
        edits.append((len(before), len(before), f'{comma}\n{indent}"default": {{"blocks": {_blocks_text(blocks, indent)}}}'))

    if not edits:
        return content
    for start, end, replacement in sorted(edits, reverse=True):
        text = text[:start] + replacement + text[end:]
    return content[:offset] + text + content[offset + len(match.group(1)):]


def _blocks_text(blocks, indent):
    if not blocks:
        return "[]"
    inner = indent + "  "
    return "[\n" + ",\n".join(inner + _dump(b) for b in blocks) + "\n" + indent + "]"


# ======================================================================= history and version (theme.theme_data)

def _state(theme, name):
    data = theme.theme_data or {}
    return (data.get("section_sync") or {}).get(name) or {"version": 0, "history": []}


def section_state(theme, name):
    state = _state(theme, name)
    return {"version": state.get("version", 0), "updated_at": state.get("updated_at"), "source": state.get("source")}


def record_version(theme, name, previous_content, source, now=None):
    """Bump the version and keep the replaced file in the history (latest first, at most HISTORY_LIMIT)."""
    now = now or time.time()
    state = _state(theme, name)
    history = list(state.get("history") or [])
    last = history[0] if history else None
    if previous_content is not None and not (
        source == "customizer" and last and last.get("source") == "customizer" and now - last.get("saved_at", 0) < COALESCE_SECONDS
    ):
        history.insert(0, {"content": previous_content, "saved_at": now, "source": state.get("source") or "starter"})
    state = {"version": state.get("version", 0) + 1, "history": history[:HISTORY_LIMIT], "updated_at": now, "source": source}
    data = dict(theme.theme_data or {})
    data["section_sync"] = {**(data.get("section_sync") or {}), name: state}
    theme.theme_data = data
    return state["version"]


def history_of(theme, name):
    return [
        {"index": i, "saved_at": h["saved_at"], "source": h.get("source"), "size": len(h["content"]),
         "preview": h["content"][:160]}
        for i, h in enumerate(_state(theme, name).get("history") or [])
    ]


def history_content(theme, name, index):
    history = _state(theme, name).get("history") or []
    if not 0 <= index < len(history):
        return None
    return history[index]["content"]


# ======================================================================= events (pushed to open editors)

class EventBus:
    """
    In-process publish/subscribe for the live event stream (Server-Sent Events).
    Works within one server process (the dev server, or one worker); several
    workers would need a shared broker such as Redis in its place.
    """

    def __init__(self, keep=500):
        self._cond = threading.Condition()
        self._events = deque(maxlen=keep)
        self._seq = 0

    def publish(self, store_id, payload):
        with self._cond:
            self._seq += 1
            self._events.append((self._seq, str(store_id), payload))
            self._cond.notify_all()
            return self._seq

    @property
    def last_seq(self):
        return self._seq

    def wait(self, store_id, after, timeout):
        """Events for this store after `after` (waiting up to `timeout` seconds): ([(seq, payload)], last_seq)."""
        store_id = str(store_id)
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                found = [(s, p) for s, sid, p in self._events if s > after and sid == store_id]
                if found:
                    return found, found[-1][0]
                left = deadline - time.monotonic()
                if left <= 0:
                    return [], max(after, self._seq)
                self._cond.wait(left)


EVENTS = EventBus()


# ======================================================================= rendering (compiled template cache)

_CACHE = {}
_CACHE_LOCK = threading.Lock()


def compiled(theme_id, path, content, env, env_key):
    """
    The compiled template for one file, cached until that file (or the environment it
    renders with) changes: saving footer.liquid invalidates only its own entry.
    """
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    key = (str(theme_id), path)
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit and hit[0] == digest and hit[1] == env_key:
            return hit[2]
    markup, _, _ = split(content)
    template = env.from_string(markup)
    with _CACHE_LOCK:
        _CACHE[key] = (digest, env_key, template)
    return template


def invalidate(theme_id, path):
    with _CACHE_LOCK:
        _CACHE.pop((str(theme_id), path), None)


def menu_links(text, link):
    links = []
    for line in str(text or "").splitlines():
        label, _, url = line.partition("|")
        label, url = label.strip(), url.strip() or label.strip()
        if label and _SAFE_URL.match(url):
            links.append({"label": label, "url": link(url)})
    return links


def _resolved_value(field, value, link):
    if field["type"] == "menu":
        return menu_links(value, link)
    if field["type"] == "url" and value:
        return link(value)  # "/collections/all" -> this store's page
    return value


def render_section(name, *, theme_id, content, values, env, env_key, context, editor=False, link=lambda u: u):
    """
    HTML of a synced section. Liquid sees section.settings and section.blocks
    (each with .settings, and .attrs for the customizer's Edit mode); menu
    settings arrive as lists of {label, url}, url settings as this store's links.
    Output is escaped by the environment.
    """
    started = time.perf_counter()
    spec = spec_of(name, parse_cached(theme_id, name, content))
    clean = clean_values(spec, values)
    config = SYNCED_SECTIONS[name]

    def resolved(fields, settings):
        return {k: _resolved_value(fields[k], v, link) for k, v in settings.items()}

    blocks = []
    for index, block in enumerate(clean["blocks"]):
        if block["hidden"]:
            continue
        bspec = spec["blocks"][block["type"]]
        attrs = Markup(f' data-tss-block="{index}" data-tss-label="{escape(bspec["name"])}"') if editor else Markup("")
        blocks.append({"type": block["type"], "index": index, "settings": resolved(bspec["settings"], block["settings"]), "attrs": attrs})
    marker = f' data-tss-section="{escape(name)}" data-tss-fixed data-tss-label="{escape(spec["name"])}"'
    section = {
        "id": name, "settings": resolved(spec["settings"], clean["settings"]), "blocks": blocks,
        # On the root element of a section without a wrapper (see SYNCED_SECTIONS)
        "attrs": Markup(marker) if editor and not config["wrapper"] else Markup(""),
    }

    html = compiled(theme_id, config["path"], content, env, env_key).render(section=section, **context)
    html = html.rstrip()  # the blank lines that stand in for the schema
    # Wrapped - also when its code no longer puts {{ section.attrs }} on its root element,
    # so the customizer can always find it
    if editor and (config["wrapper"] or "data-tss-section=" not in html):
        html = f"<div{marker}>{html}</div>"
    logger.debug("section_sync: rendered %s in %.1f ms", name, (time.perf_counter() - started) * 1000)
    return Markup(html)


_SCHEMA_CACHE = {}


def parse_cached(theme_id, name, content):
    key = (str(theme_id), name)
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    hit = _SCHEMA_CACHE.get(key)
    if hit and hit[0] == digest:
        return hit[1]
    schema = parse(content, SYNCED_SECTIONS[name]["path"])
    _SCHEMA_CACHE[key] = (digest, schema)
    return schema


# ======================================================================= timing

class Timings:
    """Durations (ms) per kind, for the "change made -> preview updated" report."""

    def __init__(self, keep=200):
        self._lock = threading.Lock()
        self._data = {}
        self._keep = keep

    def add(self, kind, ms):
        with self._lock:
            values = self._data.setdefault(kind, deque(maxlen=self._keep))
            values.append(float(ms))
        logger.info("section_sync timing %s: %.1f ms", kind, ms)

    def summary(self):
        with self._lock:
            return {
                kind: {"count": len(v), "average_ms": round(sum(v) / len(v), 1), "slowest_ms": round(max(v), 1)}
                for kind, v in self._data.items() if v
            }


TIMINGS = Timings()
