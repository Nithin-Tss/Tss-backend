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
    op = "create" if before is None else "update"
    change = services.build_change(instance, before or {}, after, op)
    if op == "update" and not change["fields"]:
        return
    collector.note((sender._meta.label, str(instance.pk)), change)


def note_delete(instance):
    """Hard deletes: called from the viewset when it loads the row that DELETE targets."""
    collector = services.current()
    if collector is None or not collector.collect or not services.auditable(type(instance)):
        return
    snap = services.snapshot(instance)
    collector.deletes.append(services.build_change(instance, snap, {}, "delete"))


pre_save.connect(remember_before, dispatch_uid="audit_remember_before")
post_save.connect(note_change, dispatch_uid="audit_note_change")
