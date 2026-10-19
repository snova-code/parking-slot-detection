# server/parking/management/commands/sweep_cameras.py
"""Mark cameras stale when their heartbeats lapse.

    python manage.py sweep_cameras            one pass, then exit
    python manage.py sweep_cameras --loop     until SIGINT/SIGTERM

The loop is the deployment form: no host cron on the VPS, and the interval
lives with the code that uses it. It becomes its own compose service in
Phase 5, once there is a server image to run it in.
"""
from __future__ import annotations

import signal
import time

from django.conf import settings
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Declare cameras stale and set their slots to UNKNOWN."

    def add_arguments(self, parser):
        parser.add_argument("--loop", action="store_true")
        parser.add_argument("--interval", type=float,
                            default=settings.SWEEP_INTERVAL_SECONDS)

    def handle(self, *args, **options):
        from parking.sweep import sweep_stale_cameras

        self.running = True

        def stop(signum, frame):
            self.running = False
            self.stderr.write("shutting down", style_func=self.style.WARNING)

        if options["loop"]:
            signal.signal(signal.SIGTERM, stop)
            signal.signal(signal.SIGINT, stop)

        while True:
            for entry in sweep_stale_cameras():
                self.stdout.write(
                    f"stale camera {entry['camera_id']} in {entry['lot']!r}: "
                    f"last seen {entry['since']:%Y-%m-%d %H:%M:%S%z}, "
                    f"{entry['slots']} slots set to UNKNOWN")
            if not options["loop"] or not self.running:
                return
            time.sleep(options["interval"])