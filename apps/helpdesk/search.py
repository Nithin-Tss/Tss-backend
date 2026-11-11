"""
Help article search, behind a small interface so the engine can change (for example to pgvector embeddings)
without touching the assistant's tools:

    articles = get_backend().search("how do I connect my domain", limit=5)   # -> [HelpArticle], best first

The default is PostgreSQL full-text search (SearchVector / SearchRank, served by the GIN index on HelpArticle).
Swap the engine with settings.HELP_SEARCH_BACKEND = "dotted.path.ToYourBackend". KeywordSearch is the fallback for
databases without full-text search (the SQLite test database).
"""
import re
from functools import reduce
from operator import or_

from django.conf import settings
from django.contrib.postgres.search import SearchQuery, SearchRank, SearchVector
from django.db import connection
from django.db.models import Q
from django.utils.module_loading import import_string

from .models import HelpArticle

DEFAULT_LIMIT = 5
MAX_QUERY_CHARS = 300
MAX_TERMS = 12
CONFIG = "english"
MIN_RANK = 0.001  # below this a "match" is noise


def terms(query):
    """The distinct words of the question (lower case, at least 2 letters), in order, capped."""
    seen = []
    for word in re.findall(r"[A-Za-z0-9]+", (query or "")[:MAX_QUERY_CHARS].lower()):
        if len(word) >= 2 and word not in seen:
            seen.append(word)
    return seen[:MAX_TERMS]


class SearchBackend:
    """What the rest of the code relies on. Only published articles may ever be returned."""

    def search(self, query, limit=DEFAULT_LIMIT):
        raise NotImplementedError


class PostgresSearch(SearchBackend):
    """
    Full-text search. Any word of the question may match (OR), so a natural question finds the article; ranking puts
    the articles matching the most words, and title / keyword matches, first.
    """

    # Must stay identical to the vector of the GIN index (HelpArticle.Meta.indexes), or the index is not used
    index_vector = SearchVector("title", "keywords", "body", config=CONFIG)
    rank_vector = (
        SearchVector("title", weight="A", config=CONFIG)
        + SearchVector("keywords", weight="B", config=CONFIG)
        + SearchVector("body", weight="C", config=CONFIG)
    )

    def search(self, query, limit=DEFAULT_LIMIT):
        words = terms(query)
        if not words:
            return []
        tsquery = reduce(or_, (SearchQuery(w, config=CONFIG, search_type="plain") for w in words))
        return list(
            HelpArticle.objects.filter(is_published=True)
            .annotate(match=self.index_vector)
            .filter(match=tsquery)
            .annotate(rank=SearchRank(self.rank_vector, tsquery))
            .filter(rank__gte=MIN_RANK)
            .order_by("-rank", "title")[:limit]
        )


class KeywordSearch(SearchBackend):
    """Substring matching scored by how many words hit the title (3), keywords (2) and body (1)."""

    def search(self, query, limit=DEFAULT_LIMIT):
        words = terms(query)
        if not words:
            return []
        condition = reduce(
            or_, (Q(title__icontains=w) | Q(keywords__icontains=w) | Q(body__icontains=w) for w in words),
        )
        scored = []
        for article in HelpArticle.objects.filter(is_published=True).filter(condition):
            title, keywords, body = article.title.lower(), article.keywords.lower(), article.body.lower()
            score = sum(3 * (w in title) + 2 * (w in keywords) + (w in body) for w in words)
            scored.append((-score, article.title, article))
        return [a for _, _, a in sorted(scored, key=lambda t: t[:2])[:limit]]


def get_backend():
    path = getattr(settings, "HELP_SEARCH_BACKEND", "")
    if path:
        return import_string(path)()
    return PostgresSearch() if connection.vendor == "postgresql" else KeywordSearch()


def snippet(article, length=300):
    """The start of the article without markdown noise, for results shown to the model."""
    text = re.sub(r"[#*_`>]", "", article.body)
    return " ".join(text.split())[:length]
