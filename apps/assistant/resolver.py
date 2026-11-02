"""
Theme-code requests, decided in Python: which file, which lines, what the customizer already offers.

The facts built here are turned into one guided step at a time by steps.py (the model is not asked), and every
step is checked afterwards (check_answer) as a safety net. This module also reads what the person means by a
short message ("next", "i did not understand", "show code").

Pure logic: it works on a file list and a `read(path)` callable, so tests need no database. In the running
assistant `read` is the read-only queries.theme_file_text (see tools.open_theme). File text is the merchant's,
so it is untrusted data.
"""
import difflib
import re
from dataclasses import dataclass, field

from . import themecode

MAX_FILES_READ = 5
EXCERPT_RADIUS = 12
MAX_EXCERPT_LINES = 40
MAX_EXCERPT_CHARS = 3_000
MAX_URLS = 10
MAX_CLASSES = 8
MAX_CUSTOMIZER = 8
MAX_PASTE_CHARS = 8_000

CHANGE_VERBS = re.compile(
    r"\b(add|change|hide|show(?!\s+me)|move|remove|delete|replace|edit|update|make|put|insert|rename|style|resize|align|"
    r"center|centre|swap|fix|display|turn|increase|decrease|bigger|smaller|bold|colou?r|link)\b"
)
FOLLOW_UP = re.compile(
    r"\b(which|where|exactly|show me|do it|that|this|why|again|same|code|line|snippet|file|there|it)\b"
)
TOP_WORDS = re.compile(r"\b(top|above|before|beginning|start|first)\b")

# Short messages that steer a guided answer. A guided answer is a numbered list written by steps.py (it starts
# with "1. "), or the review of lines the person pasted (it starts with REVIEW_MARK).
GUIDED_LIST = re.compile(r"\A\s*1\.\s")
REVIEW_MARK = "I checked your lines"
PASTE_MORE = "paste a few lines below your button too"  # the review asks for more lines with these words
LOOSE_CODE = re.compile(r"</?\w|\{%|\{\{")  # even a bare </div> or {% endfor %} counts when more lines were asked for
MAX_JOINED_PASTES = 3
# A short follow-up right after a guided answer: "what should i do", "then", "and now", "ok so", "help", "where"
AGAIN_MESSAGE = re.compile(
    r"\s*(?:what\s+(?:should|do|can|shall)\s+(?:i|we)\s+do(?:\s+(?:now|next|then))?|what\s+(?:now|next|else)|now\s+what|"
    r"(?:and\s+)?(?:then|now)|and\s+then\s+what|(?:ok(?:ay)?\s+)?so|help(?:\s+me)?|where|how|which\s+one)\s*[.!?]*\s*",
    re.I,
)
NEXT_MESSAGE = re.compile(
    r"\s*(?:next(?: step)?|continue|go on|done|ok(?:ay)?|yes|yep|finished|completed|did it|tarvata|tharvata|next ra)\s*[.!]*\s*",
    re.I,
)
SHOW_CODE = re.compile(r"\b(?:show|give|send)\s+(?:me\s+)?(?:the\s+)?(?:exact\s+)?code\b|\bexact code\b|\bcode please\b", re.I)
NOT_CLEAR = re.compile(
    r"not clear|unclear|explain\b.{0,12}\bagain|in (?:very )?simple|simpler|confus|"
    r"(?:ardam|artham|ardham)\s*(?:kale?du|avvale?du|kaale?du)|malli\s*chepp?u",
    re.I,
)
_NEGATIONS = frozenset("didnt didn dint ddint dont don cant cannot couldnt couldn not no never".split())
_UNDERSTAND = ("understand", "understood", "follow", "clear")
ASK_TO_PASTE = "paste them here"

PAGE_LABELS = {
    "index": "Home page", "product": "Product page", "collection": "Collection page", "cart": "Cart page",
    "blog": "Blog page", "article": "Article page", "checkout": "Checkout page", "page": "Page",
    "login": "Login page", "account": "Account page",
}


def _last_assistant_text(history):
    for item in reversed(list(history or ())):
        if item.get("role") == "assistant":
            return item.get("content") or ""
    return ""


def in_guided_answer(history):
    """True when the latest assistant message is a guided list or the review of pasted lines."""
    previous = _last_assistant_text(history)
    return bool(GUIDED_LIST.match(previous)) or previous.startswith(REVIEW_MARK)


def intent(message, history=()):
    """
    "done" (the person says they are finished), "again" (a short follow-up such as "what should i do"), "simple"
    (did not understand), "code" (show the code) or "start" (anything else). "done" and "again" need a guided
    answer just before them; "simple" needs a guided or a theme answer.
    """
    text = (message or "").strip()
    if not text or len(text) > 80:
        return "start"
    if SHOW_CODE.search(text):
        return "code"
    previous = _last_assistant_text(history)
    in_steps = in_guided_answer(history)
    if in_steps and NEXT_MESSAGE.fullmatch(text):
        return "done"
    if in_steps and AGAIN_MESSAGE.fullmatch(text):
        return "again"
    if in_steps or "Online Store" in previous:
        words = re.findall(r"[a-z]+", text.lower())
        said_no = any(w in _NEGATIONS for w in words)
        near = any(difflib.get_close_matches(w, _UNDERSTAND, n=1, cutoff=0.75) for w in words)
        short_plea = len(words) <= 6 and any(w in ("simple", "easy", "easier") for w in words)
        if NOT_CLEAR.search(text) or (said_no and near) or short_plea:
            return "simple"
    return "start"


def looks_like_code(text):
    return len(text) > 300 or bool(re.search(r"<\w|\{%|\{\{", text))


def is_paste(message, history):
    """Lines of code or markup. After a review asked for more lines, even a bare closing tag counts."""
    if looks_like_code(message) or _FENCE.search(message or ""):
        return True
    return PASTE_MORE in _last_assistant_text(history) and bool(LOOSE_CODE.search(message or ""))


def _paste_body(text):
    fenced = _FENCE.search(text or "")
    return fenced.group(1) if fenced else (text or "")


def joined_paste(history, message):
    """
    This paste with the pastes before it, when each review in between asked for more lines ("paste a few lines
    below your button too"): the owner is not asked for the same lines again. At most MAX_JOINED_PASTES.
    """
    items, earlier, index = list(history or ()), [], len(list(history or ())) - 1
    while index >= 1 and len(earlier) < MAX_JOINED_PASTES - 1:
        answer, asked = items[index], items[index - 1]
        if answer.get("role") != "assistant" or PASTE_MORE not in (answer.get("content") or "") \
                or asked.get("role") != "user":
            break
        earlier.insert(0, _paste_body(asked["content"]))
        index -= 2
    new = _paste_body(message)
    kept = [part for part in earlier if _normal(part) not in _normal(new)]  # the new paste may repeat the old one
    return "\n".join(kept + [new])


def reviewing(history):
    """True when a paste now would be a review: the last answer was guided and did not ask for lines."""
    return in_guided_answer(history) and ASK_TO_PASTE not in _last_assistant_text(history).lower()


def _file_of_answer(history, names):
    """The theme file the last guided answer is about (the first one it names that exists), else ''."""
    known = set(names)
    return next((p for p in _THEME_PATH.findall(_last_assistant_text(history)) if p in known), "")


def _earlier_paste(history, names):
    """(path, text) of the latest lines pasted in answer to "paste them here", else ("", "")."""
    items = list(history or ())
    for index in range(len(items) - 1, 0, -1):
        item, before = items[index], items[index - 1]
        if item.get("role") == "user" and looks_like_code(item["content"]) and before.get("role") == "assistant" \
                and ASK_TO_PASTE in (before.get("content") or "").lower():
            asked = [p for p in _THEME_PATH.findall(before["content"]) if p in names]
            if asked:
                return asked[0], item["content"][:MAX_PASTE_CHARS]
    return "", ""

# Page words -> template name (templates/<name>.liquid)
PAGES = {
    "index": r"\bhome(?:\s?page)?\b|\bfront page\b|\blanding page\b",
    "product": r"\bproduct (?:detail )?page\b|\bproduct template\b",
    "collection": r"\bcollections? (?:page|template)\b|\bcategory page\b",
    "cart": r"\bcart page\b|\bcart template\b",
    "blog": r"\bblog(?: page)?\b",
    "article": r"\barticle\b|\bblog post\b",
    "checkout": r"\bcheckout\b",
    "page": r"\bcontent page\b|\babout page\b|\bcontact page\b|\bpage template\b",
    "login": r"\blog ?in\b|\bsign ?in\b",
    "account": r"\baccount page\b",
}

# Element words. `types`: pieces of a section type that render it. `files`: where it lives when it is not a
# section of a page template. `finds`: words to search for when nothing else matches. `anchors`: regexes for
# the line to point at. `modifier`: only shapes the snippet, never picks a file. `wrapper`: "above" means above the
# container of its loop. `css`: the anchor may be inside <style>.
ELEMENTS = {
    "announcement": dict(
        label="announcement bar", words=r"announcement|promo bar|notice bar|banner bar",
        types=("announcement",), files=("sections/announcement-bar.liquid", "sections/header.liquid"),
        finds=("announcement",), anchors=(r"announcement",)),
    "header": dict(
        label="header", words=r"header|top bar|navbar", types=("header",), files=("sections/header.liquid",),
        finds=("<header",), anchors=(r"<header",)),
    "footer": dict(
        label="footer", words=r"footer", types=("footer",), files=("sections/footer.liquid",),
        finds=("<footer",), anchors=(r"<footer",)),
    "logo": dict(
        label="logo", words=r"logo|brand name", types=("header",), files=("sections/header.liquid",),
        finds=("logo",), anchors=(r"logo",)),
    "menu": dict(
        label="menu", words=r"menu|navigation|nav links?", types=("header",), files=("sections/header.liquid",),
        finds=("menu", "<nav"), anchors=(r"<nav", r"menu")),
    "search": dict(
        label="search", words=r"search", types=("header", "search"), files=("sections/header.liquid",),
        finds=("search",), anchors=(r"search",)),
    "cart icon": dict(
        label="cart icon", words=r"cart icon|cart drawer|basket|bag icon|mini cart",
        types=("cart",), files=("snippets/cart.liquid", "sections/header.liquid"),
        finds=("cart",), anchors=(r"cart",)),
    "product card": dict(
        label="product card", words=r"product card|product tile|card", types=("product-card",),
        files=("snippets/product-card.liquid",), finds=("product-card", "product.title"),
        anchors=(r"product\.title", r"<h\d")),
    "product grid": dict(
        label="product grid", words=r"all products|product grid|product list(?:ing)?|featured products|products",
        types=("product-grid", "product-carousel", "featured", "collection-list", "product-list", "products"),
        files=("sections/product-grid.liquid", "sections/product-carousel.liquid"),
        finds=("for product in", "product-grid"),
        anchors=(r"class=[\"'][^\"']*(?<![\w-])grid(?![\w-])", r"for\s+\w+\s+in\s+\S*products", r"product-grid"),
        wrapper=True),
    "banner": dict(
        label="banner/hero", words=r"banner|hero|slideshow|slider|carousel",
        types=("hero", "slideshow", "banner", "image-banner"),
        files=("sections/hero.liquid", "sections/slideshow.liquid"), finds=("hero", "slide"),
        anchors=(r"hero", r"slide", r"<h1")),
    "newsletter": dict(
        label="newsletter", words=r"newsletter|subscribe|sign.?up form|email form", types=("newsletter",),
        files=("sections/newsletter.liquid",), finds=("newsletter", "subscribe"), anchors=(r"<form", r"newsletter")),
    "colors": dict(
        label="colors", words=r"colou?rs?|background|theme colou?r", types=(), files=("snippets/styles.liquid",),
        finds=("--color", "color:"), anchors=(r"--color", r"color:"), css=True),
    "fonts": dict(
        label="fonts", words=r"fonts?|typography|typeface", types=(), files=("snippets/styles.liquid",),
        finds=("font-family", "--font"), anchors=(r"font-family", r"--font"), css=True),
    "button": dict(
        label="button", words=r"button|btn|cta", modifier=True, types=(), files=(),
        finds=('class="btn', "<button"), anchors=()),
}

_STOP = frozenset(
    "a an the to of in on for and or i my me do how can where what is are it this that please want need would like "
    "add change make put page store shop admin find exactly which file line code show hide remove update home".split()
)

_SECTION_LINE = re.compile(
    r"""\{%-?\s*section\s+(?P<q>['"])(?P<id>[^'"]+)(?P=q)\s*,\s*type:\s*(?P<tq>['"])(?P<type>[^'"]+)(?P=tq)"""
    r"""(?P<rest>[^%]*)-?%\}"""
)
_CLASS = re.compile(r"""class\s*=\s*["']([^"']+)["']""")
_HREF = re.compile(r"""href\s*=\s*["']([^"'{][^"']*)["']""")
_ROUTE = re.compile(r"\broutes\.[a-z_]+")
_THEME_PATH = re.compile(r"\b(?:sections|snippets|templates|assets|layout|config|locales)/[A-Za-z0-9._/-]*[A-Za-z0-9]")
_LINE_REF = re.compile(r"\blines?\s+(\d{1,5})(?:\s*(?:-|–|—|to|and)\s*(\d{1,5}))?", re.I)
_LINK = re.compile(r"""(?<![\w.\-/<:])(/[A-Za-z][\w\-./]*|https?://[^\s)"'`<>]+)""")
_FENCE = re.compile(r"```[A-Za-z]*\n?(.*?)```", re.S)
_PASTE_INTRO = re.compile(r"(?i)\bthat(?:'s| is)? the code (?:in|of)\s+([A-Za-z0-9._/-]+)\s*[:.\-]?\s*(.*)", re.S)
_VARIABLE = re.compile(r"\{\{|\{%|\bsection\.settings|\bblock\.settings|\bs\.[a-z_]+|\bsettings\.[a-z_]+|\b[a-z]+_[a-z_]*(?:label|link)\b")
_QUOTED = re.compile(r"""["“']([^"”'\n]{1,30})["”']""")


# ---------------------------------------------------------------- reading a page template

def parse_sections(text):
    """The {% section 'id', type: 'x'[, hidden: true] %} lines of a page template: [{line, id, type, hidden}]."""
    found = []
    for number, line in enumerate((text or "").splitlines(), start=1):
        match = _SECTION_LINE.search(line)
        if match:
            found.append({
                "line": number, "id": match["id"], "type": match["type"],
                "hidden": bool(re.search(r"hidden:\s*true", match["rest"])),
            })
    return found


def is_legacy_template(text):
    """A template that is still the starter's {{ sections }} form: it lists no sections line by line."""
    return bool(text) and not parse_sections(text) and "{{" in text and "sections" in text


# ---------------------------------------------------------------- the request

@dataclass
class Request:
    pages: list = field(default_factory=list)
    elements: list = field(default_factory=list)  # keys of ELEMENTS, targets first
    keywords: list = field(default_factory=list)
    wants_change: bool = False
    above: bool = False
    label: str = ""  # a quoted button text, if the person wrote one
    pasted_path: str = ""
    pasted_text: str = ""
    review: bool = False  # the pasted lines are the person's own edit after a guided answer, not a new request

    @property
    def targets(self):
        return [k for k in self.elements if not ELEMENTS[k].get("modifier")]

    @property
    def usable(self):
        return bool(self.pages or self.elements)


def _words_in(text):
    pages = [name for name, pattern in PAGES.items() if re.search(pattern, text)]
    elements = []
    for key, spec in ELEMENTS.items():
        if re.search(rf"\b(?:{spec['words']})\b", text):
            elements.append(key)
    # "all products" is the more specific one when "product card" is also present
    elements.sort(key=lambda k: (bool(ELEMENTS[k].get("modifier")), list(ELEMENTS).index(k)))
    return pages, elements


def parse_paste(message, names):
    """(path, text) when the message pastes code, or says "that is the code in <file>"; else ("", "")."""
    fence = _FENCE.search(message or "")
    intro = _PASTE_INTRO.search(message or "")
    path = ""
    for candidate in _THEME_PATH.findall(message or ""):
        if candidate in names:
            path = candidate
            break
    if not path and intro and intro.group(1) in names:
        path = intro.group(1)
    text = ""
    if fence:
        text = fence.group(1)
    elif intro and intro.group(2).strip():
        text = intro.group(2)
    if not text.strip() or not path:
        return "", ""
    return path, text[:MAX_PASTE_CHARS]


def analyse(message, history=(), names=()):
    """
    What the person is asking for, from the new message and, for a follow-up ("exactly which file?"), from the
    earlier user messages of this chat.
    """
    message = message or ""
    request = Request()
    request.pasted_path, request.pasted_text = parse_paste(message, set(names))
    pasted_on_request = False
    if not request.pasted_text:  # the lines the assistant asked for: pasted without a file name or a fence
        previous = _last_assistant_text(history)
        if ASK_TO_PASTE in previous.lower() and looks_like_code(message):
            asked = [p for p in _THEME_PATH.findall(previous) if p in set(names)]
            if asked:
                request.pasted_path, request.pasted_text = asked[0], message[:MAX_PASTE_CHARS]
                pasted_on_request = True
    if not pasted_on_request and is_paste(message, history) and reviewing(history):
        # Lines pasted after a guided code answer, with or without a file name or a fence, are the person's edit
        path = request.pasted_path if request.pasted_path in set(names) else _file_of_answer(history, names)
        if path:
            request.review, request.pasted_path = True, path
            request.pasted_text = joined_paste(history, message)[:MAX_PASTE_CHARS]
            pasted_on_request = True
    steering = intent(message, history) != "start"
    if steering and not request.pasted_text:  # "done" after the lines were pasted: they still count
        request.pasted_path, request.pasted_text = _earlier_paste(history, set(names))
    # Words inside pasted code say nothing about what the person wants: take that from the earlier messages
    text = "" if pasted_on_request else message.lower()
    pages, elements = _words_in(text)
    request.wants_change = bool(CHANGE_VERBS.search(text))

    if not (pages or elements) and (FOLLOW_UP.search(text) or request.pasted_text or steering):
        # a long guided chat is mostly "next": look further back for the request itself
        earlier = [h["content"] for h in history if h.get("role") == "user" and not looks_like_code(h["content"])]
        earlier = earlier[-(15 if steering or request.review else 3):]
        for content in reversed(earlier):
            pages, elements = _words_in(content.lower())
            if pages or elements:
                request.wants_change = request.wants_change or bool(CHANGE_VERBS.search(content.lower()))
                text = f"{content.lower()} {text}"
                if not request.label:
                    quoted = _QUOTED.search(content)
                    request.label = quoted.group(1).strip() if quoted else ""
                break

    request.pages, request.elements = pages, elements
    request.above = bool(TOP_WORDS.search(text))
    quoted = None if pasted_on_request else _QUOTED.search(message)
    if quoted and not request.label:
        request.label = quoted.group(1).strip()
    words = re.findall(r"[a-z]{4,}", text)
    request.keywords = [w for w in dict.fromkeys(words) if w not in _STOP][:12]
    return request


def is_theme_request(request):
    """A change to something on the site: a page or element word plus a change word (or a pasted file)."""
    return bool(request.pasted_text) or (request.usable and request.wants_change)


# ---------------------------------------------------------------- facts

@dataclass
class Facts:
    found: bool = False
    theme_note: str = ""
    primary: str = ""
    reason: str = ""
    template: str = ""  # the page template that lists the primary file, if any
    template_line: int = 0
    hidden: bool = False
    candidates: list = field(default_factory=list)  # [(path, reason)]
    total_lines: int = 0
    anchor: int = 0
    customizer: list = field(default_factory=list)
    classes: list = field(default_factory=list)
    urls: list = field(default_factory=list)
    excerpt: str = ""
    snippet_tag: str = ""  # an existing <a>/<button> opening class list, for a button snippet
    names: frozenset = frozenset()
    line_counts: dict = field(default_factory=dict)
    texts: dict = field(default_factory=dict)
    elements: list = field(default_factory=list)
    above: bool = False
    label: str = ""
    pasted: bool = False
    read_paths: list = field(default_factory=list)
    page: str = ""  # the page key ("index"), for its name in the customizer
    section_name: str = ""  # the section's name in the customizer
    custom_items: list = field(default_factory=list)  # [(kind, label, block)] kind: toggle/setting/block/block_setting
    anchor_text: str = ""  # the markup on the anchor line, when it holds no Liquid
    anchor_is_if: bool = False  # the anchor line starts an {% if %} block: the new code goes just above it


_BLOCK_OPEN = re.compile(
    r"<(?P<tag>style|script)\b[^>]*>|\{%-?\s*(?P<liquid>stylesheet|style|javascript|schema)\b[^%]*%\}", re.I
)
_BLOCK_CLOSE = {
    "style": re.compile(r"</style\s*>", re.I),
    "script": re.compile(r"</script\s*>", re.I),
    "stylesheet": re.compile(r"\{%-?\s*endstylesheet\s*-?%\}", re.I),
    "liquid-style": re.compile(r"\{%-?\s*endstyle\s*-?%\}", re.I),
    "javascript": re.compile(r"\{%-?\s*endjavascript\s*-?%\}", re.I),
}
_LOOP = re.compile(r"\{%-?\s*for\s+\w+\s+in\s")
_WRAPPER_OPEN = re.compile(r"<(?:div|ul|ol|section|nav)\b[^>]*\bclass\s*=", re.I)
_WRAPPER_CLOSE = re.compile(r"</(?:div|ul|ol|section|nav)\s*>", re.I)


def _markup_lines(lines, css=False):
    """
    [(number, text)] of the markup: what is left of each line outside <style>, <script>, {% style %},
    {% stylesheet %} and {% javascript %} blocks, and nothing from {% schema %} on. With css=True the style
    blocks stay (for colors and fonts, which live there). Numbers are the real line numbers of the file.
    """
    out = []
    closing = None
    for number, line in enumerate(lines, start=1):
        pos, visible, stop = 0, [], False
        while pos < len(line):
            if closing:
                found = closing.search(line, pos)
                if not found:
                    break
                pos, closing = found.end(), None
                continue
            found = _BLOCK_OPEN.search(line, pos)
            if not found:
                visible.append(line[pos:])
                break
            visible.append(line[pos:found.start()])
            kind = (found["tag"] or found["liquid"]).lower()
            if kind == "schema":
                stop = True  # the customizer's JSON is not markup
                break
            if css and kind in ("style", "stylesheet"):
                visible.append(line[found.start():found.end()])
                pos = found.end()
                continue
            closing = _BLOCK_CLOSE["liquid-style" if found["liquid"] and kind == "style" else kind]
            pos = found.end()
        text = "".join(visible)
        if text.strip():
            out.append((number, text))
        if stop:
            break
    return out


_TAG = re.compile(r"</?(?:div|ul|ol|section|nav)\b[^>]*>", re.I)
_COND_TAG = re.compile(r"\{%-?\s*(if|unless|case|endif|endunless|endcase)\b")
_IF_LINE = re.compile(r"\A\s*\{%-?\s*(?:if|unless|case)\b")
# Arrows, dots, pagination and other carousel buttons: never the place for something "on top of the products"
_CONTROLS = re.compile(r"arrow|control|paginat|dots?\b|\bprev|\bnext|swiper|slick|carousel__(?:btn|button|nav)", re.I)


def _conditional_frames(body):
    """
    {line: [line of the {% if %} / {% unless %} / {% case %} the line sits inside, outermost first]} for the markup
    lines. An if that wraps the whole section does not count: everything is inside it.
    """
    if not body:
        return {}
    stack, opened, closes = [], {}, {}
    for number, text in body:
        opened[number] = list(stack)
        for tag in _COND_TAG.finditer(text):
            if tag.group(1).startswith("end"):
                if stack:
                    closes[stack.pop()] = number
            else:
                stack.append(number)
    first, last = body[0][0], body[-1][0]
    whole = {first} if closes.get(first, last) == last else set()
    return {number: [o for o in opens if o not in whole] for number, opens in opened.items()}


def _outside_conditionals(number, frames):
    """The line itself, or the line of the outermost {% if %} around it (a button goes just above that line)."""
    opens = frames.get(number)
    return opens[0] if opens else number


def anchor_not_in_conditional(lines, anchor):
    """The check: the anchor line is not inside an {% if %}...{% endif %} block (other than one around the section)."""
    return not _conditional_frames(_markup_lines(lines)).get(anchor)


def _grid_wrapper(body, frames):
    """
    The line to put something above the products at, or 0: the opening tag of the container that wraps the product
    loop, else the section's first heading or wrapper above the loop. Control lines are skipped, and a line inside
    an {% if %} moves up to that {% if %} line.
    """
    loop = next((n for n, t in body if _LOOP.search(t) and "product" in t.lower()), 0)
    if not loop:
        return 0
    pick = _enclosing_opener(body, loop)
    if not pick:
        before = [(n, t) for n, t in body if n < loop and not _CONTROLS.search(t)]
        pick = next((n for n, t in before if _WRAPPER_OPEN.search(t) or re.search(r"<h[1-6]\b", t)), loop)
    return _outside_conditionals(pick, frames)


def _enclosing_opener(body, loop):
    """
    The line of the container tag that is still open where the loop starts, or 0. Boxes that were opened and closed
    before the loop (the arrows block, for one) are skipped: their closing tags are counted on the way up.
    """
    depth = 0
    for number, text in reversed([(n, t) for n, t in body if n <= loop]):
        if number == loop:
            found = _LOOP.search(text)
            text = text[:found.start()] if found else text
        for tag in reversed(_TAG.findall(text)):
            if tag.startswith("</"):
                depth += 1
            elif tag.endswith("/>"):
                continue
            elif depth:
                depth -= 1
            elif not _CONTROLS.search(text):
                return number
    return 0


def _anchor_line(lines, spec, above=False):
    """
    The markup line to point at, 0 when none fits. With "above" and a grid element it is the opening line of
    the loop's wrapper (never a control line, never inside an {% if %}); otherwise the first line matching the
    element's first pattern, then its second, and so on.
    """
    body = _markup_lines(lines, css=bool(spec and spec.get("css")))
    on_top = bool(above and spec and spec.get("wrapper"))
    frames = _conditional_frames(body) if on_top else {}
    if on_top:
        found = _grid_wrapper(body, frames)
        if found:
            return found
    patterns = (spec.get("anchors") if spec else ()) or ()
    for pattern in patterns:
        regex = re.compile(pattern, re.I)
        for number, text in body:
            if regex.search(text) and not (on_top and _CONTROLS.search(text)):
                return _outside_conditionals(number, frames) if on_top else number
    if patterns:
        return 0  # the element is not in the markup: say what to look for rather than point at CSS or JS
    for number, text in body:  # no element: the first real line
        if not re.search(r"\{%-?\s*comment", text):
            return number
    return 1


@dataclass
class Edit:
    """What the person's own pasted edit looks like next to the facts of the real file (see read_edit)."""
    verdict: str  # "missing" (no new button seen) | "inside" (in an if block) | "below" | "right" | "unsure"
    target: str = ""  # Liquid-free text of the line the new code belongs just above, when it can be quoted
    target_is_if: bool = False  # the new code belongs above an {% if %} line (it cannot be quoted)
    check_if: bool = False  # the real products sit inside an {% if %} that the pasted lines do not show


def _paste_blocks(body):
    """
    {line: first line of the outermost {% if %} block around it, or 0 when that block opens above the pasted
    lines (an {% endif %} with no {% if %} in the paste)} for the lines that sit inside a conditional.
    """
    stack, blocks, unmatched = [], {}, 0
    for number, text in body:
        if stack:
            blocks[number] = stack[0]
        for tag in _COND_TAG.finditer(text):
            if not tag.group(1).startswith("end"):
                stack.append(number)
            elif stack:
                stack.pop()
            else:
                unmatched = number
    for number, _ in body:
        if number < unmatched:
            blocks.setdefault(number, 0)
    return blocks


def read_edit(pasted, facts):
    """
    Compare lines the person pasted after an answer (their edit) with the facts of the real file: is their new
    link/button above the product loop's wrapper and outside every {% if %}? Works on the pasted text only for
    where things are; the real file only supplies the wrapper's text and whether its products sit inside an if.
    """
    body = _markup_lines((pasted or "").splitlines())
    blocks = _paste_blocks(body)
    original = {_normal(line) for line in (facts.texts.get(facts.primary) or "").splitlines()}
    label = (facts.label or "").lower()
    mine = next((n for n, t in body if _normal(t) not in original
                 and (re.search(r"<(?:a|button)\b", t, re.I) or (label and label in t.lower()))), 0)
    if not mine:
        return Edit("missing")
    if mine in blocks:
        return Edit("inside", target="" if facts.anchor_is_if else facts.anchor_text, target_is_if=facts.anchor_is_if)

    loop = next((n for n, t in body if _LOOP.search(t) and "product" in t.lower()), 0)
    in_block = blocks.get(loop)  # None: the loop is outside every if; 0: inside one opened above the paste
    if in_block == 0:
        return Edit("unsure")
    shown = dict(body)
    if in_block:  # the loop is inside an if of the paste: the new code belongs above that if line
        target, is_if = in_block, True
    else:
        wanted = _normal(facts.anchor_text)
        target = next((n for n, t in body if wanted and _normal(t) == wanted), 0)
        if not target and loop:
            target = next((n for n, t in reversed([x for x in body if x[0] <= loop])
                           if _WRAPPER_OPEN.search(t) and not _WRAPPER_CLOSE.search(t)), loop)
        is_if = False
    if not target:
        return Edit("unsure")
    text = shown.get(target, "").strip()
    quote = "" if is_if or "{" in text or len(text) > 100 else text.replace("`", "")
    return Edit("right" if mine < target else "below", target=quote, target_is_if=is_if,
                check_if=facts.anchor_is_if and not is_if)


def _excerpt(lines, anchor):
    low = max(1, anchor - EXCERPT_RADIUS)
    high = min(len(lines), low + MAX_EXCERPT_LINES - 1)
    shown, used = [], 0
    for number in range(low, high + 1):
        text = themecode._show(number, lines[number - 1])
        if shown and used + len(text) > MAX_EXCERPT_CHARS:
            break
        shown.append(text)
        used += len(text) + 1
    return "\n".join(shown)


def _classes_near(lines, anchor):
    window = "\n".join(lines[max(0, anchor - 7): anchor + 6])
    found = []
    for group in _CLASS.findall(window):
        for name in group.split():
            if "{" not in name and name not in found:
                found.append(name)
    return found[:MAX_CLASSES]


def _urls_in(texts):
    found = []
    for text in texts.values():
        for url in _HREF.findall(text) + _ROUTE.findall(text):
            if (url.startswith(("/", "http", "routes.")) and url not in found) and url != "/":
                found.append(url)
    return found[:MAX_URLS]


_OUTLINE_SETTING = re.compile(r"(?P<label>.*?) \[(?P<type>[^\]]*)\] id=")


def _setting_label(entry):
    """(label, type) of a themecode outline entry "Label [type] id=x, line n": the owner sees the label only."""
    found = _OUTLINE_SETTING.match(entry)
    return (found["label"], found["type"]) if found else (entry, "")


def _customizer_matches(text, keywords):
    """(section name, [(kind, label, block)]) of the settings and blocks whose label fits the request."""
    outline = themecode.schema_outline(text) if text else None
    if not outline:
        return "", []
    keys = [k.lower() for k in keywords]
    items = []

    def hit(*parts):
        low = " ".join(parts).lower()
        return any(k in low for k in keys)

    for setting in outline["settings"]:
        label, kind = _setting_label(setting)
        if hit(setting):
            items.append(("toggle" if kind == "checkbox" else "setting", label, ""))
    for block in outline["blocks"]:
        name = str(block.get("name") or block.get("type"))
        if hit(name, str(block.get("type"))):
            items.append(("block", name, ""))
        for setting in block["settings"]:
            label, kind = _setting_label(setting)
            if hit(setting):
                items.append(("block_toggle" if kind == "checkbox" else "block_setting", label, name))
    items = items[:MAX_CUSTOMIZER]
    return (str(outline.get("section_name") or "") if items else ""), items


def resolve(request, names, read, theme_note=""):
    """
    Decide the file and gather the facts. `names` is the theme's code file list; `read(path)` returns its text
    or None. At most MAX_FILES_READ files are read.
    """
    names = list(names)
    known = set(names)
    texts = {}
    budget = {"left": MAX_FILES_READ}

    def get(path):
        if path in texts:
            return texts[path]
        if path not in known or budget["left"] <= 0:
            return None
        budget["left"] -= 1
        text = read(path)
        if text is not None:
            texts[path] = text
        return text

    facts = Facts(theme_note=theme_note, names=frozenset(known), above=request.above, label=request.label,
                  elements=list(request.elements))

    if request.pasted_path and not request.review:  # the person's own copy of the file wins over the stored one
        texts[request.pasted_path] = request.pasted_text
        facts.pasted = True

    targets = request.targets
    specs = [ELEMENTS[k] for k in targets]
    candidates = []  # (path, reason, template, template_line, hidden, spec)

    # 1. the page's own template lists its sections
    pages = list(request.pages) or (["index"] if any(s["types"] for s in specs) else [])
    for page in pages:
        template = f"templates/{page}.liquid"
        text = get(template)
        if text is None:
            continue
        sections = parse_sections(text)
        for section in sections:
            for key, spec in zip(targets, specs):
                if any(t in section["type"].lower() for t in spec["types"]):
                    path = f"sections/{section['type']}.liquid"
                    if path in known:
                        why = f"it renders the {spec['label']}: {template} lists it on line {section['line']}"
                        candidates.append((path, why, template, section["line"], section["hidden"], spec))
        if not candidates and (is_legacy_template(text) or not sections):
            # no section lines: the template itself may hold the markup
            for spec in specs:
                if any(re.search(p, text, re.I) for p in spec["anchors"]):
                    why = f"the {spec['label']} is written directly in {template}"
                    candidates.append((template, why, "", 0, False, spec))
                    break
        if not candidates and not targets:
            candidates.append((template, f"it is the template of the {page} page", "", 0, False, None))
        if candidates:
            break

    # 2. well-known files of the element
    if not candidates:
        for spec in specs:
            for path in spec["files"]:
                if path in known:
                    candidates.append((path, f"it renders the {spec['label']}", "", 0, False, spec))
                    break
            if candidates:
                break

    # 3. search the files whose name fits, then the files read so far
    if not candidates and specs:
        for spec in specs:
            words = [w for w in re.split(r"\W+", spec["label"]) if len(w) > 3] + [t for t in spec["types"]]
            pool = [n for n in names if any(w in n.lower() for w in words)] + list(texts)
            pool += [n for n in names if n.startswith(("sections/", "snippets/", "layout/"))]
            for path in dict.fromkeys(pool):
                text = get(path)
                if text and any(term.lower() in text.lower() for term in spec["finds"]):
                    candidates.append((path, f"it mentions the {spec['label']}", "", 0, False, spec))
                    break
            if candidates:
                break

    if not candidates and request.pasted_path and not request.review:
        candidates.append((request.pasted_path, "you pasted its code", "", 0, False, specs[0] if specs else None))

    if not candidates:
        return facts  # found = False

    primary, why, template, template_line, hidden, spec = candidates[0]
    text = get(primary)
    if text is None:
        return facts
    lines = text.splitlines()
    css = bool(spec and spec.get("css"))
    markup = _markup_lines(lines, css)
    anchor = _anchor_line(lines, spec, request.above)
    if anchor and request.above and spec and spec.get("wrapper") and not anchor_not_in_conditional(lines, anchor):
        anchor = 0  # never point inside an {% if %}: ask for the lines instead
    focus = anchor or (markup[0][0] if markup else 1)  # no matching line: show the top of the markup
    facts.found = True
    facts.primary, facts.reason = primary, why
    facts.template, facts.template_line, facts.hidden = template, template_line, hidden
    facts.candidates = [(c[0], c[1]) for c in candidates[1:4]]
    facts.total_lines = len(lines)
    facts.anchor = anchor
    # Words that only name the target itself (products, header...) would match every setting of its section
    own = " ".join(ELEMENTS[k]["words"] for k in targets)
    keys = [k for k in request.keywords if k not in own]
    if request.label:
        keys.append(request.label.lower())
    facts.section_name, facts.custom_items = _customizer_matches(text, keys)
    facts.customizer = [label for _, label, _ in facts.custom_items]
    facts.page = (template.split("/")[-1].removesuffix(".liquid") if template else "") or (
        request.pages[0] if request.pages else "")
    anchor_markup = dict(markup).get(anchor, "").strip() if anchor else ""
    facts.anchor_text = anchor_markup[:100] if anchor_markup and "{" not in anchor_markup else ""
    facts.anchor_is_if = bool(anchor and _IF_LINE.match(dict(markup).get(anchor, "")))
    facts.classes = _classes_near(lines, anchor) if anchor else []
    facts.excerpt = _excerpt(lines, focus)
    plain = "\n".join(t for _, t in markup if not _CONTROLS.search(t))  # not the carousel's own arrows and dots
    tag = re.search(r"""<(?:a|button)\b[^>]*\bclass\s*=\s*["']([^"']+)["']""", plain)
    facts.snippet_tag = tag.group(1) if tag and "{" not in tag.group(1) else ""
    facts.texts = dict(texts)
    facts.line_counts = {p: len(t.splitlines()) for p, t in texts.items()}
    facts.urls = _urls_in(texts)
    facts.read_paths = list(texts)
    return facts


# ---------------------------------------------------------------- the check

def _normal(text):
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def theme_paths(text):
    """The theme file paths (sections/..., snippets/...) a text names, once each."""
    return list(dict.fromkeys(m.rstrip(".") for m in _THEME_PATH.findall(text or "")))


def unknown_theme_files(answer, names):
    """The theme file paths an answer names that are not in the theme's file list (for the model's free text)."""
    known = set(names)
    return [p for p in theme_paths(answer) if p not in known]


def check_answer(answer, facts, previous=""):
    """Why the model's answer cannot be used, or None. Only the plain facts are checked, never the style."""
    text = answer or ""
    if re.search(r"(?i)shopify", text):
        return "mentions Shopify"
    if previous and _normal(text) == _normal(previous):
        return "repeats the previous answer"
    if _VARIABLE.search(_FENCE.sub("", text)):  # code may sit in a fenced block, never in the steps
        return "shows a code variable"
    mentioned = [p.rstrip(".") for p in _THEME_PATH.findall(text)]
    if facts.names:  # every file name must come from the theme's real file list, found or not
        for path in mentioned:
            if path not in facts.names:
                return f"unknown file {path}"
    if not facts.found:
        return None

    counted = [facts.line_counts[p] for p in mentioned if p in facts.line_counts]
    limit = max(counted) if counted else max(facts.line_counts.values() or [facts.total_lines])
    for match in _LINE_REF.finditer(text):
        for number in filter(None, match.groups()):
            if int(number) < 1 or int(number) > limit:
                return f"line {number} does not exist (the file has {limit} lines)"

    allowed = list(facts.texts.values())
    for link in _LINK.findall(text):
        link = link.rstrip(".,;:)")
        if not any(link in body for body in allowed) and link not in facts.urls:
            return f"link {link} is not in the theme files"
    return None
