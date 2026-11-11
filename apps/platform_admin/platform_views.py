"""
Platform administration: /api/v1/platform/... Only platform administrators (User.is_owner).

    GET  overview/                          totals and recent activity across every store
    GET  stores/  stores/<id>/              find and inspect stores (?q, ?status, ?limit, ?offset)
    POST stores/<id>/suspend/ unsuspend/    block / restore a store without touching its data
    GET  users/   users/<id>/               find and inspect accounts (?q, ?admin=true)
    POST users/<id>/platform-admin/         {"isPlatformAdmin": true|false}
    POST users/<id>/support-agent/          {"isSupportAgent": true|false}
    GET  api-keys/                          every store's keys (?store, ?status, ?q)
    POST api-keys/<id>/revoke/
    GET/POST feature-flags/   GET/PATCH/DELETE feature-flags/<key>/
    GET  feature-flags/<key>/overrides/     PUT/DELETE feature-flags/<key>/overrides/<store id>/
    GET  audit/                             the audit log of every store (same filters as a store's, plus ?store)

Closing, reopening and permanently deleting a store stay on /api/v1/stores/<id>/ (also platform-admin only for the last two).
Everything that changes something is written to the audit log (resource "platform").
"""
from collections import Counter
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.audit import services as audit
from apps.audit.models import AuditLog
from apps.audit.views import DEFAULT_LIMIT, MAX_LIMIT, _int, entry_json, filtered
from apps.tenancy.models import Store, StoreMembership
from apps.tenancy.services import ACTIVE, CLOSED, SUSPENDED

from . import flags
from .models import ApiKey, FeatureFlag, FeatureFlagOverride
from .permissions import IsPlatformAdmin
from .serializers import ApiKeySerializer, FeatureFlagSerializer

User = get_user_model()


class PlatformView(APIView):
    permission_classes = [IsAuthenticated, IsPlatformAdmin]


def paged(request, queryset, serialize):
    limit = _int(request, "limit", DEFAULT_LIMIT, MAX_LIMIT) or DEFAULT_LIMIT
    offset = _int(request, "offset", 0)
    return Response({
        "count": queryset.count(), "limit": limit, "offset": offset,
        "results": [serialize(row) for row in queryset[offset:offset + limit]],
    })


def note(request, action, *, store=None, object_id="", object_repr="", **metadata):
    """Write one platform action to the audit log (and so to the affected store's own log)."""
    audit.record(
        f"platform.{action}", request=request, store=store, user=request.user, resource="platform",
        object_id=object_id, object_repr=object_repr, status_code=200, metadata=metadata,
    )


# ---------------------------------------------------------------- stores

def store_json(store, detail=False):
    data = {
        "storeId": str(store.store_id),
        "storeName": store.store_name,
        "storeSlug": store.store_slug,
        "status": store.status,
        "createdBy": store.created_by or None,
        "createdAt": store.created_at,
        "memberCount": getattr(store, "member_count", None),
        "apiKeyCount": getattr(store, "key_count", None),
    }
    if detail:
        data["members"] = [
            {"email": m.user.email, "name": m.user.get_full_name(), "role": m.role, "status": m.status}
            for m in store.memberships.select_related("user").order_by("created_at")
        ]
        data["flags"] = flags.flags_for_store(store)
        data["currency"] = store.currency
        data["contactEmail"] = store.contact_email
    return data


def annotated_stores():
    return Store.objects.annotate(
        member_count=Count("memberships", filter=Q(memberships__status__in=[ACTIVE, "store_closed"]), distinct=True),
        key_count=Count("api_keys", filter=Q(api_keys__revoked_at__isnull=True), distinct=True),
    )


class StoreListView(PlatformView):
    """GET /api/v1/platform/stores/  every store (?q name/slug/creator, ?status=active|suspended|closed)"""

    def get(self, request):
        stores = annotated_stores().order_by("-created_at")
        if q := request.query_params.get("q"):
            stores = stores.filter(Q(store_name__icontains=q) | Q(store_slug__icontains=q) | Q(created_by__icontains=q))
        if status := request.query_params.get("status"):
            stores = stores.filter(status=status)
        return paged(request, stores, store_json)


def get_store(store_id):
    store = annotated_stores().filter(pk=store_id).first()
    if store is None:
        raise NotFound("Store not found.")
    return store


class StoreDetailView(PlatformView):
    """GET /api/v1/platform/stores/<id>/"""

    def get(self, request, store_id):
        return Response(store_json(get_store(store_id), detail=True))


class StoreSuspendView(PlatformView):
    """POST /api/v1/platform/stores/<id>/suspend/  {"reason": "..."}: members and API keys are refused until unsuspended"""

    def post(self, request, store_id):
        store = get_store(store_id)
        reason = str(request.data.get("reason", "")).strip()[:300]
        if store.status == CLOSED:
            raise ValidationError({"detail": "A closed store can't be suspended. Reopen it first."})
        if store.status != SUSPENDED:
            store.status = SUSPENDED
            store.save(update_fields=["status", "updated_at"])
            note(request, "store_suspended", store=store, object_id=store.pk, object_repr=store.store_name, reason=reason)
        return Response(store_json(get_store(store_id)))


class StoreUnsuspendView(PlatformView):
    """POST /api/v1/platform/stores/<id>/unsuspend/"""

    def post(self, request, store_id):
        store = get_store(store_id)
        if store.status == SUSPENDED:
            store.status = ACTIVE
            store.save(update_fields=["status", "updated_at"])
            note(request, "store_unsuspended", store=store, object_id=store.pk, object_repr=store.store_name)
        return Response(store_json(get_store(store_id)))


# ---------------------------------------------------------------- users

def user_json(user, detail=False):
    data = {
        "id": str(user.pk),
        "email": user.email,
        "firstName": user.first_name,
        "lastName": user.last_name,
        "isPlatformAdmin": user.is_owner,
        "isSupportAgent": user.is_support_agent,
        "emailVerified": user.email_verified_at is not None,
        "lastLogin": user.last_login,
        "createdAt": user.created_at,
        "storeCount": getattr(user, "store_count", None),
    }
    if detail:
        data["stores"] = [
            {"storeId": str(m.store_id), "storeName": m.store.store_name, "role": m.role, "status": m.status}
            for m in user.store_memberships.select_related("store").order_by("created_at")
        ]
    return data


def annotated_users():
    return User.objects.annotate(store_count=Count("store_memberships", filter=Q(store_memberships__status=ACTIVE), distinct=True))


class UserListView(PlatformView):
    """GET /api/v1/platform/users/  (?q name/email, ?admin=true for platform administrators only)"""

    def get(self, request):
        users = annotated_users().order_by("-created_at")
        if q := request.query_params.get("q"):
            users = users.filter(Q(email__icontains=q) | Q(first_name__icontains=q) | Q(last_name__icontains=q))
        if request.query_params.get("admin") in ("true", "1"):
            users = users.filter(is_owner=True)
        return paged(request, users, user_json)


def get_user(user_id):
    user = annotated_users().filter(pk=user_id).first()
    if user is None:
        raise NotFound("User not found.")
    return user


class UserDetailView(PlatformView):
    """GET /api/v1/platform/users/<id>/"""

    def get(self, request, user_id):
        return Response(user_json(get_user(user_id), detail=True))


class UserPlatformAdminView(PlatformView):
    """POST /api/v1/platform/users/<id>/platform-admin/  {"isPlatformAdmin": true|false}"""

    @transaction.atomic
    def post(self, request, user_id):
        make_admin = request.data.get("isPlatformAdmin")
        if not isinstance(make_admin, bool):
            raise ValidationError({"isPlatformAdmin": "Send true or false."})

        # Lock every administrator row so two demotions can't both see "another admin remains"
        admins = list(User.objects.select_for_update().filter(is_owner=True).values_list("pk", flat=True))
        user = get_user(user_id)

        if user.is_owner != make_admin:
            if not make_admin:
                if user.pk == request.user.pk:
                    raise ValidationError({"detail": "You can't remove your own platform administrator access."})
                if len(admins) <= 1:
                    raise ValidationError({"detail": "There must be at least one platform administrator."})
            User.objects.filter(pk=user.pk).update(is_owner=make_admin)
            note(
                request, "admin_granted" if make_admin else "admin_revoked",
                object_id=user.pk, object_repr=user.email,
            )
        return Response(user_json(get_user(user_id)))


class UserSupportAgentView(PlatformView):
    """POST /api/v1/platform/users/<id>/support-agent/  {"isSupportAgent": true|false}"""

    @transaction.atomic
    def post(self, request, user_id):
        make_agent = request.data.get("isSupportAgent")
        if not isinstance(make_agent, bool):
            raise ValidationError({"isSupportAgent": "Send true or false."})
        user = get_user(user_id)
        if user.is_support_agent != make_agent:
            User.objects.filter(pk=user.pk).update(is_support_agent=make_agent)
            note(
                request, "support_agent_granted" if make_agent else "support_agent_revoked",
                object_id=user.pk, object_repr=user.email,
            )
        return Response(user_json(get_user(user_id)))


# ---------------------------------------------------------------- api keys

class ApiKeyListView(PlatformView):
    """GET /api/v1/platform/api-keys/  (?store=<id>, ?status=active|revoked|expired, ?q name/prefix)"""

    def get(self, request):
        keys = ApiKey.objects.select_related("store", "created_by").order_by("-created_at")
        if store := request.query_params.get("store"):
            keys = keys.filter(store_id=store)
        if q := request.query_params.get("q"):
            keys = keys.filter(Q(name__icontains=q) | Q(prefix__icontains=q) | Q(store__store_name__icontains=q))
        now = timezone.now()
        match request.query_params.get("status"):
            case "revoked":
                keys = keys.filter(revoked_at__isnull=False)
            case "expired":
                keys = keys.filter(revoked_at__isnull=True, expires_at__lte=now)
            case "active":
                keys = keys.filter(revoked_at__isnull=True).filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now))
        return paged(request, keys, lambda key: ApiKeySerializer(key).data)


class ApiKeyRevokeView(PlatformView):
    """POST /api/v1/platform/api-keys/<id>/revoke/"""

    def post(self, request, key_id):
        key = ApiKey.objects.select_related("store", "created_by").filter(pk=key_id).first()
        if key is None:
            raise NotFound("API key not found.")
        if not key.revoked_at:
            key.revoked_at = timezone.now()
            key.save(update_fields=["revoked_at", "updated_at"])
            note(request, "api_key_revoked", store=key.store, object_id=key.pk, object_repr=key.name, prefix=key.prefix)
        return Response(ApiKeySerializer(key).data)


# ---------------------------------------------------------------- feature flags

def get_flag(key):
    flag = FeatureFlag.objects.filter(key=key).first()
    if flag is None:
        raise NotFound("Feature flag not found.")
    return flag


class FeatureFlagListView(PlatformView):
    """
    GET  /api/v1/platform/feature-flags/  every flag, including the ones code knows that have no row yet ("saved": false)
    POST /api/v1/platform/feature-flags/  {key, name, description?, enabled?, rolloutPercent?}
    """

    def get(self, request):
        rows = [{**FeatureFlagSerializer(f).data, "saved": True} for f in FeatureFlag.objects.all()]
        saved = {r["key"] for r in rows}
        rows += [
            {
                "key": key, "name": name, "description": description, "enabled": default, "rolloutPercent": 100,
                "overrideCount": 0, "createdAt": None, "updatedAt": None, "saved": False,
            }
            for key, (name, description, default) in sorted(flags.REGISTRY.items()) if key not in saved
        ]
        return Response(sorted(rows, key=lambda r: r["key"]))

    def post(self, request):
        serializer = FeatureFlagSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        flag = serializer.save()
        note(request, "flag_created", object_id=flag.key, object_repr=flag.name, enabled=flag.enabled)
        return Response({**FeatureFlagSerializer(flag).data, "saved": True}, status=201)


class FeatureFlagDetailView(PlatformView):
    """GET / PATCH / DELETE /api/v1/platform/feature-flags/<key>/"""

    def get(self, request, key):
        return Response({**FeatureFlagSerializer(get_flag(key)).data, "saved": True})

    def patch(self, request, key):
        flag = get_flag(key)
        before = {"enabled": flag.enabled, "rolloutPercent": flag.rollout_percent}
        serializer = FeatureFlagSerializer(flag, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        flag = serializer.save()
        note(
            request, "flag_updated", object_id=flag.key, object_repr=flag.name,
            before=before, after={"enabled": flag.enabled, "rolloutPercent": flag.rollout_percent},
        )
        return Response({**FeatureFlagSerializer(flag).data, "saved": True})

    def delete(self, request, key):
        flag = get_flag(key)
        name = flag.name
        flag.delete()
        note(request, "flag_deleted", object_id=key, object_repr=name)
        return Response(status=204)


class FeatureFlagOverridesView(PlatformView):
    """GET /api/v1/platform/feature-flags/<key>/overrides/  the stores that have their own answer"""

    def get(self, request, key):
        flag = get_flag(key)
        rows = flag.overrides.select_related("store").order_by("store__store_name")
        return Response([
            {
                "storeId": str(o.store_id), "storeName": o.store.store_name, "storeSlug": o.store.store_slug,
                "enabled": o.enabled, "updatedAt": o.updated_at,
            }
            for o in rows
        ])


class FeatureFlagOverrideDetailView(PlatformView):
    """
    PUT    /api/v1/platform/feature-flags/<key>/overrides/<store id>/  {"enabled": true|false}
    DELETE the same: the store follows the flag again
    """

    def put(self, request, key, store_id):
        flag = get_flag(key)
        store = Store.objects.filter(pk=store_id).first()
        if store is None:
            raise NotFound("Store not found.")
        enabled = request.data.get("enabled")
        if not isinstance(enabled, bool):
            raise ValidationError({"enabled": "Send true or false."})
        FeatureFlagOverride.objects.update_or_create(flag=flag, store=store, defaults={"enabled": enabled})
        note(request, "flag_override_set", store=store, object_id=flag.key, object_repr=flag.name, enabled=enabled)
        return Response({"storeId": str(store.pk), "key": flag.key, "enabled": enabled})

    def delete(self, request, key, store_id):
        flag = get_flag(key)
        deleted, _ = FeatureFlagOverride.objects.filter(flag=flag, store_id=store_id).delete()
        if deleted:
            note(request, "flag_override_removed", store=Store.objects.filter(pk=store_id).first(), object_id=flag.key, object_repr=flag.name)
        return Response(status=204)


# ---------------------------------------------------------------- overview and audit

class OverviewView(PlatformView):
    """GET /api/v1/platform/overview/"""

    def get(self, request):
        by_status = Counter(Store.objects.values_list("status", flat=True))
        now = timezone.now()
        week_ago = now - timedelta(days=7)
        active_keys = ApiKey.objects.filter(revoked_at__isnull=True).filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now))
        all_flags = FeatureFlag.objects.all()
        recent = annotated_stores().order_by("-created_at")[:5]
        actions = AuditLog.objects.filter(action__startswith="platform.").select_related("actor")[:10]
        return Response({
            "stores": {
                "total": sum(by_status.values()), "active": by_status.get(ACTIVE, 0),
                "suspended": by_status.get(SUSPENDED, 0), "closed": by_status.get(CLOSED, 0),
                "newThisWeek": Store.objects.filter(created_at__gte=week_ago).count(),
            },
            "users": {
                "total": User.objects.count(), "platformAdmins": User.objects.filter(is_owner=True).count(),
                "newThisWeek": User.objects.filter(created_at__gte=week_ago).count(),
            },
            "apiKeys": {"active": active_keys.count(), "total": ApiKey.objects.count()},
            "featureFlags": {"total": all_flags.count(), "enabled": all_flags.filter(enabled=True).count()},
            "recentStores": [store_json(s) for s in recent],
            "recentActions": [entry_json(e) for e in actions],
        })


class PlatformAuditView(PlatformView):
    """GET /api/v1/platform/audit/  every store's log and account events (?store=<id>, ?platformOnly=true, plus the store filters)"""

    def get(self, request):
        entries = AuditLog.objects.select_related("actor", "store").order_by("-created_at", "-id")
        if store := request.query_params.get("store"):
            entries = entries.filter(store_id=store)
        if request.query_params.get("platformOnly") in ("true", "1"):
            entries = entries.filter(action__startswith="platform.")
        entries = filtered(request, entries)
        return paged(request, entries, lambda e: {**entry_json(e), "store": e.store.store_name if e.store else None})
