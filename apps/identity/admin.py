from django import forms
from django.contrib import admin
from django.contrib.admin.forms import AdminAuthenticationForm
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.forms import AdminUserCreationForm, UserChangeForm

from . import lockout
from .models import User, normalize_email


class LockoutAdminAuthenticationForm(AdminAuthenticationForm):
    """/admin/login/ shares the per-email sign-in lockout with the API."""

    def clean(self):
        email = (self.cleaned_data.get("username") or "").strip().lower()
        if email and lockout.is_locked(email):
            raise forms.ValidationError(
                "Too many failed sign-in attempts. Try again later or reset your password.",
                code="locked",
            )
        try:
            cleaned = super().clean()
        except forms.ValidationError:
            if email:
                lockout.record_failure(email)
            raise
        if email:
            lockout.clear(email)
        return cleaned


admin.site.login_form = LockoutAdminAuthenticationForm


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
    list_filter = ("is_owner", "is_support_agent")
    search_fields = ("email", "first_name", "last_name", "phone")
    ordering = ("-created_at",)
    filter_horizontal = ()
    readonly_fields = ("last_login", "created_at", "updated_at")

    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Profile", {"fields": ("first_name", "last_name", "phone", "email_verified_at")}),
        ("Access", {"fields": ("is_owner", "is_support_agent")}),
        ("Dates", {"fields": ("last_login", "created_at", "updated_at")}),
    )
    add_fieldsets = (
        (None, {
            "classes": ("wide",),
            "fields": ("email", "first_name", "last_name", "usable_password", "password1", "password2"),
        }),
    )
