import asyncio
import hashlib
import json
import logging
import time
from datetime import datetime, timedelta, timezone as dt_timezone

from asgiref.sync import sync_to_async
from django.conf import settings
from django.db import close_old_connections, transaction
from django.utils import timezone
from websockets.asyncio.client import connect

from .ai import assess_news
from .configuration import dec
from .models import Candle, Event, Heartbeat
from .providers import fetch_heatmap, fetch_news, normalize_binance, normalize_bybit, observation

log = logging.getLogger(__name__)


def stamp(value):
    return datetime.fromtimestamp(value, dt_timezone.utc)


def heartbeat(name, status, detail=""):
    Heartbeat.objects.update_or_create(name=name, defaults={"updated_at": timezone.now(), "status": status, "detail": detail[:300]})


def store_batch(batch):
    close_old_connections()
    rows = [Event(**{**row, "event_at": stamp(row["event_at"]), "received_at": stamp(row["received_at"])}) for row in batch]
    with transaction.atomic():
        Event.objects.bulk_create(rows, ignore_conflicts=True, batch_size=500)
        for row in batch:
            if row["kind"] == "candle":
                body = row["payload"]
                Candle.objects.get_or_create(symbol=row["symbol"], interval="1m", opened_at=stamp(body["opened_at"]),
                    defaults={"closed_at": stamp(body["closed_at"]), "payload": body, "fetched_at": stamp(row["received_at"])})
    heartbeat("recorder", "running", f"Recorded {len(batch)} observations in the last batch")


async def writer(queue):
    while True:
        first = await queue.get()
        batch = [first]
        await asyncio.sleep(0.1)
        while len(batch) < 500 and not queue.empty():
            batch.append(queue.get_nowait())
        # An unrecoverable DB error terminates the recorder. It must never report
        # healthy while silently dropping observations.
        await sync_to_async(store_batch, thread_sensitive=True)(batch)
        for _ in batch:
            queue.task_done()


async def stream_loop(name, url, queue, stop, *, subscribe=None):
    delay = 1
    while not stop.is_set():
        cache = {}
        try:
            async with connect(url, ping_interval=20, ping_timeout=20, close_timeout=5, max_size=2_000_000, max_queue=1024) as socket:
                if subscribe:
                    await socket.send(json.dumps(subscribe))
                await sync_to_async(heartbeat)(name, "connected")
                delay, last_heartbeat, last_derivative, flow = 1, 0, {}, {}
                async for raw in socket:
                    if stop.is_set():
                        return
                    message = json.loads(raw)
                    now = time.time()
                    if name == "binance":
                        row = normalize_binance(message, now)
                        rows = [row] if row else []
                    else:
                        rows = normalize_bybit(message, cache, now)
                    for row in rows:
                        if row["kind"] == "trade":
                            symbol = row["symbol"]
                            second = int(now)
                            old = flow.get(symbol)
                            if old and old["second"] != second:
                                queue.put_nowait(observation("binance_spot", f"flow:{old['second']}", symbol, "flow",
                                    {"buy_notional": str(old["buy"]), "sell_notional": str(old["sell"]), "aggregate_trade_count": old["count"], "bucket_seconds": 1}))
                                old = None
                            if old is None:
                                old = flow[symbol] = {"second": second, "buy": dec(0), "sell": dec(0), "count": 0}
                            side = "buy" if row["payload"]["taker_buy"] else "sell"
                            old[side] += dec(row["payload"]["price"])*dec(row["payload"]["quantity"])
                            old["count"] += 1
                            continue
                        if row["kind"] == "derivatives":
                            if now-last_derivative.get(row["symbol"], 0) < 5:
                                continue
                            last_derivative[row["symbol"]] = now
                        # Bound backlog. Reconnect and mark a gap instead of
                        # treating an overloaded, delayed stream as live.
                        queue.put_nowait(row)
                    if now-last_heartbeat > 10:
                        await sync_to_async(heartbeat)(name, "connected", "Receiving provider messages")
                        last_heartbeat = now
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await sync_to_async(heartbeat)(name, "disconnected", type(exc).__name__)
            # Invalidate books and warmup when the primary execution feed gaps.
            if name == "binance":
                for symbol in settings.SYMBOLS:
                    await queue.put(observation("recorder", f"gap:{time.time_ns()}", symbol, "gap", {"reason": type(exc).__name__}))
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except TimeoutError:
                pass
            delay = min(delay*2, 60)


def recent_articles():
    cutoff = timezone.now()-timedelta(hours=6)
    events = Event.objects.filter(kind="news", received_at__gte=cutoff).order_by("-id")[:24]
    return [{"id": e.id, **e.payload} for e in events if e.payload.get("published_at", 0) >= cutoff.timestamp()]


def ai_attempts_today():
    start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
    return Event.objects.filter(kind="ai_attempt", received_at__gte=start).count()


async def context_loop(queue, stop):
    last_ai, last_news, last_heatmap = 0, 0, 0
    while not stop.is_set():
        now = time.time()
        if now-last_news >= 120:
            success = 0
            for feed in settings.NEWS_FEEDS:
                try:
                    articles = await asyncio.to_thread(fetch_news, feed)
                    for article in articles:
                        digest = hashlib.sha256((article["url"]+article["title"]).encode()).hexdigest()
                        await queue.put(observation("rss", digest, "GLOBAL", "news", article, article["published_at"]))
                    success += 1
                except Exception as exc:
                    await sync_to_async(heartbeat)("news", "error", type(exc).__name__)
            if success:
                await sync_to_async(heartbeat)("news", "running", f"Fetched {success} configured feeds")
            last_news = now
        if now-last_ai >= 300:
            if not settings.OPENAI_API_KEY or not settings.OPENAI_MODEL:
                await sync_to_async(heartbeat)("ai", "unconfigured", "Set OPENAI_API_KEY and OPENAI_MODEL")
            else:
                articles = await sync_to_async(recent_articles)()
                attempts = await sync_to_async(ai_attempts_today)()
                for symbol in settings.SYMBOLS:
                    if attempts >= settings.AI_DAILY_ATTEMPT_LIMIT:
                        await sync_to_async(heartbeat)("ai", "paused", f"Daily limit of {settings.AI_DAILY_ATTEMPT_LIMIT} assessment attempts reached")
                        break
                    if not articles:
                        await sync_to_async(heartbeat)("ai", "waiting", "No fresh news received")
                        break
                    await queue.put(observation("openai", str(time.time_ns()), symbol, "ai_attempt", {"model": settings.OPENAI_MODEL}))
                    attempts += 1
                    try:
                        body = await asyncio.to_thread(assess_news, symbol, articles[:12], api_key=settings.OPENAI_API_KEY, model=settings.OPENAI_MODEL)
                        await queue.put(observation("openai", body["response_id"] or str(time.time_ns()), symbol, "assessment", body))
                        await sync_to_async(heartbeat)("ai", "running", "Structured assessment recorded; score is not a probability")
                    except Exception as exc:
                        await sync_to_async(heartbeat)("ai", "error", type(exc).__name__)
            last_ai = now
        if settings.COINGLASS_API_KEY and now-last_heatmap >= 120:
            for symbol in settings.SYMBOLS:
                try:
                    body = await asyncio.to_thread(fetch_heatmap, symbol, settings.COINGLASS_API_KEY)
                    await queue.put(observation("coinglass", str(time.time_ns()), symbol, "heatmap", body))
                    await sync_to_async(heartbeat)("heatmap", "running", "Estimated liquidation levels, Model1")
                except Exception as exc:
                    await sync_to_async(heartbeat)("heatmap", "error", type(exc).__name__)
            last_heatmap = now
        elif not settings.COINGLASS_API_KEY:
            await sync_to_async(heartbeat)("heatmap", "unconfigured", "Optional CoinGlass key and heatmap entitlement required")
        try:
            await asyncio.wait_for(stop.wait(), timeout=5)
        except TimeoutError:
            pass


async def record_forever(stop):
    queue = asyncio.Queue(maxsize=20000)
    streams = "/".join(f"{symbol.lower()}@{suffix}" for symbol in settings.SYMBOLS for suffix in ("depth20", "aggTrade", "kline_1m"))
    writer_task = asyncio.create_task(writer(queue))
    producers = [
        asyncio.create_task(stream_loop("binance", "wss://data-stream.binance.vision/stream?streams="+streams, queue, stop)),
        asyncio.create_task(stream_loop("bybit", "wss://stream.bybit.com/v5/public/linear", queue, stop,
            subscribe={"op": "subscribe", "args": [f"{topic}.{symbol}" for symbol in settings.SYMBOLS for topic in ("allLiquidation", "tickers")]})),
        asyncio.create_task(context_loop(queue, stop)),
    ]
    stop_task = asyncio.create_task(stop.wait())
    try:
        done, _ = await asyncio.wait([writer_task, stop_task, *producers], return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if task is not stop_task:
                task.result()
    finally:
        for task in producers:
            task.cancel()
        await asyncio.gather(*producers, return_exceptions=True)
        if not writer_task.done():
            try:
                await asyncio.wait_for(queue.join(), timeout=15)
            except TimeoutError:
                log.error("Shutdown timed out draining market observations")
        writer_task.cancel()
        stop_task.cancel()
        await asyncio.gather(writer_task, stop_task, return_exceptions=True)
        await sync_to_async(heartbeat)("recorder", "stopped")
