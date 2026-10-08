"""Customizer values (colors, fonts, button radius) stored in Theme.theme_data["tokens"]."""

# Flat customizer key -> (group, name) inside theme_data["tokens"].
SETTING_PATHS = {
    "primary": ("colors", "primary"),
    "accent": ("colors", "accent"),
    "background": ("colors", "background"),
    "heading_font": ("typography", "heading_font"),
    "body_font": ("typography", "body_font"),
    "button_radius": ("buttons", "radius"),
}

# Values end up in CSS variables, so keep them to plain CSS values.
_UNSAFE = set(";{}<>\"'\\\n\r")


def read_settings(theme):
    tokens = (theme.theme_data or {}).get("tokens", {})
    return {
        key: tokens[group][name]
        for key, (group, name) in SETTING_PATHS.items()
        if name in tokens.get(group, {})
    }


def validate_settings(data):
    """Return (clean_values, errors) for a flat customizer payload."""
    if not isinstance(data, dict):
        return {}, {"detail": "Expected a JSON object."}
    clean, errors = {}, {}
    for key, value in data.items():
        if key not in SETTING_PATHS:
            errors[key] = "Unknown setting."
        elif not isinstance(value, str) or not value.strip() or len(value) > 100:
            errors[key] = "Must be a non-empty string up to 100 characters."
        elif _UNSAFE & set(value):
            errors[key] = "Contains invalid characters."
        else:
            clean[key] = value.strip()
    return clean, errors


def save_settings(theme, clean):
    theme_data = dict(theme.theme_data or {})
    tokens = theme_data.setdefault("tokens", {})
    for key, value in clean.items():
        group, name = SETTING_PATHS[key]
        tokens.setdefault(group, {})[name] = value
    theme.theme_data = theme_data
    theme.save(update_fields=["theme_data", "updated_at"])
