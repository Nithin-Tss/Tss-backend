"""Publish or discard a draft the assistant made. Only merchants can do this; the model has no tool for it."""
import copy

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.utils import timezone
from rest_framework import exceptions

from apps.themes.models import StorePage
from apps.themes.services import delete_path, get_store_theme

from .models import AiPageDraft


def _draft(store, page_id):
    try:
        draft = AiPageDraft.objects.select_related("page").filter(store=store, page_id=page_id).first()
    except (ValueError, TypeError, DjangoValidationError):
        draft = None
    if draft is None:
        raise exceptions.NotFound("That draft wasn't made by the assistant, or it's already published or discarded.")
    return draft


@transaction.atomic
def publish_draft(store, page_id):
    draft = _draft(store, page_id)
    page = draft.page
    page.status = StorePage.VISIBLE
    if not page.published_at:
        page.published_at = timezone.now()
    page.save(update_fields=["status", "published_at", "updated_at"])
    draft.delete()
    return page


def _drop_template(store_theme, key):
    try:
        delete_path(store_theme, f"templates/{key}.liquid")
    except exceptions.NotFound:
        pass


@transaction.atomic
def discard_draft(store, page_id):
    """A page the assistant created is deleted; one it only edited goes back to how it was."""
    draft = _draft(store, page_id)
    page = draft.page
    if page.status != StorePage.HIDDEN:
        raise exceptions.ValidationError({"detail": "This page is already published."})
    store_theme = get_store_theme(store)
    if draft.created_by_ai:
        key = page.template
        page.delete()
        if key:
            _drop_template(store_theme, key)
        return {"discarded": "deleted"}

    original = draft.original or {}
    old_key = original.get("template", "")
    if page.template != old_key:  # the assistant gave the page a template of its own: drop it
        new_key = page.template
        _drop_template(store_theme, new_key)
        page.refresh_from_db()
    elif original.get("template_data") is not None:
        data = copy.deepcopy(store_theme.custom_data or {})
        data.setdefault("templates", {})[old_key] = original["template_data"]
        store_theme.custom_data = data
        store_theme.save(update_fields=["custom_data", "updated_at"])
    page.template = old_key
    page.save(update_fields=["template", "updated_at"])
    draft.delete()
    return {"discarded": "restored"}
