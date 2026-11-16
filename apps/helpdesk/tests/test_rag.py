"""Help assistant RAG: chunking, indexing, hybrid retrieval, rerank. The model service is always mocked."""
from types import SimpleNamespace
from unittest import mock

from django.test import TestCase, override_settings

from apps.ai_assistant.llm import LLMError
from apps.helpdesk import chunking, help_tools, indexing, rag
from apps.helpdesk.models import HelpArticle, HelpChunk

BODY = """Intro line about domains.

## Connect a domain

Add your domain in settings. Then create the DNS records it shows you.

## Remove a domain

Open the domain and press remove."""


def fake_embed(texts, model=None):
    """A 3-number 'embedding': the topic of the text, enough to tell domains from shipping."""
    return [[1.0 if "domain" in t.lower() else 0.0, 1.0 if "ship" in t.lower() else 0.0, 0.1] for t in texts]


def make(slug, title, body, published=True):
    return HelpArticle.objects.create(slug=slug, title=title, body=body, is_published=published)


class ChunkingTests(TestCase):
    def test_sections_become_chunks_with_their_heading(self):
        chunks = chunking.chunk_article(BODY)
        self.assertEqual([c.heading for c in chunks], ["", "Connect a domain", "Remove a domain"])
        self.assertEqual([c.position for c in chunks], [0, 1, 2])
        self.assertIn("DNS records", chunks[1].text)

    def test_long_text_is_split_under_the_limit(self):
        chunks = chunking.chunk_article("## Big\n\n" + " ".join(["Sentence number one is here."] * 200))
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c.text) <= chunking.MAX_CHARS + chunking.MIN_CHARS for c in chunks))

    def test_empty_body_has_no_chunks(self):
        self.assertEqual(chunking.chunk_article(""), [])


class HelpersTests(TestCase):
    def test_cosine(self):
        self.assertAlmostEqual(rag.cosine([1, 0], [1, 0]), 1.0)
        self.assertAlmostEqual(rag.cosine([1, 0], [0, 1]), 0.0)
        self.assertEqual(rag.cosine([0, 0], [1, 0]), 0.0)

    def test_fusion_rewards_agreement(self):
        scores = rag.fuse(["a", "b"], ["b", "c"])
        self.assertEqual(max(scores, key=scores.get), "b")

    def test_parse_order_ignores_junk(self):
        self.assertEqual(rag.parse_order("Sure: [2, 0, 9, 2, \"x\"]", {0, 1, 2}), [2, 0])
        self.assertEqual(rag.parse_order("[]", {0}), [])
        self.assertIsNone(rag.parse_order("no list here", {0}))


@override_settings(HELP_RAG_ENABLED=True)
class IndexingTests(TestCase):
    def test_saving_a_published_article_builds_embedded_chunks(self):
        with mock.patch("apps.ai_assistant.llm.embed", side_effect=fake_embed), self.captureOnCommitCallbacks(execute=True):
            article = make("domains", "Domains", BODY)
        self.assertEqual(article.chunks.count(), 3)
        self.assertTrue(all(c.embedding for c in article.chunks.all()))

    def test_unchanged_chunks_are_not_embedded_again(self):
        with mock.patch("apps.ai_assistant.llm.embed", side_effect=fake_embed):
            article = make("domains", "Domains", BODY)
            indexing.index_article(article)
        with mock.patch("apps.ai_assistant.llm.embed", side_effect=fake_embed) as embed:
            indexing.index_article(article)
        embed.assert_not_called()

    def test_embedding_failure_keeps_the_text_searchable(self):
        with mock.patch("apps.ai_assistant.llm.embed", side_effect=LLMError("down")):
            article = make("domains", "Domains", BODY)
            indexing.index_article(article)
        self.assertEqual(article.chunks.count(), 3)
        self.assertFalse(any(c.embedding for c in article.chunks.all()))

    def test_unpublishing_removes_the_chunks(self):
        with mock.patch("apps.ai_assistant.llm.embed", side_effect=fake_embed):
            article = make("domains", "Domains", BODY)
            indexing.index_article(article)
            article.is_published = False
            article.save()
            indexing.index_article(article)
        self.assertEqual(HelpChunk.objects.count(), 0)


@override_settings(HELP_RAG_ENABLED=True, HELP_RAG_RERANK=False)
class RagSearchTests(TestCase):
    def setUp(self):
        with mock.patch("apps.ai_assistant.llm.embed", side_effect=fake_embed):
            self.domains = make("domains", "Connect a custom domain", BODY)
            self.shipping = make("shipping", "Shipping rates", "## Rates\n\nCreate a shipping zone and a flat rate.")
            self.draft = make("draft", "Draft domain notes", "domain notes", published=False)
            for article in HelpArticle.objects.all():
                indexing.index_article(article)

    def search(self, query, **kwargs):
        with mock.patch("apps.ai_assistant.llm.embed", side_effect=fake_embed):
            return rag.RagSearch().search_passages(query, **kwargs)

    def test_finds_the_right_article_and_its_best_passage(self):
        passages = self.search("how do I point my domain at the store")
        self.assertEqual(passages[0].article.slug, "domains")
        self.assertNotIn("draft", [p.article.slug for p in passages])

    def test_semantic_match_without_shared_words(self):
        # the question shares no word with the shipping article; only the vectors connect them
        with mock.patch("apps.ai_assistant.llm.embed", side_effect=lambda t, model=None: [[0.0, 1.0, 0.1] for _ in t]):
            passages = rag.RagSearch().search_passages("parcel costs")
        self.assertEqual(passages[0].article.slug, "shipping")

    def test_falls_back_to_text_search_when_embedding_fails(self):
        with mock.patch("apps.ai_assistant.llm.embed", side_effect=LLMError("down")):
            passages = rag.RagSearch().search_passages("domain")
        self.assertEqual(passages[0].article.slug, "domains")

    @override_settings(HELP_RAG_RERANK=True)
    def test_rerank_orders_and_drops_passages(self):
        reply = SimpleNamespace(content="[1]")
        with mock.patch("apps.ai_assistant.llm.chat", return_value=reply):
            passages = self.search("domain")
        self.assertEqual(len(passages), 1)

    @override_settings(HELP_RAG_RERANK=True)
    def test_rerank_failure_keeps_the_fused_order(self):
        with mock.patch("apps.ai_assistant.llm.chat", side_effect=LLMError("down")):
            passages = self.search("domain")
        self.assertEqual(passages[0].article.slug, "domains")

    def test_tool_returns_passages_and_sources(self):
        ctx = help_tools.HelpContext(store=None, user=None, conversation=None)
        with mock.patch("apps.ai_assistant.llm.embed", side_effect=fake_embed):
            result = help_tools.search_help_articles(ctx, {"query": "domain"})
        first = result["articles"][0]
        self.assertEqual(first["title"], "Connect a custom domain")
        self.assertTrue(first["passages"][0]["text"])
        self.assertEqual(ctx.sources[0]["slug"], "domains")
