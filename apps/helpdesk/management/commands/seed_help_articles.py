"""
Create the starter help articles. Safe to run again: an article that already exists (by slug) is never changed, so
edits made in the Django admin are kept.

    python manage.py seed_help_articles

- One published article per entry of apps/assistant/knowledge/admin_guide.md (labels verified against the admin).
- For each topic the guide does not cover (store settings, payments, shipping, discounts, domains, themes) a
  placeholder marked TODO, left UNPUBLISHED so the assistant never answers from placeholder text. Write the real
  content in the Django admin, then tick "Is published".
"""
from django.core.management.base import BaseCommand

from apps.assistant import guide
from apps.helpdesk.models import HelpArticle

CATEGORY_BY_OPEN = {
    "customize": "themes", "code": "themes", "online_store": "themes", "menus": "themes", "pages": "themes",
    "discounts": "discounts", "add_product": "other", "products": "other",
}

PLACEHOLDERS = {
    "store_settings": ("Store settings", "store name, contact email, currency, timezone, units, order number prefix"),
    "payments": ("Payments", "stripe, accept payments, payouts, payment methods, cash on delivery"),
    "shipping": ("Shipping and delivery", "shipping rates, shipping zones, free shipping, delivery, tax on shipping"),
    "discounts": ("Discounts", "discount code, coupon, percentage off, free shipping discount"),
    "domains": ("Domains", "custom domain, connect domain, dns, ssl certificate, primary domain"),
    "themes": ("Themes", "theme, customize, change theme, theme editor, logo, colours"),
}


def article_from_entry(entry):
    steps = "\n".join(f"{i}. {step}" for i, step in enumerate(entry.steps, start=1))
    body = f"## {entry.task}\n\n{steps}\n"
    if entry.note:
        body += f"\n**Note:** {entry.note}\n"
    return {
        "title": entry.task,
        "category": CATEGORY_BY_OPEN.get(entry.open, "other"),
        "body": body,
        "keywords": ", ".join(entry.keywords),
        "is_published": True,
    }


class Command(BaseCommand):
    help = "Create the starter help articles (existing articles are left untouched)."

    def handle(self, *args, **options):
        created = 0
        for entry in guide.load():
            _, was_created = HelpArticle.objects.get_or_create(slug=entry.id, defaults=article_from_entry(entry))
            created += was_created
        have = set(HelpArticle.objects.filter(is_published=True).values_list("category", flat=True))
        for category, (title, keywords) in PLACEHOLDERS.items():
            if category in have:
                continue
            _, was_created = HelpArticle.objects.get_or_create(
                slug=f"todo-{category.replace('_', '-')}",
                defaults={
                    "title": f"{title} (TODO)", "category": category, "keywords": keywords, "is_published": False,
                    "body": f"TODO: write the help article for {title.lower()}. Describe only screens and labels "
                            "that exist in the admin.\n",
                },
            )
            created += was_created
        self.stdout.write(self.style.SUCCESS(f"{created} help article(s) created."))
