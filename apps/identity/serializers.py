import re

from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError
from rest_framework import serializers

from .email_links import user_for_email_verification, user_for_password_reset

User = get_user_model()

# Same rule as the signup form: English letters and spaces only
NAME_PATTERN = re.compile(r"[A-Za-z ]+")


class ProfileSerializer(serializers.Serializer):
    """
    Name, email and mobile number: filled in at signup and changed later on
    the profile page (PATCH /api/v1/auth/me/). Same messages as those forms.
    """

    firstName = serializers.CharField(
        required=True,
        allow_blank=False,
        error_messages={
            "required": "First name is required.",
            "blank": "First name is required.",
        }
    )

    lastName = serializers.CharField(
        required=True,
        allow_blank=False,
        error_messages={
            "required": "Last name is required.",
            "blank": "Last name is required.",
        }
    )

    email = serializers.EmailField(
        required=True,
        error_messages={
            "required": "Email is required.",
            "blank": "Email is required.",
            "invalid": "Please enter a valid email address.",
        }
    )

    mobileNumber = serializers.CharField(
        required=True,
        allow_blank=False,
        error_messages={
            "required": "Mobile number is required.",
            "blank": "Mobile number is required.",
        }
    )

    def validate_firstName(self, value):
        value = value.strip()

        if not value:
            raise serializers.ValidationError(
                "First name is required."
            )

        if not NAME_PATTERN.fullmatch(value):
            raise serializers.ValidationError(
                "First name can contain only letters and spaces."
            )

        return value

    def validate_lastName(self, value):
        value = value.strip()

        if not value:
            raise serializers.ValidationError(
                "Last name is required."
            )

        if not NAME_PATTERN.fullmatch(value):
            raise serializers.ValidationError(
                "Last name can contain only letters and spaces."
            )

        return value

    def validate_email(self, value):
        value = value.strip().lower()
        others = User.objects.filter(email__iexact=value)

        if self.instance is not None:  # editing a profile: its own email is fine
            others = others.exclude(pk=self.instance.pk)

        if others.exists():
            raise serializers.ValidationError(
                "An account with this email already exists."
            )

        return value

    def validate_mobileNumber(self, value):
        value = value.strip()

        if not (value.isascii() and value.isdigit()) or len(value) != 10:
            raise serializers.ValidationError(
                "Please enter a valid 10-digit mobile number."
            )

        return value

    def update(self, user, validated_data):
        # A new address has to be verified again
        if validated_data.get("email", user.email) != user.email:
            user.email_verified_at = None

        fields = {
            "firstName": "first_name",
            "lastName": "last_name",
            "email": "email",
            "mobileNumber": "phone",
        }

        for key, attribute in fields.items():
            if key in validated_data:
                setattr(user, attribute, validated_data[key])

        try:
            user.save()
        except IntegrityError:
            # Another account took this email at the same moment.
            raise serializers.ValidationError({
                "email": "An account with this email already exists."
            })

        return user


class SignupSerializer(ProfileSerializer):
    password = serializers.CharField(
        required=True,
        write_only=True,
        min_length=8,
        error_messages={
            "required": "Password is required.",
            "blank": "Password is required.",
            "min_length": "Password must contain at least 8 characters.",
        }
    )

    confirmPassword = serializers.CharField(
        required=True,
        write_only=True,
        error_messages={
            "required": "Please confirm your password.",
            "blank": "Please confirm your password.",
        }
    )

    agreeTerms = serializers.BooleanField(
        required=True,
        error_messages={
            "required": "You must agree to the Terms & Conditions.",
            "invalid": "You must agree to the Terms & Conditions.",
        }
    )

    def validate_agreeTerms(self, value):
        if value is not True:
            raise serializers.ValidationError(
                "You must agree to the Terms & Conditions."
            )

        return value

    def validate(self, data):
        if data["password"] != data["confirmPassword"]:
            raise serializers.ValidationError({
                "confirmPassword": "Passwords do not match."
            })

        candidate = User(
            email=data["email"],
            first_name=data["firstName"],
            last_name=data["lastName"],
        )

        try:
            validate_password(data["password"], user=candidate)
        except DjangoValidationError as error:
            raise serializers.ValidationError({
                "password": error.messages
            })

        return data

    def create(self, validated_data):
        try:
            return User.objects.create_user(
                email=validated_data["email"],
                password=validated_data["password"],
                first_name=validated_data["firstName"],
                last_name=validated_data["lastName"],
                phone=validated_data["mobileNumber"],
            )
        except IntegrityError:
            # Two signups with the same email at the same moment.
            raise serializers.ValidationError({
                "email": "An account with this email already exists."
            })


class LoginSerializer(serializers.Serializer):
    # Same messages as the sign-in form
    email = serializers.EmailField(
        required=True,
        error_messages={
            "required": "Email is required.",
            "blank": "Email is required.",
            "invalid": "Please enter a valid email address.",
        }
    )

    password = serializers.CharField(
        required=True,
        write_only=True,
        allow_blank=False,
        error_messages={
            "required": "Password is required.",
            "blank": "Password is required.",
        }
    )

    rememberMe = serializers.BooleanField(
        required=False,
        default=False
    )

    def validate_email(self, value):
        return value.strip().lower()

    def validate(self, data):
        user = authenticate(
            self.context.get("request"),
            username=data["email"],
            password=data["password"],
        )

        if user is None:
            raise serializers.ValidationError(
                "Invalid email or password."
            )

        data["user"] = user
        return data


NEW_PASSWORD_MESSAGES = {
    "required": "New password is required.",
    "blank": "New password is required.",
    "min_length": "Password must contain at least 8 characters.",
}

CONFIRM_NEW_PASSWORD_MESSAGES = {
    "required": "Please confirm your new password.",
    "blank": "Please confirm your new password.",
}


class ChangePasswordSerializer(serializers.Serializer):
    # Same messages as the change password form
    currentPassword = serializers.CharField(
        required=True,
        write_only=True,
        error_messages={
            "required": "Current password is required.",
            "blank": "Current password is required.",
        }
    )

    newPassword = serializers.CharField(
        required=True,
        write_only=True,
        min_length=8,
        error_messages=NEW_PASSWORD_MESSAGES
    )

    confirmPassword = serializers.CharField(
        required=True,
        write_only=True,
        error_messages=CONFIRM_NEW_PASSWORD_MESSAGES
    )

    def validate_currentPassword(self, value):
        if not self.context["request"].user.check_password(value):
            raise serializers.ValidationError(
                "Current password is incorrect."
            )

        return value

    def validate(self, data):
        if data["newPassword"] != data["confirmPassword"]:
            raise serializers.ValidationError({
                "confirmPassword": "Passwords do not match."
            })

        try:
            validate_password(
                data["newPassword"],
                user=self.context["request"].user,
            )
        except DjangoValidationError as error:
            raise serializers.ValidationError({
                "newPassword": error.messages
            })

        return data


class PasswordResetRequestSerializer(serializers.Serializer):
    # Same messages as the forgot password form
    email = serializers.EmailField(
        required=True,
        error_messages={
            "required": "Email is required.",
            "blank": "Email is required.",
            "invalid": "Please enter a valid email address.",
        }
    )

    def validate_email(self, value):
        return value.strip().lower()


class PasswordResetLinkSerializer(serializers.Serializer):
    """The uid and token from a password reset link."""

    uid = serializers.CharField(
        required=True
    )

    token = serializers.CharField(
        required=True
    )

    def validate(self, data):
        user = user_for_password_reset(data["uid"], data["token"])

        if user is None:
            raise serializers.ValidationError({
                "token": "This reset link is invalid or has expired. Please request a new one."
            })

        data["user"] = user
        return data


class PasswordResetConfirmSerializer(PasswordResetLinkSerializer):
    newPassword = serializers.CharField(
        required=True,
        write_only=True,
        min_length=8,
        error_messages=NEW_PASSWORD_MESSAGES
    )

    confirmPassword = serializers.CharField(
        required=True,
        write_only=True,
        error_messages=CONFIRM_NEW_PASSWORD_MESSAGES
    )

    def validate(self, data):
        data = super().validate(data)

        if data["newPassword"] != data["confirmPassword"]:
            raise serializers.ValidationError({
                "confirmPassword": "Passwords do not match."
            })

        try:
            validate_password(data["newPassword"], user=data["user"])
        except DjangoValidationError as error:
            raise serializers.ValidationError({
                "newPassword": error.messages
            })

        return data


class VerifyEmailSerializer(serializers.Serializer):
    token = serializers.CharField(
        required=True
    )

    def validate(self, data):
        user = user_for_email_verification(data["token"])

        if user is None:
            raise serializers.ValidationError({
                "token": "This verification link is invalid or has expired. Please request a new one."
            })

        data["user"] = user
        return data
