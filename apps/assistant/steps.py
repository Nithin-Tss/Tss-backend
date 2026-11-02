"""
Guided answers for theme requests: ALL the steps at once, as a short numbered list (3 to 5 short lines) in very
simple words. Python writes every list from the resolver's facts; the model is not asked, so the answer is exact
and instant.

Each step has three forms, longest first: the normal one, a shorter one and a tiny one. "I did not understand"
gives the whole list again in the next shorter form (never the same words as before, and never code). "show code"
gives the full list, with the code, again.

The customizer comes first: a setting or block that does the job is named by its label and its section, with
Online Store > Edit theme clicks. File names, line numbers and code appear only when the customizer cannot do it,
and the code comes last. When the facts are missing, the person is asked to paste the few lines instead.

When the person pastes their own edit after a guided answer, that is a review of THEIR edit (see review): it says
whether the new code is above the product loop's wrapper and outside every {% if %}, and never starts over.
"""
from dataclasses import dataclass, field

from . import resolver
from .resolver import ELEMENTS, PAGE_LABELS, REVIEW_MARK

DONE = "Great. Ask me if you need anything else."
CHECK = "Check the preview after saving."
ORDER = ("toggle", "setting", "block", "block_toggle", "block_setting")
MAX_LINES = 5
AGAIN_MODES = ("simple", "again")  # "I did not understand" and "what should i do": the same plan, in fewer words


@dataclass
class Plan:
    kind: str  # "customizer" | "code" | "ask"
    steps: list = field(default_factory=list)  # [(full, short, tiny)]


@dataclass
class Reply:
    text: str
    code_button: bool = False  # the file's Code editor button helps
    customize_button: bool = False  # the Customize button helps


def _quoted(labels):
    return " and ".join(f'"{label}"' for label in labels[:3])


def _them(labels):
    return "it" if len(labels[:3]) == 1 else "them"


def _capped(steps):
    """At most MAX_LINES lines: the last two steps are joined until the list is short enough."""
    steps = list(steps)
    while len(steps) > MAX_LINES:
        last, before = steps.pop(), steps.pop()
        steps.append(tuple(f"{a} {b}" for a, b in zip(before, last)))
    return steps


def _customizer_steps(facts):
    page = PAGE_LABELS.get(facts.page)
    section = facts.section_name
    steps = [("Good news: your theme can do this, no code needed. Go to Online Store > Edit theme.",
              "Go to Online Store > Edit theme.", "Open Edit theme.")]
    where = f"Open the {page}." if page else "Open the page you want to change."
    if section:
        steps.append((f'{where} Click the "{section}" section.', f'Click the "{section}" section.', f'Click "{section}".'))
    else:
        steps.append((f"{where} Click the section for this.", "Click the section for this.", "Click the section."))
    if facts.hidden:
        steps.append(("This section is hidden. Click the eye icon next to it to show it.",
                      "Click the eye icon to show it.", "Show the section."))
    groups = {}
    for kind, label, block in facts.custom_items:
        groups.setdefault((kind, block), []).append(label)
    for (kind, block), labels in sorted(groups.items(), key=lambda g: ORDER.index(g[0][0])):
        q, it = _quoted(labels), _them(labels)
        if kind in ("toggle", "block_toggle"):
            prefix = f'Open the "{block}" block. ' if block else ""
            steps.append((f"{prefix}Find {q} and turn {it} on.", f"Turn on {q}.", f"Tap {q}."))
        elif kind == "block":
            steps.append((f'Click "Add block". Pick {q}.', f"Add the block {q}.", f"Add {q}."))
        else:
            prefix = f'Open the "{block}" block. ' if block else ""
            steps.append((f"{prefix}Find {q}. Type your words in {it}.", f"Fill in {q}.", f"Fill {q}."))
    steps.append((CHECK, "Save, then look at the preview.", "Save, then check."))
    return _capped(steps)


def _code_steps(facts):
    primary = facts.primary
    steps = []
    if facts.hidden:
        steps.append(("This section is hidden on the page. In Online Store > Edit theme, click the eye icon next "
                      "to it to show it.", "Show the section: click its eye icon.", "Show the section."))
    steps.append((
        f"The customizer has no setting for this, so we add a small piece of code. Go to Online Store > Edit code "
        f"and open `{primary}`. Don't change anything yet.",
        f"Go to Online Store > Edit code and open `{primary}`.",
        "Open the file in Edit code.",
    ))
    quote = facts.anchor_text.replace("`", "")
    if facts.pasted:  # the lines are the person's own cut: a line number would mean nothing there
        steps.append((f"Find this line: `{quote}`.", "Find that line.", "Find that line."))
    else:
        if facts.anchor_is_if:
            shown = " It starts an 'if' block that holds the products. Your code goes above it, not inside it."
        elif quote:
            shown = f" It looks like this: `{quote}`."
            if facts.above and "product grid" in facts.elements:
                shown += " The products are inside this box."
        else:
            shown = ""
        steps.append((f"Go to line {facts.anchor}.{shown}", f"Go to line {facts.anchor}.", f"Line {facts.anchor}."))
    place = "just above" if facts.above else "near"
    if "button" in facts.elements and facts.snippet_tag:
        label = facts.label or "Button text"
        code = f'```liquid\n<a href="PICK-YOUR-LINK" class="{facts.snippet_tag}">{label}</a>\n```'
        note = "Replace `PICK-YOUR-LINK` with the page you choose. I won't guess it."
        if facts.urls:
            note = "Links this theme already uses: " + ", ".join(f"`{u}`" for u in facts.urls[:3]) + ". " + note
        steps.append((
            f"Add this {place} that line:\n\n{code}",
            f"Add a small link {place} that line, like the one around it.",
            f"Add the link {place} it.",
        ))
        steps.append((f"{note} {CHECK}", "Choose the page for the link yourself. Save, then check.", "Save, then check."))
    else:
        steps.append((
            f"Make your change {place} that line. Copy the markup that is already there. {CHECK}",
            f"Change {place} that line, like the markup around it. {CHECK}",
            f"Change that line. {CHECK}",
        ))
    return _capped(steps)


def _ask_forms(request, facts):
    """What to say when the right place cannot be seen: ask for the few lines, and change nothing yet."""
    path = ""
    if facts.found:
        path = facts.primary
    else:
        names = facts.names
        if request.pages and f"templates/{request.pages[0]}.liquid" in names:
            path = f"templates/{request.pages[0]}.liquid"
        else:
            for key in request.targets:
                path = next((p for p in ELEMENTS[key]["files"] if p in names), "")
                if path:
                    break
    if path:
        return (
            f"I cannot see the right place yet. Don't change anything yet.\n\nGo to Online Store > Edit code and "
            f"open `{path}`. Copy its first 30 lines. Paste them here.",
            f"Don't change anything yet. Open `{path}` in Edit code. Copy a few lines and paste them here.",
            f"Copy a few lines from `{path}` and paste them here.",
        )
    return (
        "I cannot see the right place yet. Don't change anything yet.\n\nGo to Online Store > Edit code and open "
        "the file for that part of your page. Copy a few lines from it. Paste them here.",
        "Don't change anything yet. Open the file in Edit code. Copy a few lines and paste them here.",
        "Copy a few lines from the file and paste them here.",
    )


def build_plan(request, facts):
    if facts.found and facts.custom_items:
        return Plan("customizer", _customizer_steps(facts))
    needs_line = facts.anchor_text if facts.pasted else facts.anchor
    if facts.found and needs_line:
        return Plan("code", _code_steps(facts))
    return Plan("ask", [_ask_forms(request, facts)])


def _lists(steps):
    """The three whole answers (normal, shorter, tiny): every step as one numbered line."""
    return tuple("\n".join(f"{number}. {step[level]}" for number, step in enumerate(steps, start=1)) for level in range(3))


def _same(a, b):
    return " ".join(a.strip().lower().split()) == " ".join(b.strip().lower().split())


def _simpler(forms, previous):
    """The next shorter form than the one just shown (the short one when the last answer was something else)."""
    shown = next((i for i, form in enumerate(forms) if _same(form, previous)), None)
    wanted = 1 if shown is None else shown + 1
    if wanted < len(forms):
        return forms[wanted]
    return "Okay, one more time:\n\n" + forms[-1]


def _last_assistant(history):
    for item in reversed(list(history or ())):
        if item.get("role") == "assistant":
            return item.get("content") or ""
    return ""


def review(facts, edit, again=False):
    """2 to 3 simple sentences about the person's own edit. Lines are quoted, never numbered."""
    start = f"{REVIEW_MARK} in `{facts.primary}`."
    above = "the 'if' line that holds the products" if edit.target_is_if else (
        f"`{edit.target}`" if edit.target else "the start of the product list")
    if edit.verdict == "missing":
        return f"{start} I cannot see your new button in them. Paste the lines around it and I will check again."
    if edit.verdict == "inside":
        where = ("just above the 'if' line that holds the products" if edit.target_is_if else
                 f"just above `{edit.target}`" if edit.target else "above the products")
        return (f"{start} Your new button is inside an 'if' block, so it shows only when that 'if' is true. "
                f"Move it out of the block, to {where}.")
    if edit.verdict == "below":
        return (f"{start} Your new button is below {above}, so it is not on top of the products. "
                f"Move it to just above that line.")
    if edit.verdict == "right":
        text = f"{start} Your new button is above {above} and outside every 'if' block. That is the right place."
        if edit.check_if:
            text = (f"{start} Your new button is above the products. In the real file the products sit inside an "
                    f"'if' block, so make sure your button is above that 'if' line, not inside it.")
        return text + " Save, then check the preview."
    if again:  # asked once already: other words, so the answer is never the same twice
        return (f"{start} Thank you. I still cannot see the product list, so {resolver.PASTE_MORE}, "
                f"a bit further down.")
    return (f"{start} Your new button is outside every 'if' block. "
            f"I cannot see the product list in these lines, so {resolver.PASTE_MORE}.")


def reply(request, facts, message, history=()):
    """The guided message for a theme request (see the module docstring)."""
    mode = resolver.intent(message, history)
    if request.review and facts.found:  # the person's own edit: a review, never a new list
        asked_before = resolver.PASTE_MORE in _last_assistant(history)
        return Reply(review(facts, resolver.read_edit(request.pasted_text, facts), asked_before), code_button=True)
    if mode == "done":
        return Reply(DONE)

    plan = build_plan(request, facts)
    previous = _last_assistant(history)
    if plan.kind == "ask":
        forms = plan.steps[0]
        text = _simpler(forms, previous) if mode in AGAIN_MODES else forms[0]
        return Reply(text, code_button=facts.found)

    forms = _lists(plan.steps)
    prefix = "You do not need code for this.\n\n" if mode == "code" and plan.kind != "code" else ""
    body = _simpler(forms, previous) if mode in AGAIN_MODES else forms[0]
    return Reply(prefix + body, code_button=plan.kind == "code", customize_button=plan.kind == "customizer")
