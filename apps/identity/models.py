import uuid

from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.db import models


def normalize_email(email):
    """Emails are stored lower-case, so sign-in is case-insensitive."""
    return (email or "").strip().lower()


class UserManager(BaseUserManager):
    def get_by_natural_key(self, username):
        # Used by sign-in (API and /admin/): any letter case finds the account.
        return self.get(email__iexact=normalize_email(username))

    def create_user(self, email, password=None, **extra_fields):
        email = normalize_email(email)

        if not email:
            raise ValueError("Email is required.")

        user = self.model(
            email=email,
            **extra_fields,
        )

        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()

        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        if not password:
            raise ValueError("Superuser password is required.")

        user = self.create_user(
            email=email,
            password=password,
            **extra_fields,
        )

        user.is_owner = True
        user.save(using=self._db)

        return user


class User(AbstractBaseUser):
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    email = models.EmailField(
        max_length=320,
        unique=True,
    )

    first_name = models.CharField(
        max_length=100,
    )

    last_name = models.CharField(
        max_length=100,
    )

    phone = models.CharField(
        max_length=30,
        null=True,
        blank=True,
    )

    # Platform admin: may use the Django admin site (/admin/).
    is_owner = models.BooleanField(
        default=False,
    )

    password = models.TextField(db_column="password_hash")

    # Django's built-in last_login field is physically stored as last_login_at.
    last_login = models.DateTimeField(
        null=True,
        blank=True,
        db_column="last_login_at",
    )

    email_verified_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    updated_at = models.DateTimeField(
        auto_now=True,
    )

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = [
        "first_name",
        "last_name",
    ]

    @property
    def is_staff(self):
        return self.is_owner

    @property
    def is_superuser(self):
        return self.is_owner

    @property
    def is_active(self):
        # No such column in identity.users: every account can sign in.
        return True

    def has_perm(self, perm, obj=None):
        return self.is_owner

    def has_perms(self, perm_list, obj=None):
        return all(self.has_perm(perm, obj) for perm in perm_list)

    def has_module_perms(self, app_label):
        return self.is_owner

    def clean(self):
        super().clean()
        self.email = normalize_email(self.email)

    def get_full_name(self):
        return f"{self.first_name} {self.last_name}".strip()

    def get_short_name(self):
        return self.first_name

    def __str__(self):
        return self.email

    class Meta:
        db_table = '"identity"."users"'
        verbose_name = "User"
        verbose_name_plural = "Users"