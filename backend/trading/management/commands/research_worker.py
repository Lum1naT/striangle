import signal
import threading

from django.core.management.base import BaseCommand
from django.utils import timezone

from trading.locking import single_worker
from trading.models import Job
from trading.recording import heartbeat
from trading.research import work_one_job


class Command(BaseCommand):
    help = "Run history imports and backtests independently from order processing"

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")

    def handle(self, *args, **options):
        stop = threading.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())
        with single_worker("research") as check:
            Job.objects.filter(status="running").update(status="failed", error="Research worker restarted before completion; submit a new job", finished_at=timezone.now())
            try:
                while not stop.is_set():
                    check()
                    heartbeat("research", "running", "Ready for historical research jobs")
                    worked = work_one_job()
                    if options["once"]:
                        break
                    if not worked:
                        stop.wait(2)
            finally:
                heartbeat("research", "stopped")
