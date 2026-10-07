from rest_framework import serializers


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
        return value.strip().lower()

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

        return data


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