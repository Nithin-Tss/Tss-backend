"""Model signals that feed the current request's audit notes (see services.py)."""
from django.db.models.signals import post_save, pre_save

from . import services


def _active(sender):
    collector = services.current()
    if collector is None or not collector.collect or not services.auditable(sender):
        return None
    return collector


def remember_before(sender, instance, raw=False, **kwargs):
    if raw or _active(sender) is None:
        return
    instance._audit_before = None
    if instance.pk is not None and not instance._state.adding:
        old = sender._base_manager.filter(pk=instance.pk).first()
        if old is not None:
            instance._audit_before = services.snapshot(old)


def note_change(sender, instance, created=False, raw=False, **kwargs):
    collector = _active(sender)
    if collector is None or raw:
        return
    after = services.snapshot(instance)
    before = getattr(instance, "_audit_before", None)
    if created:
        op = "create"
    elif before is None:
        # Saved without a read first (a signal sent by hand after queryset.update()): we know
        # what the row is now, not what it was, so say that instead of calling it a create.
        change = services.build_change(instance, {}, after, "create")
        change["op"] = "update"
        change["partial"] = True
        collector.note((sender._meta.label, str(instance.pk)), change)
        return
    else:
        op = "update"
    change = services.build_change(instance, before or {}, after, op)
    if op == "update" and not change["fields"]:
        return
    collector.note((sender._meta.label, str(instance.pk)), change)


def note_delete(instance):
    """Hard deletes: called from the viewset when it loads the row that DELETE targets."""
    services.note_deleted([instance])


pre_save.connect(remember_before, dispatch_uid="audit_remember_before")
post_save.connect(note_change, dispatch_uid="audit_note_change")
