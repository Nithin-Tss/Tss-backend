"""
Pure helpers for reading theme code (no database): the file list, numbered lines with caps, search with
context, the {% schema %} outline, and masking of anything that looks like a secret.

File text is merchant-controlled, so it is untrusted: callers return it inside JSON and the prompt says
it is data, never instructions.
"""
import difflib
import json
import re

TEXT_EXTENSIONS = (".liquid", ".css", ".js", ".json")
# The code editor shows these too, but they are generated from the customizer's data, not theme code.
# templates/index.json is the home page's customizer data; the home page's code is templates/index.liquid.
NOT_CODE = frozenset({"config/settings_data.json", "assets/images.json", "templates/index.json"})

MAX_LINES = 300
MAX_CHARS = 8_000
MAX_LINE_CHARS = 400
CONTEXT_LINES = 3
MAX_MATCHES = 20
MAX_OUTLINE_SETTINGS = 60

# Same rule the code editor uses for a file path (apps/themes/services.py): short segments of letters,
# digits, dot, dash, underscore.
_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")

# What a file is for, from its name. Shown only for files that exist.
HINTS = {
    "layout/theme.liquid": "Page frame around every page",
    "sections/header.liquid": "Header: announcement bar, logo, menu, search, wishlist and cart icons",
    "sections/footer.liquid": "Footer: utility bar, logo, text and link blocks, copyright",
    "snippets/product-card.liquid": "Product card used in product lists",
    "snippets/cart.liquid": "Cart icon and cart drawer",
    "snippets/wishlist.liquid": "Wishlist heart and page",
    "snippets/styles.liquid": "Global styles",
    "sections/hero.liquid": "Hero banner",
    "sections/slideshow.liquid": "Slideshow",
    "sections/product-grid.liquid": "Product grid",
    "sections/product-carousel.liquid": "Product carousel",
    "sections/main-product.liquid": "Product page",
    "sections/main-collection.liquid": "Collection page",
    "sections/main-page.liquid": "Content page",
    "sections/main-blog.liquid": "Blog page",
    "sections/main-article.liquid": "Blog article page",
    "sections/main-checkout.liquid": "Checkout page",
    "sections/newsletter.liquid": "Newsletter sign-up",
    "sections/rich-text.liquid": "Rich text",
    "sections/promotions.liquid": "Promotions",
    "templates/index.liquid": "Home page template",
    "templates/product.liquid": "Product page template",
    "templates/collection.liquid": "Collection page template",
}


def valid_path(path):
    """True for a path the code editor itself could have made (no .., no leading slash, safe characters)."""
    if not isinstance(path, str) or not path or len(path) > 200 or path.startswith("/"):
        return False
    return all(_SEGMENT.match(part) for part in path.split("/"))


def listable(rows):
    """[(path, size)] -> the theme code files to show: text files only, no folder markers, no generated ones."""
    result = []
    for path, size in rows:
        if not valid_path(path) or path in NOT_CODE or path.rsplit("/", 1)[-1] == ".keep":
            continue
        if path.lower().endswith(TEXT_EXTENSIONS):
            result.append((path, size))
    return result


def file_entry(path, size):
    text = f"{path} ({size / 1024:.1f} KB)" if size >= 1024 else f"{path} ({size} bytes)"
    hint = HINTS.get(path)
    return f"{text} - {hint}" if hint else text


def suggestions(path, names):
    """Files the person may have meant: same file name in another folder, then the closest spellings."""
    wanted = (path or "").strip().strip("/").lower()
    base = wanted.rsplit("/", 1)[-1]
    same_name = [n for n in names if n.lower().rsplit("/", 1)[-1] == base]
    close = difflib.get_close_matches(wanted, [n.lower() for n in names], n=5, cutoff=0.5)
    by_lower = {n.lower(): n for n in names}
    out = list(same_name)
    out += [by_lower[c] for c in close if by_lower[c] not in out]
    return out[:5]


# ---------------------------------------------------------------- secrets

_SECRET_PATTERNS = [
    (re.compile(r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{8,}"), "[hidden]"),
    (re.compile(r"\btssk_[A-Za-z0-9_-]{8,}"), "[hidden]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[hidden]"),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}"), "Bearer [hidden]"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "[hidden]"),
    (re.compile(
        r"""(?ix)((?:api[_-]?key|secret|token|password|passwd|private[_-]?key|authorization|client[_-]?secret)
        ["']?\s*[:=]\s*["'])([^"'\n]{8,})(["'])"""
    ), r"\1[hidden]\3"),
]


def mask_secrets(line):
    for pattern, replacement in _SECRET_PATTERNS:
        line = pattern.sub(replacement, line)
    return line


def _show(number, line):
    line = mask_secrets(line)
    if len(line) > MAX_LINE_CHARS:
        line = line[:MAX_LINE_CHARS] + " ...[line cut]"
    return f"{number}| {line}"


# ---------------------------------------------------------------- reading

def read_range(lines, start):
    """Numbered lines from `start` (1-based), at most MAX_LINES and about MAX_CHARS."""
    shown, used = [], 0
    for number in range(start, min(len(lines), start + MAX_LINES - 1) + 1):
        text = _show(number, lines[number - 1])
        if shown and used + len(text) > MAX_CHARS:
            break
        shown.append(text)
        used += len(text) + 1
    last = start + len(shown) - 1
    return {
        "code": "\n".join(shown),
        "first_line": start if shown else None,
        "last_line": last if shown else None,
        "total_lines": len(lines),
        "truncated": last < len(lines),
        "next_start_line": last + 1 if last < len(lines) else None,
    }


def find_lines(lines, term):
    """Lines containing `term` (any case), each with CONTEXT_LINES around it; overlapping windows are merged."""
    needle = term.lower()
    hits = [i for i, line in enumerate(lines, start=1) if needle in line.lower()]
    shown_hits = hits[:MAX_MATCHES]
    windows = []
    for hit in shown_hits:
        low, high = max(1, hit - CONTEXT_LINES), min(len(lines), hit + CONTEXT_LINES)
        if windows and low <= windows[-1][1] + 1:
            windows[-1][1] = max(windows[-1][1], high)
        else:
            windows.append([low, high])
    blocks, used = [], 0
    for low, high in windows:
        text = "\n".join(_show(n, lines[n - 1]) for n in range(low, high + 1))
        if blocks and used + len(text) > MAX_CHARS:
            break
        blocks.append(text)
        used += len(text)
    return {
        "matches": len(hits),
        "matching_lines": shown_hits,
        "code": "\n\n...\n\n".join(blocks),
        "total_lines": len(lines),
        "truncated": len(hits) > len(shown_hits) or len(blocks) < len(windows),
    }


# ---------------------------------------------------------------- schema outline

_SCHEMA = re.compile(r"\{%-?\s*schema\s*-?%\}(.*?)\{%-?\s*endschema\s*-?%\}", re.S)


def schema_outline(text):
    """
    What the customizer already offers for a section file: its settings and blocks, from the file's own
    {% schema %}, with the line each is written on. None when the file has no (valid) schema.
    """
    match = _SCHEMA.search(text)
    if not match:
        return None
    try:
        schema = json.loads(match.group(1))
    except ValueError:
        return None
    if not isinstance(schema, dict):
        return None

    lines = text.splitlines()
    cursor = text[: match.start(1)].count("\n")  # index of the line the schema starts on

    def find(token):
        """Line number (1-based) of the next line at or after the cursor containing token, or None."""
        nonlocal cursor
        for index in range(cursor, len(lines)):
            if token in lines[index]:
                cursor = index
                return index + 1
        return None

    def setting_text(item):
        if not isinstance(item, dict) or "id" not in item:
            return None
        line = find(f'"id": "{item["id"]}"')
        label = item.get("label") or item["id"]
        where = f", line {line}" if line else ""
        return f'{label} [{item.get("type", "?")}] id={item["id"]}{where}'

    settings = []
    for item in schema.get("settings") or []:
        entry = setting_text(item)
        if entry and len(settings) < MAX_OUTLINE_SETTINGS:
            settings.append(entry)

    blocks = []
    for block in schema.get("blocks") or []:
        if not isinstance(block, dict):
            continue
        line = find(f'"type": "{block.get("type")}"')
        entry = {
            "name": block.get("name") or block.get("type"),
            "type": block.get("type"),
            "line": line,
            "settings": [s for s in (setting_text(i) for i in block.get("settings") or []) if s][:20],
        }
        blocks.append(entry)

    return {
        "section_name": schema.get("name"),
        "max_blocks": schema.get("max_blocks"),
        "settings": settings,
        "blocks": blocks,
    }


MAX_EDIT_LINES = 40
MAX_EDIT_CHARS = 4_000


def line_edit(text, start, end, new_text):
    """
    Replace lines start..end (1-based, inclusive) of `text` with `new_text`.
    Returns (old_text, new_file_text), or None when the range does not fit the file. Pure: nothing is saved.
    """
    if not isinstance(text, str) or "\r" in text:
        return None
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()  # the file's final newline is not a line
        trailing = "\n"
    else:
        trailing = ""
    if not (isinstance(start, int) and isinstance(end, int)) or not 1 <= start <= end <= len(lines):
        return None
    new_lines = new_text.rstrip("\n").split("\n") if new_text.strip("\n") else []
    old = "\n".join(lines[start - 1:end])
    return old, "\n".join(lines[:start - 1] + new_lines + lines[end:]) + trailing
