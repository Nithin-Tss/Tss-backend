import re

from rest_framework import serializers

MAX_FILE_SIZE = 1024 * 1024  # 1 MB
PATH_RE = re.compile(r"^[A-Za-z0-9_\-. ]+(/[A-Za-z0-9_\-. ]+)*$")


FOLDER_PLACEHOLDER = ".keep"


def is_allowed_file(path):
    """Theme code is Liquid only. '.keep' marks an otherwise empty folder."""
    name = path.rsplit("/", 1)[-1]
    return name == FOLDER_PLACEHOLDER or (name.endswith(".liquid") and len(name) > len(".liquid"))


def validate_path(value):
    value = value.strip()

    if not PATH_RE.match(value):
        raise serializers.ValidationError(
            "Path may only contain letters, numbers, spaces, '_', '-', '.' and '/' separators."
        )

    if any(part in (".", "..") for part in value.split("/")):
        raise serializers.ValidationError("Path cannot contain '.' or '..' segments.")

    return value


class FilePathField(serializers.CharField):
    def __init__(self, **kwargs):
        kwargs.setdefault("max_length", 255)
        super().__init__(**kwargs)
        self.validators.append(validate_path)

    def to_internal_value(self, data):
        return super().to_internal_value(data).strip()


class ThemeFileWriteSerializer(serializers.Serializer):
    path = FilePathField()
    content = serializers.CharField(allow_blank=True, trim_whitespace=False)

    def validate_path(self, value):
        if not is_allowed_file(value):
            raise serializers.ValidationError("Only .liquid files are allowed.")
        return value

    def validate_content(self, value):
        if len(value.encode("utf-8")) > MAX_FILE_SIZE:
            raise serializers.ValidationError("File is too large (max 1 MB).")
        return value


class ThemeFileRenameSerializer(serializers.Serializer):
    source = FilePathField()
    target = FilePathField()
