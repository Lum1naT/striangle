import signal
import threading

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from trading.live import live_tick
from trading.locking import single_worker
from trading.models import Run
from trading.recording import heartbeat
from trading.services import process_run


class Command(BaseCommand):
    help = "Process forward paper portfolios and explicitly armed live runs"

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")

    def handle(self, *args, **options):
        stop = threading.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())
        with single_worker("trader") as check:
            try:
                while not stop.is_set():
                    check()
                    close_old_connections()
                    for run_id, mode in Run.objects.filter(mode__in=["paper", "live"], status__in=["running", "reconciling"]).values_list("id", "mode"):
                        process_run(run_id)
                        if mode == "live":
                            live_tick(run_id)
                        if stop.is_set():
                            break
                    heartbeat("trader", "running", "Paper comparisons and order reconciliation")
                    if options["once"]:
                        break
                    stop.wait(1)
            finally:
                heartbeat("trader", "stopped")
