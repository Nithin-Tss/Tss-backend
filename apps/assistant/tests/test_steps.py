"""Guided theme answers: all the steps at once, the anchor, the review of a pasted edit. No database, no network, no model."""
import re

from django.test import SimpleTestCase

from apps.assistant import agent, resolver, steps
from apps.assistant.tests.test_logic import ctx, scripted
from apps.assistant.tests.test_resolver import (
    FILES, INDEX, NL, PRODUCT_GRID, QUESTION, SCHEMA, THEME_CTX, fake_theme, long_grid_file, solve,
)

# The same section, but its customizer has a View all label and a View all link
CUSTOM_GRID = PRODUCT_GRID.replace(
    '{"type": "text", "id": "heading", "label": "Heading"},',
    '{"type": "text", "id": "heading", "label": "Heading"},\n    {"type": "text", "id": "view_all_label", '
    '"label": "View all label"},\n    {"type": "url", "id": "view_all_link", "label": "View all link"},',
)
CUSTOM_FILES = {**FILES, "sections/product-grid.liquid": CUSTOM_GRID}

# The only mention of the grid is in CSS: no line in the markup can be pointed at
STYLE_ONLY = {
    **FILES,
    "sections/product-grid.liquid": NL.join(["<style>", ".product-grid { }", "</style>", "<p>x</p>"]),
}
PASTE = NL.join([
    '<section class="grid-wrap">',
    '  <a href="/collections/all" class="btn btn-outline">Shop everything</a>',
    '  <div class="product-grid">',
    "    {% for product in collections.all.products %}{% endfor %}",
    "  </div>",
    "</section>",
])

# A section shaped like the real one: the carousel's arrows sit inside an {% if %}, before the product loop
CAROUSEL = NL.join([
    '<section class="products">',
    '  <h2 class="section-title">{{ section.settings.heading }}</h2>',
    '  <a href="/collections/all" class="btn btn-outline">Shop everything</a>',
    '  <div class="carousel-wrap">',
    "    {% if s.layout == 'carousel' and s.show_arrows %}",
    '      <div class="carousel__controls">',
    '        <button class="carousel__prev">Prev</button>',
    "      </div>",
    "    {% endif %}",
    "    {% for product in collections.all.products %}",
    "      {% render 'product-card', product: product %}",
    "    {% endfor %}",
    "  </div>",
    "</section>",
    "",
    SCHEMA,
])
CAROUSEL_FILES = {**FILES, "sections/product-grid.liquid": CAROUSEL}
CAROUSEL_WRAPPER = '<div class="carousel-wrap">'

# The products themselves are in a carousel branch of an {% if %} (and a grid branch of its else)
BRANCHES = NL.join([
    '<section class="products">',
    '  <h2 class="section-title">{{ section.settings.heading }}</h2>',
    '  <a href="/collections/all" class="btn btn-outline">Shop everything</a>',
    "  {% if s.layout == 'carousel' %}",
    '    <div class="carousel">',
    "      {% for product in collections.all.products %}{% render 'product-card', product: product %}{% endfor %}",
    "      {% if s.show_arrows %}",
    '        <div class="carousel__controls"><button class="carousel__next">Next</button></div>',
    "      {% endif %}",
    "    </div>",
    "  {% else %}",
    '    <div class="product-grid grid">',
    "      {% for product in collections.all.products %}{% render 'product-card', product: product %}{% endfor %}",
    "    </div>",
    "  {% endif %}",
    "</section>",
    "",
    SCHEMA,
])
BRANCH_FILES = {**FILES, "sections/product-grid.liquid": BRANCHES}

FORBIDDEN_FOR_OWNERS = ("view_all", "section.settings", "settings.", "{{", "{%", ".liquid", "```")


def no_model(*args, **kwargs):
    raise AssertionError("the model must not be asked for a theme request")


def talk(messages, files=FILES, permissions=THEME_CTX):
    """Play a chat through agent.run: [(answer, context)], each turn getting the earlier turns as history."""
    history, turns = [], []
    for message in messages:
        context = ctx(permissions)
        answer, used = agent.run(message, context, chat=no_model, history=history, open_theme=fake_theme(files))
        assert used == []
        history += [{"role": "user", "content": message}, {"role": "assistant", "content": answer}]
        turns.append((answer, context))
    return turns


def words(text):
    return len(text.split())


def numbered(text):
    """The numbered lines of a list answer (code blocks and blank lines left out)."""
    return [line for line in text.splitlines() if re.match(r"\d+\. ", line)]


class CustomizerPathTests(SimpleTestCase):
    def test_view_all_is_one_short_list_with_labels_and_clicks(self):
        (answer, context), = talk([QUESTION], CUSTOM_FILES)
        lines = numbered(answer)
        self.assertEqual(len(lines), 4)
        self.assertEqual(answer.splitlines(), lines)  # nothing but the list: no code block
        self.assertIn("Online Store > Edit theme", lines[0])
        self.assertIn("Home page", lines[1])
        self.assertIn('"Product grid" section', lines[1])
        self.assertIn('"View all label" and "View all link"', lines[2])
        self.assertTrue(lines[3].endswith("Check the preview after saving."))
        for text in FORBIDDEN_FOR_OWNERS:
            self.assertNotIn(text, answer)
        self.assertNotIn("Step ", answer)
        self.assertNotIn("Say next", answer)
        self.assertIsNone(re.search(r"\bline \d", answer))
        self.assertNotEqual(answer, agent.SAFE_STEP)
        self.assertEqual(context.opened_files, [])  # no Code editor button for a no-code answer
        self.assertEqual(context.open_keys, ["customize"])

    def test_a_toggle_and_a_block_get_their_own_lines(self):
        files = dict(FILES)
        files["sections/product-grid.liquid"] = PRODUCT_GRID.replace(
            '"blocks": []',
            '"blocks": [{"type": "view_all", "name": "View all button", "settings": []}]',
        ).replace(
            '{"type": "text", "id": "heading", "label": "Heading"},',
            '{"type": "text", "id": "heading", "label": "Heading"},\n    {"type": "checkbox", "id": "show_view_all", '
            '"label": "Show View all"},',
        )
        (answer, _), = talk([QUESTION], files)
        self.assertIn('Find "Show View all" and turn it on.', answer)
        self.assertIn('Click "Add block". Pick "View all button".', answer)
        self.assertNotIn("show_view_all", answer)
        self.assertLessEqual(len(numbered(answer)), 5)

    def test_a_hidden_section_gets_a_show_line(self):
        files = dict(CUSTOM_FILES)
        files["templates/index.liquid"] = INDEX.replace("type: 'product-grid' %}", "type: 'product-grid', hidden: true %}")
        (answer, _), = talk([QUESTION], files)
        self.assertEqual(len(numbered(answer)), 5)
        self.assertIn("eye icon", numbered(answer)[2])

    def test_a_long_list_is_cut_to_five_lines(self):
        steps_ = [(f"a{i}", f"b{i}", f"c{i}") for i in range(8)]
        self.assertEqual(len(steps._capped(steps_)), 5)


class CodePathTests(SimpleTestCase):
    def test_all_steps_at_once_and_the_code_is_in_the_last_ones(self):
        (answer, context), = talk([QUESTION])
        lines = numbered(answer)
        self.assertEqual(len(lines), 4)
        self.assertIn("Online Store > Edit code", lines[0])
        self.assertIn("`sections/product-grid.liquid`", lines[0])
        self.assertTrue(lines[1].startswith("2. Go to line 8."))
        self.assertIn('`<div class="product-grid grid grid-cols-4">`', lines[1])
        self.assertIn(NL.join(["```liquid", '<a href="PICK-YOUR-LINK" class="btn btn-outline">View all</a>', "```"]), answer)
        self.assertEqual(answer.count("```"), 2)  # only the final snippet is a code block
        self.assertLess(answer.index("```"), answer.index("`/collections/all`"))
        self.assertTrue(answer.endswith("Check the preview after saving."))
        self.assertNotIn("Step ", answer)
        self.assertNotIn("Say next", answer)
        self.assertNotIn("view_all", answer)
        self.assertNotEqual(answer, agent.SAFE_STEP)
        self.assertEqual(context.opened_files, ["sections/product-grid.liquid"])

    def test_the_long_file_with_a_big_style_points_at_the_markup(self):
        lines, wrapper = long_grid_file()
        files = {**FILES, "sections/product-grid.liquid": NL.join(lines)}
        (answer, _), = talk([QUESTION], files)
        self.assertIn(f"Go to line {wrapper}.", answer)
        self.assertIn('`<div class="product-grid grid grid-cols-4">`', answer)

    def test_show_code_gives_the_full_list_with_the_code_again(self):
        answers = [a for a, _ in talk([QUESTION, "i didn't understand", "show me the code"])]
        self.assertNotIn("```", answers[1])
        self.assertIn("```liquid", answers[2])
        self.assertEqual(answers[2], answers[0])

    def test_show_code_when_the_customizer_does_it_says_no_code_is_needed(self):
        answers = [a for a, _ in talk([QUESTION, "show me the code"], CUSTOM_FILES)]
        self.assertIn("You do not need code for this.", answers[1])
        self.assertNotIn("```", answers[1])

    def test_a_hidden_section_gets_a_show_line_first(self):
        files = dict(FILES)
        files["templates/index.liquid"] = INDEX.replace("type: 'product-grid' %}", "type: 'product-grid', hidden: true %}")
        (answer, _), = talk([QUESTION], files)
        self.assertEqual(len(numbered(answer)), 5)
        self.assertIn("eye icon", numbered(answer)[0])

    def test_done_is_answered_in_one_line(self):
        answers = [a for a, _ in talk([QUESTION, "done"])]
        self.assertEqual(answers[1], steps.DONE)


class AnchorTests(SimpleTestCase):
    """The View all case on a section shaped like the real one: arrows inside an {% if %} before the loop."""

    def setUp(self):
        self.lines = CAROUSEL.splitlines()
        self.controls = next(i for i, line in enumerate(self.lines, 1) if "carousel__controls" in line)
        self.wrapper = next(i for i, line in enumerate(self.lines, 1) if CAROUSEL_WRAPPER in line)

    def test_the_anchor_is_the_product_wrapper_not_the_arrows_block(self):
        _, facts = solve(QUESTION, CAROUSEL_FILES)
        self.assertEqual(facts.anchor, self.wrapper)
        self.assertNotEqual(facts.anchor, self.controls)
        self.assertEqual(facts.anchor_text, CAROUSEL_WRAPPER)
        self.assertFalse(facts.anchor_is_if)
        self.assertTrue(resolver.anchor_not_in_conditional(self.lines, facts.anchor))
        self.assertEqual(facts.snippet_tag, "btn btn-outline")  # not the arrows' button class

    def test_the_check_sees_that_the_arrows_line_is_inside_an_if(self):
        self.assertFalse(resolver.anchor_not_in_conditional(self.lines, self.controls))
        self.assertTrue(resolver.anchor_not_in_conditional(self.lines, self.wrapper))
        self.assertTrue(resolver.anchor_not_in_conditional(self.lines, 0))

    def test_the_answer_points_above_the_wrapper(self):
        (answer, _), = talk([QUESTION], CAROUSEL_FILES)
        self.assertIn(f"Go to line {self.wrapper}.", answer)
        self.assertIn(f"`{CAROUSEL_WRAPPER}`", answer)
        self.assertNotIn("carousel__controls", answer)
        self.assertNotIn("That is where the products start", answer)

    def test_products_inside_an_if_move_the_anchor_above_the_if_line(self):
        lines = BRANCHES.splitlines()
        if_line = next(i for i, line in enumerate(lines, 1) if "s.layout == 'carousel'" in line)
        _, facts = solve(QUESTION, BRANCH_FILES)
        self.assertEqual(facts.anchor, if_line)
        self.assertTrue(facts.anchor_is_if)
        self.assertTrue(resolver.anchor_not_in_conditional(lines, facts.anchor))
        (answer, _), = talk([QUESTION], BRANCH_FILES)
        self.assertIn(f"Go to line {if_line}.", answer)
        self.assertIn("'if' block", answer)
        self.assertNotIn("{%", answer)

    def test_a_section_wrapped_in_one_if_is_not_moved(self):
        lines = ["{% if section.settings.show %}", '<div class="product-grid grid">',
                 "  {% for product in collections.all.products %}{% endfor %}", "</div>", "{% endif %}"]
        self.assertEqual(resolver._anchor_line(lines, resolver.ELEMENTS["product grid"], above=True), 2)
        self.assertTrue(resolver.anchor_not_in_conditional(lines, 2))

    def test_nested_ifs_move_above_the_outermost(self):
        lines = ["<section>", "{% if a %}", "{% if b %}", '<div class="product-grid">',
                 "{% for product in collections.all.products %}{% endfor %}", "</div>", "{% endif %}", "{% endif %}",
                 "</section>"]
        self.assertEqual(resolver._anchor_line(lines, resolver.ELEMENTS["product grid"], above=True), 2)

    def test_control_lines_and_style_blocks_are_never_the_anchor(self):
        lines = ["<style>", ".carousel__controls { }", "</style>", '<div class="carousel__controls">', "</div>",
                 "{% for product in collections.all.products %}{% endfor %}"]
        self.assertEqual(resolver._anchor_line(lines, resolver.ELEMENTS["product grid"], above=True), 6)


class NotUnderstoodTests(SimpleTestCase):
    def test_the_same_steps_in_fewer_words_without_code_and_never_the_same(self):
        for files in (CUSTOM_FILES, FILES, CAROUSEL_FILES):
            answers = [a for a, _ in talk([QUESTION, "i ddint undertand", "i didn't understand", "not clear"], files)]
            first, second, third, fourth = answers
            self.assertEqual(len(numbered(second)), len(numbered(first)))  # the same steps again
            self.assertLess(words(second), words(first))
            self.assertLess(words(third), words(second))
            for answer in answers[1:]:
                for text in ("```", "view_all", "Step "):
                    self.assertNotIn(text, answer)
            self.assertEqual(len({first, second, third, fourth}), 4)  # never a repeat of an earlier answer

    def test_it_also_works_straight_after_an_older_style_answer(self):
        history = [{"role": "user", "content": QUESTION},
                   {"role": "assistant", "content": "Open Online Store > Edit code, then sections/product-grid.liquid."}]
        context = ctx(THEME_CTX)
        answer, _ = agent.run("i ddint undertand", context, chat=no_model, history=history, open_theme=fake_theme())
        self.assertTrue(answer.startswith("1. "))
        self.assertNotIn("```", answer)

    def test_the_intent_words(self):
        history = [{"role": "user", "content": QUESTION},
                   {"role": "assistant", "content": "1. Go to Online Store > Edit theme.\n2. Click it."}]
        for text, expected in [
            ("i ddint undertand", "simple"), ("i didn't understand", "simple"), ("I dont understand", "simple"),
            ("not clear", "simple"), ("explain again", "simple"), ("in simple words", "simple"), ("easy", "simple"),
            ("ardam kaledu", "simple"), ("malli cheppu", "simple"), ("show me the code", "code"), ("exact code", "code"),
            ("next", "done"), ("Done.", "done"), ("ok", "done"), ("continue", "done"),
            ("I understand, add a hero banner", "start"), ("make the header bigger and easier to use for my customers", "start"),
        ]:
            self.assertEqual(resolver.intent(text, history), expected, text)

    def test_done_means_nothing_without_a_guided_answer(self):
        for text in ("next", "ok", "done"):
            self.assertEqual(resolver.intent(text, ()), "start")
            self.assertEqual(resolver.intent(text, [{"role": "assistant", "content": "Net sales were ₹100."}]), "start")
            self.assertFalse(resolver.is_theme_request(resolver.analyse(text, ())))


class MissingFactsTests(SimpleTestCase):
    def test_nothing_found_asks_for_the_lines_and_changes_nothing(self):
        files = {"layout/theme.liquid": "<html></html>"}
        (answer, context), = talk([QUESTION], files)
        self.assertIn("Don't change anything yet", answer)
        self.assertIn("Paste them here", answer)
        self.assertNotIn("Step ", answer)
        self.assertNotIn("```", answer)
        self.assertEqual(context.opened_files, [])

    def test_the_ask_names_the_file_when_there_is_one(self):
        files = {"layout/theme.liquid": "<html></html>", "templates/index.liquid": "{% section 'x', type: 'other' %}"}
        (answer, _), = talk([QUESTION], files)
        self.assertIn("`templates/index.liquid`", answer)

    def test_no_line_in_the_markup_asks_for_the_lines(self):
        (answer, context), = talk([QUESTION], STYLE_ONLY)
        self.assertIn("`sections/product-grid.liquid`", answer)
        self.assertIn("Paste them here", answer)
        self.assertIn("Don't change anything yet", answer)
        self.assertEqual(context.opened_files, ["sections/product-grid.liquid"])

    def test_pasted_lines_after_the_ask_give_the_list_and_quote_the_line(self):
        answers = [a for a, _ in talk([QUESTION, PASTE], STYLE_ONLY)]
        self.assertIn("Paste them here", answers[0])
        self.assertTrue(answers[1].startswith("1. "))
        self.assertIn("`sections/product-grid.liquid`", answers[1])
        self.assertIn('Find this line: `<div class="product-grid">`.', answers[1])
        self.assertNotIn("Go to line", answers[1])  # a pasted cut has no real line numbers
        self.assertIn("```liquid", answers[1])
        self.assertFalse(answers[1].startswith(resolver.REVIEW_MARK))

    def test_the_ask_in_fewer_words(self):
        answers = [a for a, _ in talk([QUESTION, "i didn't understand"], STYLE_ONLY)]
        self.assertLess(words(answers[1]), words(answers[0]))
        self.assertIn("paste them here", answers[1])


# The lines a person pastes back after the answer for CAROUSEL_FILES
IN_THE_IF = NL.join([
    '  <div class="carousel-wrap">',
    "    {% if s.layout == 'carousel' and s.show_arrows %}",
    '      <a href="/collections/all" class="btn btn-outline">View all</a>',
    '      <div class="carousel__controls">',
    '        <button class="carousel__prev">Prev</button>',
    "      </div>",
    "    {% endif %}",
    "    {% for product in collections.all.products %}",
])
ABOVE_THE_WRAPPER = NL.join([
    '  <a href="/collections/all" class="btn btn-outline">View all</a>',
    '  <div class="carousel-wrap">',
    "    {% if s.layout == 'carousel' and s.show_arrows %}",
    '      <div class="carousel__controls">',
    "    {% endif %}",
    "    {% for product in collections.all.products %}",
])
BELOW_THE_WRAPPER = NL.join([
    '  <div class="carousel-wrap">',
    '    <a href="/collections/all" class="btn btn-outline">View all</a>',
    "    {% if s.layout == 'carousel' and s.show_arrows %}",
    "    {% endif %}",
    "    {% for product in collections.all.products %}",
])


def review_of(paste, files=CAROUSEL_FILES, first=QUESTION):
    answers = [a for a, _ in talk([first, paste], files)]
    return answers[0], answers[1]


class ReviewTests(SimpleTestCase):
    def check_review(self, answer, files=CAROUSEL_FILES):
        self.assertTrue(answer.startswith(resolver.REVIEW_MARK), answer)
        self.assertIn("`sections/product-grid.liquid`", answer)
        self.assertNotIn("1. ", answer)
        self.assertNotIn("Step ", answer)
        self.assertNotIn("```", answer)
        self.assertIsNone(re.search(r"\bline \d", answer))  # no invented line numbers
        self.assertLessEqual(len(re.findall(r"[.!?](?:\s|$)", answer)), 4)
        self.assertNotEqual(answer, agent.SAFE_STEP)
        self.assertEqual(resolver.unknown_theme_files(answer, sorted(files)), [])

    def test_a_button_inside_the_arrows_if_is_told_to_move_out_and_above_the_wrapper(self):
        first, answer = review_of(IN_THE_IF)
        self.assertTrue(first.startswith("1. "))
        self.check_review(answer)
        self.assertIn("inside an 'if' block", answer)
        self.assertIn(f"just above `{CAROUSEL_WRAPPER}`", answer)

    def test_a_button_above_the_wrapper_and_outside_every_if_is_right(self):
        _, answer = review_of(ABOVE_THE_WRAPPER)
        self.check_review(answer)
        self.assertIn(f"above `{CAROUSEL_WRAPPER}` and outside every 'if' block", answer)
        self.assertIn("right place", answer)

    def test_a_button_below_the_wrapper_is_told_to_move_up(self):
        _, answer = review_of(BELOW_THE_WRAPPER)
        self.check_review(answer)
        self.assertIn(f"below `{CAROUSEL_WRAPPER}`", answer)
        self.assertIn("Move it to just above that line", answer)

    def test_a_paste_with_a_file_name_and_a_fence_is_a_review_too(self):
        message = f"is this right? sections/product-grid.liquid\n```liquid\n{IN_THE_IF}\n```"
        _, answer = review_of(message)
        self.check_review(answer)
        self.assertIn("inside an 'if' block", answer)

    def test_a_paste_with_words_around_it_is_a_review(self):
        _, answer = review_of(f"I pasted my lines, please check:\n{ABOVE_THE_WRAPPER}")
        self.check_review(answer)
        self.assertIn("right place", answer)

    def test_the_button_alone_cannot_be_placed(self):
        _, answer = review_of('<a href="/collections/all" class="btn btn-outline">View all</a>')
        self.check_review(answer)
        self.assertIn("paste a few lines below", answer)

    def test_lines_without_a_new_button_ask_for_the_lines_around_it(self):
        _, answer = review_of(NL.join(CAROUSEL.splitlines()[3:13]))
        self.check_review(answer)
        self.assertIn("I cannot see your new button", answer)

    def test_a_second_paste_is_a_review_again_and_never_step_one(self):
        answers = [a for a, _ in talk([QUESTION, IN_THE_IF, ABOVE_THE_WRAPPER], CAROUSEL_FILES)]
        self.assertTrue(answers[0].startswith("1. "))
        self.check_review(answers[1])
        self.check_review(answers[2])
        self.assertIn("right place", answers[2])

    def test_a_review_names_only_real_files_even_when_the_paste_names_a_fake_one(self):
        _, answer = review_of(f"sections/grid.liquid\n{IN_THE_IF}")
        self.check_review(answer)
        self.assertNotIn("sections/grid.liquid", answer)

    def test_products_inside_an_if_in_the_real_file_are_mentioned_when_the_paste_hides_it(self):
        _, answer = review_of(ABOVE_THE_WRAPPER.replace("carousel-wrap", "carousel"), BRANCH_FILES)
        self.assertIn("above that 'if' line", answer)

    def test_the_model_is_not_asked_for_a_review(self):
        talk([QUESTION, IN_THE_IF], CAROUSEL_FILES)  # no_model raises if it is called

    def test_a_paste_that_answers_the_ask_is_not_a_review(self):
        answers = [a for a, _ in talk([QUESTION, PASTE], STYLE_ONLY)]
        self.assertFalse(answers[1].startswith(resolver.REVIEW_MARK))

    def test_the_review_edit_reader_directly(self):
        _, facts = solve(QUESTION, CAROUSEL_FILES)
        edit = resolver.read_edit(IN_THE_IF, facts)
        self.assertEqual((edit.verdict, edit.target), ("inside", CAROUSEL_WRAPPER))
        self.assertEqual(resolver.read_edit(ABOVE_THE_WRAPPER, facts).verdict, "right")
        self.assertEqual(resolver.read_edit(BELOW_THE_WRAPPER, facts).verdict, "below")
        # lines that start inside an if opened above them: the end of that if shows it
        cut = NL.join(['  <a href="/collections/all" class="btn">View all</a>', "  {% endif %}", "  <div>x</div>"])
        self.assertEqual(resolver.read_edit(cut, facts).verdict, "inside")


BUTTON_ONLY = '<a href="/collections/all" class="btn btn-outline">View all</a>'
LINES_BELOW = NL.join(['  <div class="carousel-wrap">', "    {% for product in collections.all.products %}", "    {% endfor %}", "  </div>"])
MORE_PHRASE = resolver.PASTE_MORE
FOLLOW_UPS = ("what should i do", "What should I do?", "then", "and now", "ok so", "help", "where", "what next", "now what")


class FollowUpTests(SimpleTestCase):
    def test_a_short_follow_up_after_a_list_gives_the_same_plan_in_fewer_words(self):
        for files in (CUSTOM_FILES, CAROUSEL_FILES):
            for text in FOLLOW_UPS:
                first, again = [a for a, _ in talk([QUESTION, text], files)]  # no_model raises if the model is asked
                self.assertEqual(len(numbered(again)), len(numbered(first)), text)
                self.assertLess(words(again), words(first), text)
                self.assertNotIn("```", again)
                self.assertNotEqual(again, first)

    def test_it_keeps_getting_shorter_and_never_repeats(self):
        answers = [a for a, _ in talk([QUESTION, "what should i do", "then", "and now"], CAROUSEL_FILES)]
        self.assertEqual(len(set(answers)), 4)
        self.assertLess(words(answers[2]), words(answers[1]))

    def test_after_a_review_it_stays_in_the_guided_flow(self):
        answers = [a for a, _ in talk([QUESTION, IN_THE_IF, "what should i do"], CAROUSEL_FILES)]
        self.assertTrue(answers[1].startswith(resolver.REVIEW_MARK))
        self.assertTrue(answers[2].startswith("1. "))
        self.assertIn("`sections/product-grid.liquid`", answers[2])
        self.assertNotEqual(answers[2], agent.SAFE_STEP)

    def test_the_same_words_go_to_the_model_when_the_last_answer_was_not_guided(self):
        history = [{"role": "user", "content": "How were sales?"}, {"role": "assistant", "content": "Net sales were ₹100."}]
        for text in FOLLOW_UPS:
            self.assertEqual(resolver.intent(text, history), "start", text)
            chat = scripted({"role": "assistant", "content": "ok"})
            answer, _ = agent.run(text, ctx(THEME_CTX), chat=chat, history=history, open_theme=fake_theme())
            self.assertEqual(answer, "ok")
        guided = [{"role": "user", "content": QUESTION}, {"role": "assistant", "content": "1. Go to Online Store > Edit theme."}]
        self.assertEqual(resolver.intent("what should i do", guided), "again")
        self.assertEqual(resolver.intent("how were sales", guided), "start")


class TwoPasteTests(SimpleTestCase):
    def test_the_button_alone_asks_for_more_lines_then_the_lines_below_are_combined(self):
        answers = [a for a, _ in talk([QUESTION, BUTTON_ONLY, LINES_BELOW], CAROUSEL_FILES)]
        self.assertIn(MORE_PHRASE, answers[1])
        self.assertTrue(answers[2].startswith(resolver.REVIEW_MARK))
        self.assertNotIn(MORE_PHRASE, answers[2])  # the owner is not asked again
        self.assertIn(f"above `{CAROUSEL_WRAPPER}` and outside every 'if' block", answers[2])
        self.assertIn("right place", answers[2])

    def test_a_button_pasted_below_the_wrapper_is_found_wrong_after_combining(self):
        below = NL.join(['  <div class="carousel-wrap">', "    {% for product in collections.all.products %}"])
        answers = [a for a, _ in talk([QUESTION, BUTTON_ONLY, below], CAROUSEL_FILES)]
        self.assertIn("right place", answers[2])  # the button came first in the combined lines

    def test_bare_closing_tags_count_as_a_paste_after_the_ask_for_more(self):
        answers = [a for a, _ in talk([QUESTION, BUTTON_ONLY, "  </div>"], CAROUSEL_FILES)]
        self.assertTrue(answers[2].startswith(resolver.REVIEW_MARK))
        self.assertNotEqual(answers[2], agent.SAFE_STEP)
        self.assertNotEqual(answers[2], answers[1])  # asked again in other words
        self.assertIn(MORE_PHRASE, answers[2])  # and the next paste is joined again

    def test_a_bare_closing_tag_is_not_a_paste_otherwise(self):
        self.assertFalse(resolver.is_paste("</div>", []))
        self.assertFalse(resolver.is_paste("</div>", [{"role": "assistant", "content": "Net sales."}]))

    def test_a_new_paste_that_repeats_the_old_one_is_not_doubled(self):
        both = BUTTON_ONLY + NL + LINES_BELOW
        history = [{"role": "assistant", "content": "x"}, {"role": "user", "content": BUTTON_ONLY},
                   {"role": "assistant", "content": f"{resolver.REVIEW_MARK}. {MORE_PHRASE}."}]
        self.assertEqual(resolver.joined_paste(history, both), both)
        self.assertEqual(resolver.joined_paste(history, LINES_BELOW), BUTTON_ONLY + NL + LINES_BELOW)

    def test_at_most_three_pastes_are_joined(self):
        ask = {"role": "assistant", "content": f"{resolver.REVIEW_MARK}. {MORE_PHRASE}."}
        history = []
        for part in ("<p>one</p>", "<p>two</p>", "<p>three</p>", "<p>four</p>"):
            history += [{"role": "user", "content": part}, ask]
        self.assertEqual(resolver.joined_paste(history, "<p>five</p>"), "<p>three</p>\n<p>four</p>\n<p>five</p>")

    def test_pastes_before_a_finished_review_are_not_joined(self):
        done = {"role": "assistant", "content": f"{resolver.REVIEW_MARK}. That is the right place."}
        history = [{"role": "user", "content": BUTTON_ONLY}, done]
        self.assertEqual(resolver.joined_paste(history, LINES_BELOW), LINES_BELOW)


class SafetyNetTests(SimpleTestCase):
    def test_every_answer_passes_the_checks(self):
        for files in (FILES, CUSTOM_FILES, CAROUSEL_FILES, BRANCH_FILES):
            request, facts = solve(QUESTION, files)
            history = []
            for message in (QUESTION, "next", "i didn't understand", "show code", IN_THE_IF):
                guide = steps.reply(resolver.analyse(message, history, sorted(files)), facts, message, history)
                self.assertIsNone(resolver.check_answer(guide.text, facts), guide.text)
                history += [{"role": "user", "content": message}, {"role": "assistant", "content": guide.text}]

    def test_the_checks_reject_variable_names_but_allow_a_code_block(self):
        _, facts = solve(QUESTION)
        self.assertEqual(resolver.check_answer("Fill in s.view_all_label.", facts), "shows a code variable")
        self.assertEqual(resolver.check_answer("Use {{ section.settings.x }}", facts), "shows a code variable")
        self.assertEqual(resolver.check_answer("Use view_all_link here", facts), "shows a code variable")
        self.assertIsNone(resolver.check_answer("Add:\n\n```liquid\n{{ product.title }}\n```", facts))

    def test_a_file_name_that_is_not_in_the_list_is_rejected_even_when_nothing_was_found(self):
        _, facts = solve(QUESTION, {"layout/theme.liquid": "<html></html>"})
        self.assertFalse(facts.found)
        self.assertEqual(resolver.check_answer("Open sections/grid.liquid.", facts), "unknown file sections/grid.liquid")
        self.assertIsNone(resolver.check_answer("Open layout/theme.liquid.", facts))

    def test_an_answer_that_fails_a_check_is_replaced_by_a_safe_reply(self):
        original = steps.reply

        def bad(*args, **kwargs):
            return steps.Reply("Open sections/grid.liquid at line 3.")

        steps.reply = bad
        try:
            with self.assertLogs("apps.assistant.agent", level="WARNING"):
                (answer, _), = talk([QUESTION])
        finally:
            steps.reply = original
        self.assertEqual(answer, agent.SAFE_STEP)

    def test_the_paste_path_is_guarded_too(self):
        original = steps.reply
        calls = []

        def bad(request, facts, message, history=()):
            calls.append(request.review)
            if request.review:
                return steps.Reply("I checked your lines in `sections/grid.liquid`. Move it up.")
            return original(request, facts, message, history)

        steps.reply = bad
        try:
            with self.assertLogs("apps.assistant.agent", level="WARNING"):
                answers = [a for a, _ in talk([QUESTION, IN_THE_IF], CAROUSEL_FILES)]
        finally:
            steps.reply = original
        self.assertEqual(calls, [False, True])
        self.assertEqual(answers[1], agent.SAFE_STEP)

    def test_labels_never_carry_ids_or_line_numbers(self):
        _, facts = solve(QUESTION, CUSTOM_FILES)
        self.assertEqual(facts.section_name, "Product grid")
        self.assertEqual(facts.page, "index")
        self.assertEqual(facts.customizer, ["View all label", "View all link"])
        self.assertEqual([kind for kind, _, _ in facts.custom_items], ["setting", "setting"])


class ModelPathFileGuardTests(SimpleTestCase):
    def run_model(self, text, open_theme=None):
        chat = scripted({"role": "assistant", "content": text})
        return agent.run("hello there", ctx(THEME_CTX), chat=chat, open_theme=open_theme or fake_theme())[0]

    def test_a_file_that_is_not_in_the_theme_replaces_the_answer(self):
        with self.assertLogs("apps.assistant.agent", level="WARNING"):
            answer = self.run_model("Open sections/grid.liquid and add it there.")
        self.assertEqual(answer, agent.SAFE_STEP)

    def test_files_in_the_theme_pass(self):
        text = "Open sections/header.liquid and then snippets/product-card.liquid."
        self.assertEqual(self.run_model(text), text)

    def test_an_answer_without_file_names_never_opens_the_theme(self):
        def never(context):
            raise AssertionError("the theme must not be opened")

        self.assertEqual(self.run_model("Sales were fine.", never), "Sales were fine.")

    def test_a_store_without_a_theme_has_no_list_to_check_against(self):
        self.assertEqual(self.run_model("Open sections/header.liquid.", lambda context: None),
                         "Open sections/header.liquid.")

    def test_a_failure_to_check_keeps_the_answer(self):
        def broken(context):
            raise RuntimeError("no database")

        with self.assertLogs("apps.assistant.agent", level="ERROR"):
            answer = self.run_model("Open sections/header.liquid.", broken)
        self.assertEqual(answer, "Open sections/header.liquid.")


class AgentRoutingTests(SimpleTestCase):
    def test_the_model_is_asked_for_everything_that_is_not_a_theme_request(self):
        for message in ("How were sales in the last 7 days?", "next"):
            chat = scripted({"role": "assistant", "content": "ok"})
            answer, _ = agent.run(message, ctx(THEME_CTX), chat=chat, open_theme=fake_theme())
            self.assertEqual(answer, "ok")
            self.assertEqual([m["role"] for m in chat.calls[0]["messages"]], ["system", "user"])

    def test_a_member_without_themes_view_goes_to_the_model(self):
        chat = scripted({"role": "assistant", "content": "ok"})
        answer, _ = agent.run(QUESTION, ctx({"orders.view"}), chat=chat, open_theme=fake_theme())
        self.assertEqual(answer, "ok")

    def test_a_store_without_a_theme_goes_to_the_model(self):
        chat = scripted({"role": "assistant", "content": "ok"})
        answer, _ = agent.run(QUESTION, ctx(THEME_CTX), chat=chat, open_theme=lambda c: None)
        self.assertEqual(answer, "ok")

    def test_the_prompt_asks_for_simple_words(self):
        context = ctx(THEME_CTX)
        chat = scripted({"role": "assistant", "content": "ok"})
        agent.run("hello", context, chat=chat)
        system = chat.calls[0]["messages"][0]["content"]
        self.assertIn("very simple English", system)
        self.assertIn("Never show code variable names", system)
        self.assertIn("Never mention Shopify", system)
