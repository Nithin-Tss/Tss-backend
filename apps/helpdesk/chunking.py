"""
Split a help article (markdown) into passages for retrieval. Each passage stays inside one heading section, so it
makes sense on its own, and carries that heading. Pure functions: no database, no network.
"""
import re
from dataclasses import dataclass

MAX_CHARS = 900     # a passage is packed with paragraphs up to about this size
MIN_CHARS = 60      # a smaller tail is merged into the passage before it
HEADING = re.compile(r"^(#{1,4})\s+(.*\S)\s*$")


@dataclass(frozen=True)
class Chunk:
    position: int
    heading: str
    text: str


def _sections(body):
    """[(heading, [paragraph, ...])] in order. Text before the first heading has the heading ''."""
    sections, heading, paragraphs, current = [], "", [], []

    def close_paragraph():
        if current:
            paragraphs.append("\n".join(current).strip())
            current.clear()

    for line in (body or "").replace("\r\n", "\n").split("\n"):
        match = HEADING.match(line)
        if match:
            close_paragraph()
            if paragraphs:
                sections.append((heading, paragraphs[:]))
            heading, paragraphs = match.group(2).strip(), []
        elif not line.strip():
            close_paragraph()
        else:
            current.append(line.rstrip())
    close_paragraph()
    if paragraphs:
        sections.append((heading, paragraphs))
    return sections


def _split_long(paragraph):
    """A paragraph over MAX_CHARS is cut at sentence ends (or, failing that, at spaces)."""
    if len(paragraph) <= MAX_CHARS:
        return [paragraph]
    pieces, current = [], ""
    for sentence in re.split(r"(?<=[.!?])\s+", paragraph):
        while len(sentence) > MAX_CHARS:
            cut = sentence.rfind(" ", 0, MAX_CHARS) or MAX_CHARS
            if cut <= 0:
                cut = MAX_CHARS
            if current:
                pieces.append(current)
                current = ""
            pieces.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        if current and len(current) + len(sentence) + 1 > MAX_CHARS:
            pieces.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        pieces.append(current)
    return pieces


def chunk_article(body):
    """The article body as a list of Chunk, positions 0, 1, 2..."""
    chunks = []
    for heading, paragraphs in _sections(body):
        texts, current = [], ""
        for paragraph in (piece for p in paragraphs for piece in _split_long(p)):
            if current and len(current) + len(paragraph) + 2 > MAX_CHARS:
                texts.append(current)
                current = paragraph
            else:
                current = f"{current}\n\n{paragraph}".strip()
        if current:
            texts.append(current)
        if len(texts) > 1 and len(texts[-1]) < MIN_CHARS:
            texts[-2] = f"{texts[-2]}\n\n{texts.pop()}"
        chunks.extend((heading, text) for text in texts)
    return [Chunk(position=i, heading=heading, text=text) for i, (heading, text) in enumerate(chunks)]


def embedding_input(title, chunk):
    """What is embedded: the article title and the heading give a short passage its context."""
    parts = [title, chunk.heading] if chunk.heading and chunk.heading != title else [title]
    return "\n".join(parts + [chunk.text])
