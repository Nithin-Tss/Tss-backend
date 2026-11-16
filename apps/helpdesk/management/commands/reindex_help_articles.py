from django.core.management.base import BaseCommand

from apps.helpdesk import indexing


class Command(BaseCommand):
    help = "Split the published help articles into passages and embed them for the help assistant's search."

    def add_arguments(self, parser):
        parser.add_argument("--missing", action="store_true", help="Only articles without passages or vectors.")

    def handle(self, *args, **options):
        articles, chunks = indexing.reindex_all(only_missing=options["missing"])
        self.stdout.write(self.style.SUCCESS(f"Indexed {articles} article(s), {chunks} passage(s)."))
