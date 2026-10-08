def generate_css_variables(theme_data):
    tokens = theme_data.get("tokens", {})

    colors = tokens.get("colors", {})
    typography = tokens.get("typography", {})
    buttons = tokens.get("buttons", {})

    css_variables = []

    # Colors
    for name, value in colors.items():
        css_variables.append(f"--color-{name}: {value};")

    # Typography
    if typography.get("heading_font"):
        css_variables.append(
            f"--font-heading: {typography['heading_font']};"
        )

    if typography.get("body_font"):
        css_variables.append(
            f"--font-body: {typography['body_font']};"
        )

    # Buttons
    if buttons.get("radius"):
        css_variables.append(
            f"--button-radius: {buttons['radius']};"
        )

    return ":root {\n    " + "\n    ".join(css_variables) + "\n}" 