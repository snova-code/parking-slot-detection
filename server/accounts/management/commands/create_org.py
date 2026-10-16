# server/accounts/management/commands/create_org.py
"""Create an organization and its first OWNER.

    python manage.py create_org --slug labim --name "Labim Mall" \
        --owner-phone 9812345678 [--owner-name "Full Name"] [--password-env VAR]

If the phone already belongs to a user, that user joins as OWNER and their
password is untouched. Otherwise the password is read interactively, or from
the environment variable named by --password-env for scripted seeding. It is
never accepted as an argument: argv is visible in `ps` and shell history.
"""
from __future__ import annotations

import os
from getpass import getpass

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.core.validators import validate_slug
from django.db import transaction

from accounts.models import Membership, Organization, Role, User
from accounts.phone import InvalidPhoneNumber, normalize_phone


class Command(BaseCommand):
    help = "Create an organization and its first OWNER."

    def add_arguments(self, parser):
        parser.add_argument("--slug", required=True)
        parser.add_argument("--name", required=True)
        parser.add_argument("--owner-phone", required=True)
        parser.add_argument("--owner-name", default="")
        parser.add_argument("--password-env", metavar="VAR")

    def handle(self, *args, **options):
        slug = options["slug"]
        try:
            validate_slug(slug)
            phone = normalize_phone(options["owner_phone"])
        except (ValidationError, InvalidPhoneNumber) as exc:
            raise CommandError(str(exc)) from exc
        if Organization.objects.filter(slug=slug).exists():
            raise CommandError(f"Organization {slug!r} already exists.")

        user = User.objects.filter(phone_number=phone).first()
        password = None
        if user is None:
            password = self._new_password(options["password_env"], phone,
                                          options["owner_name"])

        with transaction.atomic():
            org = Organization.objects.create(name=options["name"], slug=slug)
            created_user = user is None
            if created_user:
                user = User.objects.create_user(phone, password,
                                                full_name=options["owner_name"])
            Membership.objects.for_org(org).create(user=user, role=Role.OWNER)

        self.stdout.write(self.style.SUCCESS(
            f"Created organization {slug!r} ({org.pk}); "
            f"{'new' if created_user else 'existing'} user {phone} is OWNER."
        ))

    def _new_password(self, env_var: str | None, phone: str, full_name: str) -> str:
        if env_var:
            password = os.environ.get(env_var, "")
            if not password:
                raise CommandError(f"${env_var} is empty or unset.")
        else:
            password = getpass("Owner password: ")
            if password != getpass("Repeat: "):
                raise CommandError("Passwords do not match.")
        try:
            validate_password(password, user=User(phone_number=phone, full_name=full_name))
        except ValidationError as exc:
            raise CommandError(" ".join(exc.messages)) from exc
        return password