"""Page-sized, durable historical imports and restart checkpoints."""
from datetime import datetime, timezone as dt_timezone

from django.db import transaction

from .markets import ASSETS, MAX_HISTORY_CANDLES
from .models import Candle
from .providers import candle_batches


class HistoryPaused(Exception):
    pass


def import_history(symbol, count, *, end_ms=None, checkpoint=None, save_checkpoint=None, stop=None):
    if symbol not in ASSETS or type(count) is not int or not 50 <= count <= MAX_HISTORY_CANDLES:
        raise ValueError("History must contain 50–1,000,000 candles for a supported asset")
    progress = dict(checkpoint or {})
    progress.update(symbol=symbol, requested=count, provider="Binance spot", interval="1m")
    progress.setdefault("candles", 0)
    progress.setdefault("missing_minutes", 0)
    if progress["candles"] >= count:
        return progress
    cursor = progress.get("next_end_ms", end_ms)
    if stop is not None and stop.is_set():
        raise HistoryPaused()
    for batch, cursor in candle_batches(symbol, count-progress["candles"], end_ms=cursor):
        # Data and cursor commit atomically, so restarting cannot skip a page.
        with transaction.atomic():
            Candle.objects.bulk_create([Candle(symbol=symbol,
                opened_at=datetime.fromtimestamp(b["opened_at"], dt_timezone.utc),
                closed_at=datetime.fromtimestamp(b["closed_at"], dt_timezone.utc), payload=b) for b in batch],
                ignore_conflicts=True, batch_size=1000)
            previous_start = progress.get("start")
            gaps = sum(max(0, int((b["opened_at"]-a["closed_at"])/60)) for a, b in zip(batch, batch[1:]))
            if previous_start is not None:
                gaps += max(0, int((previous_start-batch[-1]["closed_at"])/60))
            progress.update(candles=progress["candles"]+len(batch), start=batch[0]["opened_at"],
                end=progress.get("end", batch[-1]["closed_at"]), next_end_ms=cursor,
                missing_minutes=progress["missing_minutes"]+gaps)
            if save_checkpoint:
                save_checkpoint(dict(progress))
        if stop is not None and stop.is_set():
            raise HistoryPaused()
    progress["exhausted"] = progress["candles"] < count
    return progress
