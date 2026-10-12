import re

from django.core.exceptions import ValidationError


class CharacterMixValidator:
    """
    The password needs an uppercase letter, a lowercase letter, a number and
    a special character: the same checklist the signup page shows.
    """

    rules = [
        (r"[A-Z]", "password_no_upper", "Include at least one uppercase letter."),
        (r"[a-z]", "password_no_lower", "Include at least one lowercase letter."),
        (r"[0-9]", "password_no_number", "Include at least one number."),
        (r"[^A-Za-z0-9]", "password_no_special", "Include at least one special character."),
    ]

    def validate(self, password, user=None):
        errors = [
            ValidationError(message, code=code)
            for pattern, code, message in self.rules
            if not re.search(pattern, password)
        ]

        if errors:
            raise ValidationError(errors)

    def get_help_text(self):
        return (
            "Your password must contain an uppercase letter, a lowercase letter, "
            "a number and a special character."
        )
