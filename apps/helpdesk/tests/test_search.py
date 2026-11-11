"""
Help article search. The PostgreSQL tests need a real PostgreSQL database (full-text search, GIN index) and run with:

    python manage.py test apps.helpdesk.tests.test_search --settings=config.test_settings_pg --keepdb

Under the SQLite settings the KeywordSearch fallback tests run and the PostgreSQL ones are skipped by design:
SQLite has no full-text search. CI runs them in the PostgreSQL job.
"""
import unittest

from django.db import connection
from django.test import TestCase

from apps.helpdesk import search
from apps.helpdesk.models import HelpArticle

ON_POSTGRES = connection.vendor == "postgresql"


def article(slug, title, body, keywords="", category="other", published=True):
    return HelpArticle.objects.create(
        slug=slug, title=title, body=body, keywords=keywords, category=category, is_published=published,
    )


class Content:
    def make(self):
        article("domains", "Connect a custom domain", "Add your domain, then create the DNS records it shows.",
                "domain, dns, custom domain", "domains")
        article("shipping-rates", "Set up shipping rates", "Create a shipping zone and add a flat rate per zone.",
                "shipping, delivery, rates", "shipping")
        article("discount", "Create a discount code", "Choose a percentage off and share the code.",
                "coupon, promo", "discounts")
        article("draft", "Unpublished domain notes", "Domain notes not ready yet.", "domain", "domains", published=False)


@unittest.skipUnless(ON_POSTGRES, "PostgreSQL full-text search: run with config.test_settings_pg")
class PostgresSearchTests(Content, TestCase):
    def setUp(self):
        self.make()
        self.backend = search.PostgresSearch()

    def test_a_natural_question_finds_the_right_article_first(self):
        hits = self.backend.search("How do I connect my domain?")
        self.assertEqual(hits[0].slug, "domains")

    def test_stemming_matches_other_word_forms(self):
        self.assertEqual(self.backend.search("discounts")[0].slug, "discount")
        self.assertEqual(self.backend.search("my deliveries are late")[0].slug, "shipping-rates")

    def test_keywords_are_searched(self):
        self.assertEqual(self.backend.search("coupon")[0].slug, "discount")

    def test_unpublished_articles_never_appear(self):
        self.assertNotIn("draft", [a.slug for a in self.backend.search("domain notes")])

    def test_nothing_relevant_returns_nothing(self):
        self.assertEqual(self.backend.search("quantum spaceship"), [])
        self.assertEqual(self.backend.search(""), [])
        self.assertEqual(self.backend.search("!!! ???"), [])

    def test_hostile_query_text_is_harmless(self):
        self.assertEqual(self.backend.search("'; DROP TABLE x; -- & | ! ( )"), [])

    def test_limit(self):
        self.assertEqual(len(self.backend.search("shipping domain discount", limit=2)), 2)

    def test_the_gin_index_exists(self):
        with connection.cursor() as cursor:
            cursor.execute("select indexdef from pg_indexes where indexname = 'gin_help_article_fts'")
            [(definition,)] = cursor.fetchall()
        self.assertIn("gin", definition.lower())
        self.assertIn("to_tsvector", definition)

    def test_get_backend_is_postgres_here(self):
        self.assertIsInstance(search.get_backend(), search.PostgresSearch)


class KeywordSearchTests(Content, TestCase):
    def setUp(self):
        self.make()

    def test_fallback_backend_finds_and_hides_unpublished(self):
        hits = search.KeywordSearch().search("connect my domain")
        self.assertEqual([a.slug for a in hits], ["domains"])
        self.assertEqual(search.KeywordSearch().search("quantum"), [])

    def test_backend_can_be_swapped_by_setting(self):
        from django.test import override_settings

        with override_settings(HELP_SEARCH_BACKEND="apps.helpdesk.search.KeywordSearch"):
            self.assertIsInstance(search.get_backend(), search.KeywordSearch)


class SeedCommandTests(TestCase):
    def run_seed(self):
        from django.core.management import call_command

        call_command("seed_help_articles", stdout=__import__("io").StringIO())

    def test_seeds_published_guide_articles_and_unpublished_todo_placeholders(self):
        self.run_seed()
        published = HelpArticle.objects.filter(is_published=True)
        self.assertGreaterEqual(published.count(), 10)
        self.assertTrue(published.filter(slug="create-discount", category="discounts").exists())
        placeholders = HelpArticle.objects.filter(is_published=False)
        self.assertEqual({a.category for a in placeholders}, {"store_settings", "payments", "shipping", "domains"})
        for placeholder in placeholders:
            self.assertIn("TODO", placeholder.body)  # clearly marked, and never shown to the assistant
        hits = search.KeywordSearch().search("TODO payments shipping domains store settings", limit=50)
        self.assertTrue(all(a.is_published for a in hits))  # the assistant can never answer from placeholder text

    def test_running_it_again_keeps_admin_edits_and_adds_nothing(self):
        self.run_seed()
        count = HelpArticle.objects.count()
        HelpArticle.objects.filter(slug="create-discount").update(body="Edited in the admin")
        self.run_seed()
        self.assertEqual(HelpArticle.objects.count(), count)
        self.assertEqual(HelpArticle.objects.get(slug="create-discount").body, "Edited in the admin")
