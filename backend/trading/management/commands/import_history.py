import json
import signal
import threading

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from trading.history import HistoryPaused, import_history
from trading.locking import single_worker
from trading.markets import MAX_HISTORY_CANDLES


class Command(BaseCommand):
    help = "Import real training candles into PostgreSQL without creating trading experiments"

    def add_arguments(self, parser):
        parser.add_argument("--symbols", nargs="+", default=list(settings.SYMBOLS))
        parser.add_argument("--count", type=int, default=100000)

    def handle(self, *args, **options):
        symbols = list(dict.fromkeys(options["symbols"]))
        if set(symbols)-set(settings.SYMBOLS) or not 50 <= options["count"] <= MAX_HISTORY_CANDLES:
            raise CommandError("Choose configured assets and 50–1,000,000 candles per asset")
        stop = threading.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())
        end_ms = int(timezone.now().timestamp()*1000)-1
        with single_worker("history_import", stop):
            for symbol in symbols:
                self.stdout.write(f"Importing {options['count']:,} completed candles for {symbol}")
                def checkpoint(progress):
                    if progress["candles"] % 10000 == 0:
                        self.stdout.write(f"{symbol}: {progress['candles']:,}/{progress['requested']:,} processed")
                try:
                    result = import_history(symbol, options["count"], end_ms=end_ms, save_checkpoint=checkpoint, stop=stop)
                except HistoryPaused:
                    self.stdout.write("Import interrupted. Committed pages are retained; rerunning is safe.")
                    return
                self.stdout.write(json.dumps(result))
