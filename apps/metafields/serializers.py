from rest_framework import serializers

from apps.core.viewsets import StoreScopedSerializerMixin

from .models import (
    VALUE_TYPES, Metafield, MetafieldDefinition, Metaobject, MetaobjectDefinition,
)
from .services import clean_metaobject_values, owner_exists, validate_field_list
from .types import clean_value


class MetafieldDefinitionSerializer(StoreScopedSerializerMixin, serializers.ModelSerializer):
    class Meta:
        model = MetafieldDefinition
        fields = ["id", "owner_type", "namespace", "key", "name", "description", "type", "validations",
                  "created_at", "updated_at"]

    def validate(self, data):
        if self.instance:
            # Changing these would strand the values already saved against the definition.
            for name in ("owner_type", "namespace", "key", "type"):
                if name in data and data[name] != getattr(self.instance, name):
                    raise serializers.ValidationError({name: "Can't be changed once the definition exists."})
        store = self.context["request"].store
        owner_type = data.get("owner_type") or getattr(self.instance, "owner_type", None)
        namespace = data.get("namespace") or getattr(self.instance, "namespace", "custom")
        key = data.get("key") or getattr(self.instance, "key", None)
        clash = MetafieldDefinition.objects.filter(store=store, owner_type=owner_type, namespace=namespace, key=key)
        if self.instance:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise serializers.ValidationError({"key": "A definition with this namespace and key already exists."})
        return data

    def validate_key(self, value):
        return _slug_key(value)

    def validate_namespace(self, value):
        return _slug_key(value)

    def validate_validations(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError("Must be an object.")
        return value


def _slug_key(value):
    import re

    if not re.fullmatch(r"[a-z0-9_]+", value):
        raise serializers.ValidationError("Use lowercase letters, numbers and underscores only.")
    return value


class MetafieldSerializer(StoreScopedSerializerMixin, serializers.ModelSerializer):
    namespace = serializers.CharField(source="definition.namespace", read_only=True)
    key = serializers.CharField(source="definition.key", read_only=True)
    type = serializers.CharField(source="definition.type", read_only=True)
    owner_type = serializers.CharField(source="definition.owner_type", read_only=True)

    class Meta:
        model = Metafield
        fields = ["id", "definition", "owner_type", "owner_id", "namespace", "key", "type", "value",
                  "created_at", "updated_at"]
        validators = []  # a second POST for the same definition and owner replaces the value (see the view)

    def validate(self, data):
        store = self.context["request"].store
        definition = data.get("definition") or self.instance.definition
        owner_id = data.get("owner_id") or self.instance.owner_id
        if self.instance and (
            definition.pk != self.instance.definition_id or owner_id != self.instance.owner_id
        ):
            raise serializers.ValidationError("The definition and owner of a saved value can't be changed.")
        if not owner_exists(store, definition.owner_type, owner_id):
            raise serializers.ValidationError({"owner_id": f"No such {definition.owner_type} in this store."})
        if "value" in data and data["value"] is not None:
            try:
                data["value"] = clean_value(definition.type, data["value"], definition.validations, store)
            except serializers.ValidationError as e:
                raise serializers.ValidationError({"value": e.detail})
        return data


class MetaobjectDefinitionSerializer(StoreScopedSerializerMixin, serializers.ModelSerializer):
    entry_count = serializers.IntegerField(source="entries.count", read_only=True)

    class Meta:
        model = MetaobjectDefinition
        fields = ["id", "type", "name", "description", "fields", "entry_count", "created_at", "updated_at"]

    def validate_fields(self, value):
        return validate_field_list(value)

    def validate(self, data):
        store = self.context["request"].store
        if self.instance and "type" in data and data["type"] != self.instance.type:
            raise serializers.ValidationError({"type": "Can't be changed once the definition exists."})
        if not self.instance and MetaobjectDefinition.objects.filter(store=store, type=data.get("type")).exists():
            raise serializers.ValidationError({"type": "A metaobject type with this name already exists."})
        return data


class MetaobjectSerializer(StoreScopedSerializerMixin, serializers.ModelSerializer):
    type = serializers.CharField(source="definition.type", read_only=True)

    class Meta:
        model = Metaobject
        fields = ["id", "definition", "type", "handle", "status", "values", "created_at", "updated_at"]

    def validate(self, data):
        store = self.context["request"].store
        definition = data.get("definition") or self.instance.definition
        if self.instance and definition.pk != self.instance.definition_id:
            raise serializers.ValidationError({"definition": "Can't be changed once the entry exists."})
        handle = data.get("handle") or getattr(self.instance, "handle", None)
        clash = Metaobject.objects.filter(definition=definition, handle=handle)
        if self.instance:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise serializers.ValidationError({"handle": "An entry with this handle already exists."})
        if "values" in data:
            data["values"] = clean_metaobject_values(definition, data["values"], store)
        return data
