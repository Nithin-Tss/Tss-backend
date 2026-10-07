import uuid

from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.db import models


class UserManager(BaseUserManager):
    def create_user(self, email, password=None, **extra_fields):
        if not email:
            raise ValueError("Email is required.")

        email = self.normalize_email(email)

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
    def is_active(self):
        return True

    def has_perm(self, perm, obj=None):
        return self.is_owner

    def has_module_perms(self, app_label):
        return self.is_owner

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