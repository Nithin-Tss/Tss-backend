from django import forms
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.forms import AdminUserCreationForm, UserChangeForm

from .models import User, normalize_email

# Registering models
class UniqueEmailMixin:
    """Emails are unique regardless of letter case."""

    def clean_email(self):
        email = normalize_email(self.cleaned_data.get("email"))
        taken = User.objects.filter(email__iexact=email).exclude(pk=self.instance.pk)

        if taken.exists():
            raise forms.ValidationError("A user with this email already exists.")

        return email


class UserCreationForm(UniqueEmailMixin, AdminUserCreationForm):
    class Meta:
        model = User
        fields = ("email", "first_name", "last_name")


class UserEditForm(UniqueEmailMixin, UserChangeForm):
    class Meta:
        model = User
        fields = "__all__"


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    """Django's user admin, adapted to email sign-in and no groups/permissions."""

    form = UserEditForm
    add_form = UserCreationForm

    list_display = ("email", "first_name", "last_name", "is_owner", "last_login", "created_at")
    list_filter = ("is_owner",)
    search_fields = ("email", "first_name", "last_name", "phone")
    ordering = ("-created_at",)
    filter_horizontal = ()
    readonly_fields = ("last_login", "created_at", "updated_at")

    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Profile", {"fields": ("first_name", "last_name", "phone", "email_verified_at")}),
        ("Access", {"fields": ("is_owner",)}),
        ("Dates", {"fields": ("last_login", "created_at", "updated_at")}),
    )
    add_fieldsets = (
        (None, {
            "classes": ("wide",),
            "fields": ("email", "first_name", "last_name", "usable_password", "password1", "password2"),
        }),
    )
