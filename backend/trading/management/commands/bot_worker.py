import signal
import threading
import time

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from trading.live import live_tick
from trading.locking import single_worker
from trading.models import Run
from trading.recording import heartbeat
from trading.services import process_run
from trading.auto_paper import tick as autonomy_tick


class Command(BaseCommand):
    help = "Process forward paper portfolios and explicitly armed live runs"

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")

    def handle(self, *args, **options):
        stop = threading.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())
        with single_worker("trader", stop) as check:
            try:
                last_heartbeat, last_autonomy = 0, -1
                while not stop.is_set():
                    check()
                    close_old_connections()
                    backlog = False
                    if time.monotonic()-last_autonomy >= 1:
                        backlog = autonomy_tick()
                        if not backlog:
                            last_autonomy = time.monotonic()
                    for run_id, mode in Run.objects.filter(mode__in=["paper", "live"], status__in=["running", "reconciling"]).values_list("id", "mode"):
                        backlog = process_run(run_id) or backlog
                        if mode == "live":
                            live_tick(run_id)
                        if stop.is_set():
                            break
                    if time.monotonic()-last_heartbeat >= 5:
                        heartbeat("trader", "running", "Continuous paper comparisons and order reconciliation; 250 ms idle polling")
                        last_heartbeat = time.monotonic()
                    if options["once"]:
                        break
                    stop.wait(0 if backlog else 0.25)
            finally:
                heartbeat("trader", "stopped")
