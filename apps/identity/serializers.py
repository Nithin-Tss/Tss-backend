from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError
from rest_framework import serializers

from .email_links import user_for_email_verification, user_for_password_reset

User = get_user_model()


class SignupSerializer(serializers.Serializer):
    firstName = serializers.CharField(
        required=True,
        allow_blank=False
    )

    lastName = serializers.CharField(
        required=True,
        allow_blank=False
    )

    email = serializers.EmailField(
        required=True
    )

    password = serializers.CharField(
        required=True,
        write_only=True,
        min_length=8
    )

    confirmPassword = serializers.CharField(
        required=True,
        write_only=True
    )

    mobileNumber = serializers.CharField(
        required=True,
        allow_blank=False
    )

    agreeTerms = serializers.BooleanField(
        required=True
    )

    def validate_firstName(self, value):
        value = value.strip()

        if not value:
            raise serializers.ValidationError(
                "First name is required."
            )

        return value

    def validate_lastName(self, value):
        value = value.strip()

        if not value:
            raise serializers.ValidationError(
                "Last name is required."
            )

        return value

    def validate_email(self, value):
        value = value.strip().lower()

        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError(
                "An account with this email already exists."
            )

        return value

    def validate_mobileNumber(self, value):
        value = value.strip()

        if not value.isdigit() or len(value) != 10:
            raise serializers.ValidationError(
                "Please enter a valid 10-digit mobile number."
            )

        return value

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
    email = serializers.EmailField(
        required=True
    )

    password = serializers.CharField(
        required=True,
        write_only=True,
        allow_blank=False
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


class ChangePasswordSerializer(serializers.Serializer):
    currentPassword = serializers.CharField(
        required=True,
        write_only=True
    )

    newPassword = serializers.CharField(
        required=True,
        write_only=True,
        min_length=8
    )

    confirmPassword = serializers.CharField(
        required=True,
        write_only=True
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
    email = serializers.EmailField(
        required=True
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
        min_length=8
    )

    confirmPassword = serializers.CharField(
        required=True,
        write_only=True
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
