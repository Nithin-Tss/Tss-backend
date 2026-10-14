import re

from django.db import transaction

from apps.tenancy.models import Store


@transaction.atomic
def next_number(store, model, field, prefix, start=1001, suffix=""):
    """
    The next human-readable number for a store, e.g. "#1001", "PO-1002", "#1003-A".
    Locks the store row so two requests can't get the same number.
    """
    Store.objects.select_for_update().filter(pk=store.pk).first()

    highest = start - 1
    for value in model.objects.filter(store=store).values_list(field, flat=True):
        value = value or ""
        if suffix and value.endswith(suffix):
            value = value[: -len(suffix)]
        match = re.search(r"(\d+)$", value)
        if match:
            highest = max(highest, int(match.group(1)))

    return f"{prefix}{highest + 1}{suffix}"
