import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from rest_framework import serializers

from apps.catalog.models import EXTRA_FIELDS

from .access import member_roles, membership_permissions
from .models import Store
from .services import platform_membership

UNIT_SYSTEMS = ["metric", "imperial"]
WEIGHT_UNITS = ["kg", "g", "lb", "oz"]
AUTO_FULFILL = ["all", "gift_cards", "none"]


class StoreSerializer(serializers.ModelSerializer):
    storeId = serializers.UUIDField(source="store_id", read_only=True)
    storeName = serializers.CharField(source="store_name", max_length=200)
    storeSlug = serializers.SlugField(
        source="store_slug",
        max_length=100,
        required=False,
    )
    createdAt = serializers.DateTimeField(source="created_at", read_only=True)
    fieldLabels = serializers.JSONField(source="field_labels", required=False)
    contactEmail = serializers.EmailField(source="contact_email", required=False, allow_blank=True, max_length=254)
    phone = serializers.CharField(required=False, allow_blank=True, max_length=30)
    address = serializers.CharField(required=False, allow_blank=True, max_length=500)
    currency = serializers.CharField(required=False, max_length=3)
    backupRegion = serializers.CharField(source="backup_region", required=False, allow_blank=True, max_length=60)
    unitSystem = serializers.ChoiceField(source="unit_system", choices=UNIT_SYSTEMS, required=False)
    weightUnit = serializers.ChoiceField(source="weight_unit", choices=WEIGHT_UNITS, required=False)
    timezone = serializers.CharField(required=False, max_length=64)
    orderPrefix = serializers.CharField(source="order_prefix", required=False, allow_blank=True, max_length=10)
    orderSuffix = serializers.CharField(source="order_suffix", required=False, allow_blank=True, max_length=10)
    autoFulfill = serializers.ChoiceField(source="auto_fulfill", choices=AUTO_FULFILL, required=False)
    autoArchive = serializers.BooleanField(source="auto_archive", required=False)
    role = serializers.SerializerMethodField()
    roles = serializers.SerializerMethodField()
    permissions = serializers.SerializerMethodField()

    class Meta:
        model = Store
        fields = [
            "storeId", "storeName", "storeSlug", "status", "createdAt", "fieldLabels",
            "contactEmail", "phone", "address", "currency", "backupRegion", "unitSystem",
            "weightUnit", "timezone", "orderPrefix", "orderSuffix", "autoFulfill", "autoArchive",
            "role", "roles", "permissions",
        ]
        read_only_fields = ["status"]

    def validate_currency(self, value):
        value = value.strip().upper()
        if not re.fullmatch(r"[A-Z]{3}", value):
            raise serializers.ValidationError("Use a 3-letter currency code, e.g. INR.")
        return value

    def validate_timezone(self, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise serializers.ValidationError("Unknown time zone.")
        return value

    def validate_phone(self, value):
        value = value.strip()
        if value and not re.fullmatch(r"[0-9+()\-.\s]{5,30}", value):
            raise serializers.ValidationError("Enter a valid phone number.")
        return value

    def validate_address(self, value):
        return " ".join(value.split())

    def validate_orderPrefix(self, value):
        return self._order_affix(value)

    def validate_orderSuffix(self, value):
        return self._order_affix(value)

    @staticmethod
    def _order_affix(value):
        # Order numbers are read back by their trailing digits, so an affix can't hold digits
        value = value.strip()
        if any(c.isdigit() for c in value):
            raise serializers.ValidationError("Can't contain numbers.")
        return value

    def _membership(self, store):
        request = self.context.get("request")
        if request is None or not request.user.is_authenticated:
            return None
        membership = store.memberships.filter(user=request.user, status="active").first()
        if membership is None and request.user.is_owner:
            membership = platform_membership(request.user, store)
        return membership

    def get_role(self, store):
        """The signed-in user's role in this store ("owner", "staff", ...)."""
        membership = self._membership(store)
        return membership.role if membership else None

    def get_roles(self, store):
        """Names of the roles they hold (owners hold none: they skip the role check)."""
        membership = self._membership(store)
        return [r.name for r in member_roles(membership)] if membership else []

    def get_permissions(self, store):
        membership = self._membership(store)
        return sorted(membership_permissions(membership)) if membership else []

    def validate_fieldLabels(self, value):
        """Only the generic "columnN" fields can be renamed; blank resets to the default."""
        if not isinstance(value, dict):
            raise serializers.ValidationError("Field labels must be an object.")

        allowed = [f for f in EXTRA_FIELDS if f.startswith("column")]
        cleaned = {}
        for key, label in value.items():
            if key not in allowed:
                raise serializers.ValidationError(f"{key} can't be renamed.")
            if not isinstance(label, str):
                raise serializers.ValidationError("Each label must be text.")
            label = " ".join(label.split())
            if len(label) > 60:
                raise serializers.ValidationError("A field name can be at most 60 characters.")
            if label:
                cleaned[key] = label

        if len({v.lower() for v in cleaned.values()}) != len(cleaned):
            raise serializers.ValidationError("Each field needs a different name.")

        return cleaned

    def validate_storeName(self, value):
        value = value.strip()

        if not value:
            raise serializers.ValidationError("Store name is required.")

        return value

    def validate_storeSlug(self, value):
        value = value.lower()

        taken = Store.objects.filter(store_slug=value)
        if self.instance is not None:
            taken = taken.exclude(pk=self.instance.pk)

        if taken.exists():
            raise serializers.ValidationError("This store URL is already taken.")

        return value
