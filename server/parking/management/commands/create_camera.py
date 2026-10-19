# server/parking/management/commands/create_camera.py
"""Register a camera and print its device token once.

    TOKEN=$(python manage.py create_camera --org labim --lot "Labim Mall" \
        --name "North deck" --source-uri file:///data/demo.mp4 \
        --frame 1280x720 --model-version mnv3s-pklot-20260921)

The lot is created inside the organization if it does not exist. Only the
token goes to stdout, so it can be captured by a script; everything else goes
to stderr. Only its SHA-256 is stored — a lost token is rotated with
POST /api/v1/cameras/{id}/token:rotate as an OWNER.
"""
from __future__ import annotations

import re

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from accounts.models import Organization
from parking.models import Camera, ParkingLot
from parking.tokens import generate_device_token
from config.errors import ApiError
from parking.worker_config import load_model_card

FRAME = re.compile(r"(\d+)x(\d+)")


def parse_frame(value: str) -> tuple[int, int]:
    match = FRAME.fullmatch(value)
    if not match or int(match[1]) == 0 or int(match[2]) == 0:
        raise CommandError(f"--frame must look like 1280x720, got {value!r}")
    return int(match[1]), int(match[2])


class Command(BaseCommand):
    help = "Register a camera and print its device token once."

    def add_arguments(self, parser):
        parser.add_argument("--org", required=True, help="organization slug")
        parser.add_argument("--lot", required=True, help="lot name; created if missing")
        parser.add_argument("--name", required=True)
        parser.add_argument("--source-uri", required=True)
        parser.add_argument("--frame", required=True,
                            help="reference frame the polygons are drawn on, WxH")
        parser.add_argument("--model-version", required=True)

    def handle(self, *args, **options):
        org = Organization.objects.filter(slug=options["org"]).first()
        if org is None:
            raise CommandError(f"No organization with slug {options['org']!r}.")
        try:
            load_model_card(options["model_version"])
        except ApiError as exc:
            raise CommandError(
                f"No model card for {options['model_version']!r}: "
                f"{exc.detail['reason']}. Put the card in MODEL_CARDS_DIR first."
            ) from exc
        width, height = parse_frame(options["frame"])
        token, digest = generate_device_token()

        with transaction.atomic():
            lot, lot_created = (ParkingLot.objects.for_org(org)
                                .get_or_create(name=options["lot"]))
            camera = Camera.objects.for_org(org).create(
                lot=lot,
                name=options["name"],
                source_uri=options["source_uri"],
                device_token_hash=digest,
                frame_width=width,
                frame_height=height,
                model_version=options["model_version"],
            )

        note = self.style.SUCCESS
        self.stderr.write(f"{'Created' if lot_created else 'Using'} lot "
                          f"{lot.name!r} ({lot.pk})", style_func=note)
        self.stderr.write(f"Created camera {camera.name!r} ({camera.pk})", style_func=note)
        self.stderr.write("Device token on stdout. Shown once; not stored.",
                          style_func=self.style.WARNING)
        self.stdout.write(token)