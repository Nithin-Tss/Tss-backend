"""The theme-code resolver, the facts, the post-check and the checked answer. No database, no network, no model."""
from unittest import mock

from django.test import SimpleTestCase

from apps.assistant import agent, resolver, tools
from apps.assistant.tests.test_logic import ctx, scripted

QUESTION = 'I want to add a "View all" button on top of all products in the home page'

# templates/index.liquid: 14 lines, the product grid is listed on line 10
INDEX = "\n".join([
    "{% comment %}Home page{% endcomment %}",
    "{% section 'slideshow', type: 'slideshow' %}",
    "{% section 'intro', type: 'rich-text' %}",
    "{% section 'cats', type: 'collection-list' %}",
    "",
    "{% comment %}Shop{% endcomment %}",
    "",
    "{% section 'about-ab12', type: 'rich-text', hidden: true %}",
    "",
    "{% section 'all-products', type: 'product-grid' %}",
    "",
    "{% section 'news', type: 'newsletter' %}",
    "",
    "{% section 'logos', type: 'logo-list' %}",
])

SCHEMA = """{% schema %}
{
  "name": "Product grid",
  "settings": [
    {"type": "text", "id": "heading", "label": "Heading"},
    {"type": "range", "id": "count", "label": "Products to show"}
  ],
  "blocks": []
}
{% endschema %}"""

PRODUCT_GRID = "\n".join([
    '<section class="product-grid-section">',
    '  <div class="container">',
    "    {% if section.settings.heading != blank %}",
    '      <h2 class="section-title">{{ section.settings.heading }}</h2>',
    "    {% endif %}",
    '    <a href="/collections/all" class="btn btn-outline">Shop everything</a>',
    "    {% assign items = collections.all.products %}",
    '    <div class="product-grid grid grid-cols-4">',
    "      {% for product in items limit: section.settings.count %}",
    "        {% render 'product-card', product: product %}",
    "      {% endfor %}",
    "    </div>",
    "  </div>",
    "</section>",
    "",
    SCHEMA,
])

FILES = {
    "templates/index.liquid": INDEX,
    "templates/product.liquid": "{% section 'main', type: 'main-product' %}",
    "sections/product-grid.liquid": PRODUCT_GRID,
    "sections/header.liquid": '<header class="site-header"><a class="logo" href="/">Shop</a></header>',
    "sections/footer.liquid": "<footer>Footer</footer>",
    "sections/newsletter.liquid": "<form class='newsletter'></form>",
    "sections/rich-text.liquid": "<div>text</div>",
    "snippets/product-card.liquid": '<div class="card">{{ product.title }}</div>',
}
NAMES = sorted(FILES)
NOTE = 'Reading the published theme "Main".'


def read_from(files, counter=None):
    def read(path):
        if counter is not None:
            counter.append(path)
        return files.get(path)
    return read


def solve(message, files=FILES, history=()):
    request = resolver.analyse(message, history, sorted(files))
    return request, resolver.resolve(request, sorted(files), read_from(files), NOTE)


class SectionLineTests(SimpleTestCase):
    def test_lines_ids_types_and_hidden(self):
        sections = resolver.parse_sections(INDEX)
        self.assertEqual(len(INDEX.splitlines()), 14)
        by_id = {s["id"]: s for s in sections}
        self.assertEqual(by_id["all-products"], {"line": 10, "id": "all-products", "type": "product-grid", "hidden": False})
        self.assertTrue(by_id["about-ab12"]["hidden"])
        self.assertEqual(by_id["about-ab12"]["type"], "rich-text")  # the file is sections/rich-text.liquid
        self.assertEqual(by_id["about-ab12"]["line"], 8)

    def test_a_legacy_template_has_no_section_lines(self):
        self.assertEqual(resolver.parse_sections("{{ sections }}"), [])
        self.assertTrue(resolver.is_legacy_template("{{ sections }}"))
        self.assertFalse(resolver.is_legacy_template(INDEX))


class AnalyseTests(SimpleTestCase):
    def test_the_view_all_request(self):
        request = resolver.analyse(QUESTION, ())
        self.assertEqual(request.pages, ["index"])
        self.assertIn("product grid", request.targets)
        self.assertIn("button", request.elements)
        self.assertTrue(request.above)
        self.assertEqual(request.label, "View all")
        self.assertTrue(resolver.is_theme_request(request))

    def test_page_words(self):
        for text, page in [("change the home page", "index"), ("hide something on the product page", "product"),
                           ("edit the collection page", "collection"), ("fix the cart page", "cart"),
                           ("change the blog", "blog"), ("edit checkout", "checkout"), ("update the contact page", "page")]:
            self.assertIn(page, resolver.analyse(text, ()).pages, text)

    def test_element_words(self):
        for text, key in [("change the footer text", "footer"), ("hide the search box", "search"),
                          ("change my logo", "logo"), ("edit the menu", "menu"),
                          ("change the announcement bar", "announcement"), ("change the cart icon", "cart icon"),
                          ("change the product card", "product card"), ("edit the hero banner", "banner"),
                          ("change the newsletter", "newsletter"), ("change the colors", "colors"),
                          ("change the fonts", "fonts")]:
            self.assertIn(key, resolver.analyse(text, ()).elements, text)

    def test_a_follow_up_uses_the_earlier_question(self):
        history = [{"role": "user", "content": QUESTION}, {"role": "assistant", "content": "Three files."}]
        request = resolver.analyse("exactly which file?", history)
        self.assertEqual(request.pages, ["index"])
        self.assertIn("product grid", request.targets)
        self.assertEqual(request.label, "View all")
        self.assertTrue(resolver.is_theme_request(request))

    def test_questions_that_are_not_theme_code(self):
        for text in ("How were sales in the last 7 days?", "Show me the latest orders", "which products are low on stock",
                     "show me low stock products"):
            self.assertFalse(resolver.is_theme_request(resolver.analyse(text, ())), text)


class ResolveTests(SimpleTestCase):
    def test_view_all_goes_to_the_product_grid_section_listed_in_the_home_template(self):
        request, facts = solve(QUESTION)
        self.assertTrue(facts.found)
        self.assertEqual(facts.primary, "sections/product-grid.liquid")
        self.assertEqual((facts.template, facts.template_line, facts.hidden), ("templates/index.liquid", 10, False))
        self.assertEqual(facts.total_lines, len(PRODUCT_GRID.splitlines()))
        self.assertEqual(facts.line_counts["templates/index.liquid"], 14)
        self.assertEqual(facts.anchor, 8)  # the product-grid wrapper
        self.assertIn("grid", facts.classes)
        self.assertEqual(facts.snippet_tag, "btn btn-outline")
        self.assertEqual(facts.urls, ["/collections/all"])
        self.assertTrue(facts.excerpt.splitlines()[0].startswith("1| "))

    def test_the_grid_written_directly_in_a_14_line_home_template(self):
        files = dict(FILES)
        files["templates/index.liquid"] = "\n".join(
            ["<main>", "  <h1>Welcome</h1>", "  <p>Intro</p>", "", "  <section>", "    <h2>Shop</h2>", "", "  <div>",
             "    <ul>", '    <div class="product-grid grid">', "      {% for p in collections.all.products %}{% endfor %}",
             "    </div>", "  </section>", "</main>"])
        self.assertEqual(len(files["templates/index.liquid"].splitlines()), 14)
        request, facts = solve(QUESTION, files)
        self.assertEqual(facts.primary, "templates/index.liquid")  # the markup is written in the template itself
        self.assertEqual(facts.total_lines, 14)
        self.assertEqual(facts.anchor, 10)

    def test_a_hidden_section_is_reported(self):
        files = dict(FILES)
        files["templates/index.liquid"] = INDEX.replace("type: 'product-grid' %}", "type: 'product-grid', hidden: true %}")
        _, facts = solve(QUESTION, files)
        self.assertTrue(facts.hidden)

    def test_header_elements_use_the_known_file(self):
        _, facts = solve("change my logo")
        self.assertEqual(facts.primary, "sections/header.liquid")
        _, facts = solve("hide the search box on every page")
        self.assertEqual(facts.primary, "sections/header.liquid")

    def test_product_card_and_footer(self):
        self.assertEqual(solve("change the product card")[1].primary, "snippets/product-card.liquid")
        self.assertEqual(solve("change the footer text")[1].primary, "sections/footer.liquid")

    def test_a_page_without_an_element_resolves_to_its_template(self):
        _, facts = solve("change the product page")
        self.assertEqual(facts.primary, "templates/product.liquid")

    def test_the_fallback_searches_the_files(self):
        files = {"layout/theme.liquid": "<html>", "sections/promo-strip.liquid": "<div class='announcement'>x</div>"}
        _, facts = solve("change the announcement bar", files)
        self.assertEqual(facts.primary, "sections/promo-strip.liquid") if facts.found else self.fail("not found")

    def test_nothing_found_says_so(self):
        _, facts = solve("change the newsletter", {"layout/theme.liquid": "<html></html>"})
        self.assertFalse(facts.found)

    def test_at_most_five_files_are_read(self):
        files = {f"sections/s{i}.liquid": "<div>x</div>" for i in range(30)}
        files["layout/theme.liquid"] = "<html>"
        reads = []
        request = resolver.analyse("change the fonts", (), sorted(files))
        resolver.resolve(request, sorted(files), read_from(files, reads), NOTE)
        self.assertLessEqual(len(set(reads)), resolver.MAX_FILES_READ)

    def test_pasted_code_is_used_instead_of_the_stored_file(self):
        pasted = '<section class="mine">\n  <div class="product-grid">x</div>\n</section>'
        message = f"that is the code in sections/product-grid.liquid:\n{pasted}"
        request = resolver.analyse(message, [{"role": "user", "content": QUESTION}], NAMES)
        self.assertEqual(request.pasted_path, "sections/product-grid.liquid")
        self.assertTrue(resolver.is_theme_request(request))
        facts = resolver.resolve(request, NAMES, read_from(FILES), NOTE)
        self.assertTrue(facts.pasted)
        self.assertEqual(facts.total_lines, 3)
        self.assertIn("mine", facts.excerpt)

    def test_a_fenced_paste_is_read(self):
        request = resolver.analyse("here is sections/footer.liquid ```<footer>New</footer>```", (), NAMES)
        self.assertEqual((request.pasted_path, request.pasted_text), ("sections/footer.liquid", "<footer>New</footer>"))


class CheckAnswerTests(SimpleTestCase):
    def setUp(self):
        _, self.facts = solve(QUESTION)

    def test_the_bad_answer_from_memory_is_rejected(self):
        bad = ('In the Shopify admin open sections/product-grid.liquid, lines 20-30, and add '
               '<a href="/products">View all</a>.')
        self.assertEqual(resolver.check_answer(bad, self.facts), "mentions Shopify")

    def test_each_check_alone(self):
        check = lambda text, previous="": resolver.check_answer(text, self.facts, previous)  # noqa: E731
        self.assertIn("unknown file sections/hero.liquid", check("Edit sections/hero.liquid at line 3."))
        self.assertIn("unknown file assets/x.css", check("Edit assets/x.css."))
        self.assertIn("line 30 does not exist", check("Edit sections/product-grid.liquid, lines 20-30."))
        self.assertIn("line 40 does not exist", check("See templates/index.liquid line 40"))  # index has 14 lines
        self.assertIn("link /products is not in the theme files", check('Use <a href="/products">All</a>'))
        self.assertIn("link https://x.example/a is not in the theme files", check("Go to https://x.example/a"))
        self.assertEqual(check("Same words.", "same   words."), "repeats the previous answer")

    def test_a_good_answer_passes(self):
        good = ("Open Online Store > Edit code, then sections/product-grid.liquid. Add the link above line 8, "
                'for example <a href="/collections/all" class="btn btn-outline">View all</a>. Check the preview '
                "after saving.")
        self.assertIsNone(resolver.check_answer(good, self.facts))
        self.assertIsNone(resolver.check_answer("Use lines 8 to 14 of sections/product-grid.liquid.", self.facts))

    def test_the_line_limit_follows_the_file_named(self):
        self.assertIsNone(resolver.check_answer("sections/product-grid.liquid line 16", self.facts))
        self.assertIsNotNone(resolver.check_answer("templates/index.liquid line 16", self.facts))

    def test_nothing_is_checked_against_a_theme_that_was_not_read(self):
        facts = resolver.Facts()
        self.assertIsNone(resolver.check_answer("Edit sections/hero.liquid line 99 at /anything", facts))
        self.assertEqual(resolver.check_answer("This is Shopify", facts), "mentions Shopify")


NL = chr(10)


def long_grid_file():
    """A section with a big <style> and a <script> that mention the grid, then the markup, then the schema."""
    style = ["<style>"] + [f"  .product-grid .item-{i} {{ color: #333; }}" for i in range(400)] + ["  .grid { display: grid; }", "</style>"]
    script = ["<script>", "  var html = '<div class=\"product-grid grid\">';", "  for (var p in products) { html += p; }", "</script>"]
    markup = [
        '<section class="product-grid-section">',
        '  <h2 class="section-title">{{ section.settings.heading }}</h2>',
        '  <a href="/collections/all" class="btn btn-outline">Shop everything</a>',
        '  <div class="product-grid grid grid-cols-4">',
        "    {% for product in collections.all.products %}",
        "      {% render 'product-card', product: product %}",
        "    {% endfor %}",
        "  </div>",
        "</section>",
    ]
    return style + script + markup + [""] + SCHEMA.splitlines(), len(style) + len(script) + 4


class MarkupAnchorTests(SimpleTestCase):
    def test_style_script_and_schema_are_skipped(self):
        lines = [
            "<style>", ".product-grid { }", "</style>", '<div class="product-grid">', "<script>x = 'product-grid'</script>",
            "<style>.a{}</style><p>hi</p>", "{% schema %}", '{"name": "product-grid"}', "{% endschema %}",
        ]
        seen = dict(resolver._markup_lines(lines))
        self.assertEqual(sorted(seen), [4, 6])
        self.assertEqual(seen[6], "<p>hi</p>")
        self.assertEqual(resolver._anchor_line(lines, resolver.ELEMENTS["product grid"]), 4)

    def test_no_markup_match_gives_no_line(self):
        lines = ["<style>", ".product-grid { }", "</style>", "<p>nothing</p>"]
        self.assertEqual(resolver._anchor_line(lines, resolver.ELEMENTS["product grid"]), 0)

    def test_colors_may_still_be_found_in_style(self):
        lines = ["<style>", ":root { --color-primary: red; }", "</style>"]
        self.assertEqual(resolver._anchor_line(lines, resolver.ELEMENTS["colors"]), 2)

    def test_view_all_on_a_long_file_points_at_the_grid_wrapper_in_the_markup(self):
        lines, wrapper = long_grid_file()
        files = dict(FILES)
        files["sections/product-grid.liquid"] = NL.join(lines)
        _, facts = solve(QUESTION, files)
        self.assertGreater(facts.total_lines, 400)
        self.assertEqual(facts.anchor, wrapper)  # the opening <div class="product-grid ..."> of the loop
        self.assertIn("product-grid grid grid-cols-4", lines[wrapper - 1])
        self.assertIn(f"{wrapper}| ", facts.excerpt)
        self.assertEqual(facts.snippet_tag, "btn btn-outline")


def fake_theme(files=FILES):
    return lambda c: tools.OpenTheme(note=NOTE, theme_id="t-1", names=sorted(files), read=read_from(files))


THEME_CTX = {"themes.view"}
HISTORY = [{"role": "user", "content": QUESTION}, {"role": "assistant", "content": "Three possible files."}]


