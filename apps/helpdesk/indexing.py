"""
Keep HelpChunk in step with HelpArticle: split the published articles, embed the passages, store them.

Embedding needs the model service. If it is down or not set up, the passages are still saved without a vector, so
full-text search keeps working, and `reindex_help_articles` fills the vectors in later.
"""
import hashlib
import logging

from django.conf import settings
from django.db import transaction

from apps.ai_assistant import llm

from . import chunking
from .models import HelpArticle, HelpChunk

logger = logging.getLogger(__name__)
BATCH = 64


def enabled():
    return getattr(settings, "HELP_RAG_ENABLED", True)


def _hash(title, chunk):
    return hashlib.sha256(chunking.embedding_input(title, chunk).encode()).hexdigest()


def _embed_all(texts):
    """Vectors for the texts, or None for each when the service fails."""
    vectors = []
    for start in range(0, len(texts), BATCH):
        try:
            vectors.extend(llm.embed(texts[start:start + BATCH]))
        except llm.LLMError as error:
            logger.warning("help rag: embedding failed (%s); passages saved without vectors", error)
            vectors.extend([None] * len(texts[start:start + BATCH]))
    return vectors


@transaction.atomic
def index_article(article):
    """Rebuild the article's passages. Unchanged passages keep their vector. Returns the number of passages."""
    if not article.is_published:
        article.chunks.all().delete()
        return 0
    model = llm.embedding_model_name()
    chunks = chunking.chunk_article(article.body)
    hashes = [_hash(article.title, c) for c in chunks]
    known = {
        (row.content_hash): row.embedding
        for row in article.chunks.filter(embedding_model=model).exclude(embedding=[])
    }
    missing = [i for i, h in enumerate(hashes) if h not in known]
    fresh = dict(zip(missing, _embed_all([chunking.embedding_input(article.title, chunks[i]) for i in missing])))

    article.chunks.all().delete()
    rows = []
    for i, (chunk, content_hash) in enumerate(zip(chunks, hashes)):
        vector = known.get(content_hash) or fresh.get(i)
        rows.append(HelpChunk(
            article=article, position=chunk.position, heading=chunk.heading, text=chunk.text,
            content_hash=content_hash, embedding=vector or [], embedding_model=model if vector else "",
        ))
    HelpChunk.objects.bulk_create(rows)
    return len(rows)


def reindex_all(only_missing=False):
    """Every published article (and drop the passages of unpublished ones). Returns (articles, passages)."""
    HelpChunk.objects.filter(article__is_published=False).delete()
    articles = HelpArticle.objects.filter(is_published=True)
    if only_missing:
        articles = articles.filter(chunks__isnull=True) | articles.filter(chunks__embedding=[])
    count = chunks = 0
    for article in articles.distinct():
        chunks += index_article(article)
        count += 1
    return count, chunks
