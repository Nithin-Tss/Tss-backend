"""Re-index a help article's passages when it is saved (published, edited, unpublished)."""
import logging

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from . import indexing
from .models import HelpArticle

logger = logging.getLogger(__name__)


@receiver(post_save, sender=HelpArticle)
def reindex_on_save(sender, instance, raw=False, **kwargs):
    if raw or not indexing.enabled():
        return

    def run():
        try:
            indexing.index_article(HelpArticle.objects.get(pk=instance.pk))
        except HelpArticle.DoesNotExist:
            pass
        except Exception:  # saving an article must never fail because indexing did
            logger.exception("help rag: indexing article %s failed", instance.pk)

    transaction.on_commit(run)
