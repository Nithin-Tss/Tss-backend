import difflib
import re

from django.core.management.base import BaseCommand, CommandError

from apps.themes import section_sync as sync
from apps.themes.models import Theme
from apps.themes.services import starter_files

PATH = "sections/main-product.liquid"
# Old copies print the SKU as the button label and never read product.options.
STALE_MARK = "variant.sku | default: 'Option'"

BACK_MARKUP = re.compile(r"[ \t]*\{%-? comment -?%\}\s*\"< Back\".*?</div>\r?\n", re.S)
BACK_CSS = re.compile(r"[ \t]*#section-\{\{ section\.id \}\} \.product-back-wrap.*?\.product-back svg \{.*?\}\r?\n", re.S)
BACK_JS = re.compile(r"[ \t]*// \"< Back\":.*?\r?\n    \}\r?\n(?=\r?\n)", re.S)


def merge(live, starter):
    """The starter's picker, plus the theme's own "< Back" link (markup, CSS, script) when it has one."""
    new = starter.replace("\r\n", "\n")
    old = live.replace("\r\n", "\n")
    if "data-product-back" not in old:
        return new
    markup, css, js = (rx.search(old) for rx in (BACK_MARKUP, BACK_CSS, BACK_JS))
    if not (markup and css and js):
        raise ValueError("could not find the Back link parts in this copy; merge it by hand")
    new = new.replace('<section id="section-{{ section.id }}" class="section">\n',
                      '<section id="section-{{ section.id }}" class="section">\n' + markup.group(0), 1)
    new = new.replace("  #section-{{ section.id }} .product__title {", css.group(0) + "  #section-{{ section.id }} .product__title {", 1)
    new = new.replace("    // Image zoom:", js.group(0) + "\n    // Image zoom:", 1)
    return new


class Command(BaseCommand):
    help = (
        "Give themes whose sections/main-product.liquid still shows SKUs the starter's variant picker "
        "(option name heading, value-only buttons). Dry run unless --apply; the old file is kept in the section history."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="write the changes (default: only show them)")
        parser.add_argument("--theme", help="only this theme id")

    def handle(self, *args, **options):
        if options["apply"] and not options["theme"]:
            raise CommandError("--apply needs --theme <id>: this template is stale in many stores' themes, update them one at a time")
        new_starter = starter_files()[PATH]
        themes = Theme.objects.all()
        if options["theme"]:
            themes = themes.filter(pk=options["theme"])
        changed = 0
        for theme in themes:
            file = theme.files.filter(file_path=PATH).first()
            if file is None or STALE_MARK not in file.content:
                continue
            try:
                new = merge(file.content, new_starter)
            except ValueError as error:
                self.stdout.write(self.style.ERROR(f"{theme.name} ({theme.pk}): {error}"))
                continue
            diff = list(difflib.unified_diff(file.content.replace("\r\n", "\n").splitlines(), new.splitlines(), "current", "new", lineterm="", n=0))
            added = sum(1 for d in diff if d.startswith("+") and not d.startswith("+++"))
            removed = sum(1 for d in diff if d.startswith("-") and not d.startswith("---"))
            self.stdout.write(f"{theme.name} ({theme.pk}): +{added} -{removed} lines, Back link kept: {'data-product-back' in new}")
            if not options["apply"]:
                continue
            section = sync.PATH_TO_SECTION.get(PATH)
            if section:
                sync.record_version(theme, section, file.content, "upgrade")
                sync.invalidate(theme.pk, PATH)
            file.content = new
            file.file_size = len(new.encode("utf-8"))
            file.save(update_fields=["content", "file_size", "updated_at"])
            changed += 1
        verb = "Updated" if options["apply"] else "Would update (dry run, pass --apply)"
        self.stdout.write(f"{verb}: {changed if options['apply'] else 'see above'}")
