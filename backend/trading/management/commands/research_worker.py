import signal
import threading
import time

from django.core.management.base import BaseCommand

from trading.locking import single_worker
from trading.recording import heartbeat
from trading.research import recover_jobs, work_one_job
from trading.autonomy import schedule_cycle


class Command(BaseCommand):
    help = "Run history imports and backtests independently from order processing"

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")

    def handle(self, *args, **options):
        stop = threading.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())
        with single_worker("research", stop) as check:
            recover_jobs()
            try:
                last_schedule = -60
                while not stop.is_set():
                    check()
                    if time.monotonic()-last_schedule >= 60:
                        schedule_cycle()
                        last_schedule = time.monotonic()
                    heartbeat("research", "running", "Ready for historical research jobs")
                    worked = work_one_job(stop)
                    if options["once"]:
                        break
                    if not worked:
                        stop.wait(2)
            finally:
                heartbeat("research", "stopped")
