"""
The admin guide: knowledge/admin_guide.md, searched with plain keyword scoring. No database, no network.
"""
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

GUIDE_FILE = Path(__file__).parent / "knowledge" / "admin_guide.md"
MAX_ALTERNATIVES = 2
CLOSE = 0.6  # an entry scoring at least this share of the best one is offered as an alternative

STOP_WORDS = frozenset(
    "a an the to of in on for and or i my me do how can where what is are it this that change edit "
    "update set make want need would like please store shop admin find".split()
)
# Words an owner may use for what the guide calls something else
SYNONYMS = {
    "banner": "announcement", "notice": "announcement", "brand": "logo", "navigation": "menu", "nav": "menu",
    "coupon": "discount", "promo": "discount", "voucher": "discount", "sku": "product",
    "bottle": "product", "css": "code", "html": "code", "liquid": "code", "javascript": "code",
}


@dataclass(frozen=True)
class Entry:
    id: str
    task: str
    keywords: tuple
    open: str
    steps: tuple
    note: str


def tokens(text):
    """Lower-case words without stop words, plural 's' and with synonyms folded in."""
    words = []
    for word in re.findall(r"[a-z0-9]+", (text or "").lower()):
        if word in STOP_WORDS:
            continue
        if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
            word = word[:-1]
        words.append(SYNONYMS.get(word, word))
    return words


def parse(text):
    """Split the markdown into entries. A malformed entry raises, so a bad edit is caught by the tests."""
    entries = []
    for block in re.split(r"(?m)^## ", text)[1:]:
        entry_id, _, body = block.partition("\n")
        fields, steps, in_steps = {}, [], False
        for line in body.splitlines():
            line = line.rstrip()
            if line.startswith("steps:"):
                in_steps = True
            elif in_steps and re.match(r"\d+\.\s", line):
                steps.append(re.sub(r"^\d+\.\s*", "", line))
            elif re.match(r"(task|keywords|open|note):", line):
                in_steps = False
                key, _, value = line.partition(":")
                fields[key] = value.strip()
        for needed in ("task", "keywords", "open"):
            if not fields.get(needed):
                raise ValueError(f"Guide entry {entry_id.strip()!r} has no {needed}.")
        if not steps:
            raise ValueError(f"Guide entry {entry_id.strip()!r} has no steps.")
        entries.append(Entry(
            id=entry_id.strip(), task=fields["task"], keywords=tuple(k.strip() for k in fields["keywords"].split(",")),
            open=fields["open"], steps=tuple(steps), note=fields.get("note", ""),
        ))
    return entries


@lru_cache(maxsize=1)
def load():
    return tuple(parse(GUIDE_FILE.read_text(encoding="utf-8")))


def score(entry, query_tokens):
    """Whole keyword phrases count most, then single words of keywords, then words of the task title."""
    if not query_tokens:
        return 0.0
    query = set(query_tokens)
    joined = " ".join(query_tokens)
    total = 0.0
    for keyword in entry.keywords:
        words = tokens(keyword)
        if not words:
            continue
        if len(words) > 1 and " ".join(words) in joined:
            total += 3.0 * len(words)
        total += 1.0 * len(query & set(words))
    total += 1.5 * len(query & set(tokens(entry.task)))
    return total


def search(topic, entries=None):
    """The best entry and up to two close alternatives; ([], ...) when nothing matches."""
    entries = load() if entries is None else entries
    query = tokens(topic)
    ranked = sorted(((score(e, query), e) for e in entries), key=lambda pair: -pair[0])
    ranked = [(s, e) for s, e in ranked if s > 0]
    if not ranked:
        return None, []
    best_score, best = ranked[0]
    close = [e for s, e in ranked[1:] if s >= best_score * CLOSE][:MAX_ALTERNATIVES]
    return best, close
