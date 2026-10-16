# server/accounts/models.py
"""Organizations, users and memberships.

User is global: one person can belong to several organizations. Role lives on
Membership, never on User, so it is always relative to an organization.

Removed from 03_DATA_MODEL.md's User: `is_staff`. With no Django admin
installed it has no consumer, and a platform-wide staff flag is a de facto third
role — which the data model forbids.
"""
from __future__ import annotations

import uuid

from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.db import models
from django.db.models import Q

from tenancy.scoping import TenantModel

# E.164: '+', country code, subscriber number, 8-15 digits total. Enforced in the
# database so '9812345678' and '+9779812345678' cannot become two accounts for
# one person. The API normalises input to this form before it gets here.
E164_PATTERN = r"^\+[1-9][0-9]{7,14}$"


class Organization(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=64, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "organization"

    def __str__(self) -> str:
        return self.slug


class UserManager(BaseUserManager):
    use_in_migrations = True

    def create_user(self, phone_number: str, password: str | None = None, **extra):
        if not phone_number:
            raise ValueError("phone_number is required")
        user = self.model(phone_number=phone_number, **extra)
        user.set_password(password)   # None produces an unusable password
        user.save(using=self._db)
        return user


class User(AbstractBaseUser):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    phone_number = models.CharField(max_length=20, unique=True)
    email = models.EmailField(max_length=255, unique=True, null=True, blank=True)
    full_name = models.CharField(max_length=255, blank=True, default="")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    USERNAME_FIELD = "phone_number"
    REQUIRED_FIELDS: list[str] = []

    objects = UserManager()

    class Meta:
        db_table = "app_user"   # "user" is a reserved word in Postgres
        constraints = [
            models.CheckConstraint(condition=Q(phone_number__regex=E164_PATTERN),
                                   name="app_user_phone_e164"),
        ]

    def __str__(self) -> str:
        return self.phone_number


class Role(models.TextChoices):
    OWNER = "OWNER"
    OPERATOR = "OPERATOR"


class Membership(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="memberships")
    role = models.CharField(max_length=16, choices=Role)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "membership"
        constraints = [
            models.UniqueConstraint(fields=["org", "user"],
                                    name="membership_unique_user_per_org"),
            # TextChoices alone is Python-only; this makes "exactly two roles"
            # true at the database.
            models.CheckConstraint(condition=Q(role__in=Role.values),
                                   name="membership_role_valid"),
        ]