import csv
import hashlib
import io
import json
import math
from functools import wraps

from django.conf import settings
from django.contrib.auth import authenticate, login, logout
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.db.models import Q
from django.http import HttpResponse, JsonResponse, StreamingHttpResponse
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from .configuration import DEFAULTS, fingerprint, validate_config
from .engine import MAX_CANDLE_AGE_SECONDS, metrics
from .models import Candle, Event, Heartbeat, Job, LoginThrottle, MarketModel, Run
from .ml_training import model_summary
from .markets import ASSETS, MAX_HISTORY_CANDLES, MAX_QUEUED_JOBS, MAX_RESEARCH_CANDLES
from .research import candidate_configs
from .services import create_paper, readiness
from . import autonomy
from .models import AutoCycle, AutonomyPolicy


def body(request):
    try:
        parsed = json.loads(request.body or b"{}")
    except (ValueError, UnicodeDecodeError):
        raise ValueError("Expected a JSON object") from None
    if not isinstance(parsed, dict):
        raise ValueError("Expected a JSON object")
    return parsed


def api(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse({"error": "Sign in to your Striangle account"}, status=401)
        try:
            return view(request, *args, **kwargs)
        except Run.DoesNotExist:
            return JsonResponse({"error": "Run not found"}, status=404)
        except Job.DoesNotExist:
            return JsonResponse({"error": "Research job not found"}, status=404)
        except AutoCycle.DoesNotExist:
            return JsonResponse({"error": "Automatic research cycle not found"}, status=404)
        except (ValueError, KeyError, TypeError, ValidationError) as exc:
            return JsonResponse({"error": str(exc)[:300] if isinstance(exc, ValueError) else "Invalid request fields"}, status=400)
    return wrapped


def symbol_value(value):
    if value not in settings.SYMBOLS or value not in ASSETS:
        raise ValueError("Choose a configured BTCUSDT, XRPUSDT, SOLUSDT or ETHUSDT market")
    return value


@require_GET
def health(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        return JsonResponse({"status": "ok"})
    except Exception:
        return JsonResponse({"status": "database unavailable"}, status=503)


@require_GET
@ensure_csrf_cookie
def session(request):
    return JsonResponse({"authenticated": request.user.is_authenticated,
                         "username": request.user.username if request.user.is_authenticated else None})


@require_POST
def login_view(request):
    try:
        data = body(request)
        username, password = data.get("username", ""), data.get("password", "")
        if not isinstance(username, str) or not isinstance(password, str) or len(username) > 150 or len(password) > 1024:
            raise ValueError("Invalid credentials")
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    # Persisted per-account throttle works across web processes and restarts.
    key = hashlib.sha256(username.casefold().encode()).hexdigest()
    with transaction.atomic():
        throttle, _ = LoginThrottle.objects.get_or_create(key=key)
        throttle = LoginThrottle.objects.select_for_update().get(pk=key)
        if (timezone.now()-throttle.window_start).total_seconds() > 900:
            throttle.attempts, throttle.window_start = 0, timezone.now()
        if throttle.attempts >= 10:
            return JsonResponse({"error": "Too many sign-in attempts. Try again in 15 minutes."}, status=429)
        user = authenticate(request, username=username, password=password)
        throttle.attempts = 0 if user else throttle.attempts+1
        throttle.save()
    if user is None:
        return JsonResponse({"error": "Username or password is incorrect"}, status=401)
    login(request, user)
    return JsonResponse({"authenticated": True, "username": user.username})


@require_POST
@api
def logout_view(request):
    logout(request)
    return JsonResponse({"authenticated": False})


def runtime_status(run, now=None):
    now = (now or timezone.now()).timestamp()
    market, runtime = run.state.get("market", {}), run.state.get("runtime", {})
    book_age = now-market["book_at"] if "book_at" in market else None
    candle_age = now-market["last_closed_at"] if "last_closed_at" in market else None
    warmup = len(market.get("closes", []))
    if run.status not in ("running", "reconciling"):
        status = run.status
    elif book_age is None:
        status = "waiting for live book"
    elif not 0 <= book_age <= run.config["max_book_age_seconds"]:
        status = "stale market data"
    elif warmup < run.config["slow"]:
        status = "warming indicators"
    elif candle_age is None or not 0 <= candle_age <= MAX_CANDLE_AGE_SECONDS:
        status = "waiting for fresh candles"
    elif run.flatten_requested:
        status = "closing positions"
    elif run.entries_paused:
        status = "entries paused"
    else:
        status = "monitoring live data"
    return {"status": status, "decision_interval_seconds": run.config.get("decision_interval_seconds"),
        "book_age_seconds": book_age, "candle_age_seconds": candle_age,
        "processed_at": runtime.get("processed_at"), "processing_delay_seconds": runtime.get("processing_delay_seconds"),
        "evaluated_at": run.state.get("evaluated_at"), "evaluated_event_id": run.state.get("evaluated_event_id"),
        "warmup_candles": warmup, "warmup_required": run.config["slow"],
        "seeded_candles": run.state.get("warmup", {}).get("seeded_candles", 0)}


def run_summary(run, now=None):
    values = run.results.get("holdout", {}).get("metrics", {}) if run.mode in ("replay", "candles") else run.results
    return {"id": str(run.id), "name": run.name, "symbol": run.symbol, "mode": run.mode, "status": run.status,
            "config": run.config, "metrics": values, "started_at": run.started_at, "ended_at": run.ended_at,
            "entries_paused": run.entries_paused, "flatten_requested": run.flatten_requested, "error": run.error,
            "testnet": run.state.get("testnet"), "validation_id": str(run.validation_id) if run.validation_id else None,
            "runtime": runtime_status(run, now) if run.mode in ("paper", "live") else None}


def source_statuses(now):
    sources = []
    for source in Heartbeat.objects.all():
        age = (now-source.updated_at).total_seconds()
        ttl = 600 if source.name in ("ai", "news", "heatmap") else 45
        sources.append({"name": source.name, "status": source.status if age <= ttl else "stale", "detail": source.detail, "at": source.updated_at, "age_seconds": age})
    return sources


def market_records(symbol):
    records = {}
    for kind in ("book", "candle", "flow", "derivatives", "assessment", "heatmap", "liquidation"):
        # This ordering uses (symbol, kind, received_at), including the fast
        # empty result for sources which have never been configured.
        item = Event.objects.filter(symbol=symbol, kind=kind).order_by("-received_at", "-id").first()
        records[kind] = {"id": item.id, "at": item.received_at, "available_at": item.available_at, "payload": item.payload} if item else None
    return records


@require_GET
@api
def dashboard(request):
    now = timezone.now()
    sources = source_statuses(now)
    markets = []
    for symbol in settings.SYMBOLS:
        records = market_records(symbol)
        first = Event.objects.filter(symbol=symbol).order_by("id").first()
        candles = Candle.objects.filter(symbol=symbol, interval="1m")
        first_bar, last_bar = candles.order_by("opened_at").first(), candles.order_by("opened_at").last()
        markets.append({"symbol": symbol, "records": records, "recording_since": first.received_at if first else None,
                        "candles": candles.count(), "candle_start": first_bar.opened_at if first_bar else None,
                        "candle_end": last_bar.closed_at if last_bar else None})
    news = [{"id": e.id, "at": e.received_at, **e.payload} for e in Event.objects.filter(kind="news").order_by("-id")[:10]]
    return JsonResponse({"now": now, "sources": sources, "markets": markets, "news": news,
        "runs": [run_summary(r) for r in Run.objects.filter(owner=request.user).order_by("-created_at")[:50]],
        "jobs": list(Job.objects.filter(Q(owner=request.user) | Q(owner__isnull=True, kind__in=["train", "autotrain"])).order_by("-created_at").values("id", "kind", "status", "error", "result", "params", "created_at")[:12]),
        "autonomy": autonomy.summary(), "can_control_autonomy": request.user.is_staff,
        "market_models": [model_summary(model) for symbol in settings.SYMBOLS
            if (model := MarketModel.objects.filter(symbol=symbol).order_by("-created_at").first())],
        "limits": {"history_candles": MAX_HISTORY_CANDLES, "research_candles": MAX_RESEARCH_CANDLES},
        "defaults": DEFAULTS, "ai_configured": bool(settings.OPENAI_API_KEY and settings.OPENAI_MODEL),
        "model": settings.OPENAI_MODEL or None, "live_server_enabled": settings.LIVE_TRADING_ENABLED,
        "execution_environment": "Binance Spot Testnet" if settings.BINANCE_TESTNET else "Binance Spot production"})


@require_GET
@api
def realtime(request):
    now = timezone.now()
    data = {"now": now, "sources": source_statuses(now),
        "autonomy_live": autonomy.summary(full=False),
        "markets": [{"symbol": symbol, "records": market_records(symbol)} for symbol in settings.SYMBOLS],
        "runs": [run_summary(r, now) for r in Run.objects.filter(owner=request.user).order_by("-created_at")[:50]],
        "news": [{"id": e.id, "at": e.received_at, **e.payload} for e in Event.objects.filter(kind="news").order_by("-id")[:10]],
        "cadence": {"dashboard_seconds": 1, "worker_idle_seconds": 0.25, "news_seconds": 120, "ai_seconds": 300}}
    if request.GET.get("run_id"):
        run = Run.objects.get(pk=request.GET["run_id"], owner=request.user)
        data["detail"] = run_detail_data(run)
    return JsonResponse(data)


@require_POST
@api
def autonomy_control(request):
    if not request.user.is_staff:
        return JsonResponse({"error": "A workspace administrator controls the shared automatic research service"}, status=403)
    data = body(request)
    if set(data)-{"enabled", "config", "run_now"} or "enabled" not in data:
        raise ValueError("Supply enabled, optional config and optional run_now")
    if type(data.get("run_now", False)) is not bool or data.get("run_now") and data["enabled"] is not True:
        raise ValueError("run_now must be a boolean and requires enabled research")
    current = AutonomyPolicy.objects.filter(pk=1).first()
    autonomy.configure(data.get("config", current.config if current else None), data["enabled"])
    autonomy.schedule_cycle(force=data.get("run_now", False))
    return JsonResponse(autonomy.summary())


@require_GET
@api
def autonomy_report(request, cycle_id):
    cycle = AutoCycle.objects.defer("artifacts", "state").get(pk=cycle_id)
    return JsonResponse({"id": str(cycle.id), "status": cycle.status, "config": cycle.config,
        "cutoff": cycle.cutoff, "report": cycle.report, "error": cycle.error})


@require_POST
@api
def jobs(request):
    data = body(request)
    kind = data.get("kind")
    if kind == "history":
        symbols = data.get("symbols", [data.get("symbol")])
        if not isinstance(symbols, list) or not 1 <= len(symbols) <= 4 or ("symbols" in data and "symbol" in data):
            raise ValueError("Choose one asset or a list of up to four assets")
        symbols = list(dict.fromkeys(symbol_value(s) for s in symbols))
        count = data.get("count", 100000)
        if type(count) is not int or not 50 <= count <= MAX_HISTORY_CANDLES:
            raise ValueError("History must contain 50–1,000,000 candles per asset")
        end = data.get("end", timezone.now().timestamp())
        if isinstance(end, bool) or not isinstance(end, (int, float)) or not math.isfinite(end) or not 0 < end <= timezone.now().timestamp():
            raise ValueError("Choose a past history end time in UTC")
        if Job.objects.filter(owner=request.user, status__in=["queued", "running"]).count()+len(symbols) > MAX_QUEUED_JOBS:
            raise ValueError("Wait for your current research jobs to finish; at most eight can be queued")
        with transaction.atomic():
            created = [Job.objects.create(owner=request.user, kind=kind,
                params={"symbol": symbol, "count": count, "end_ms": int(end*1000)-1},
                result={"symbol": symbol, "requested": count, "candles": 0}) for symbol in symbols]
        return JsonResponse({"id": str(created[0].id), "ids": [str(j.id) for j in created], "status": "queued"}, status=202)
    symbol = symbol_value(data.get("symbol"))
    if Job.objects.filter(owner=request.user, status__in=["queued", "running"]).count() >= MAX_QUEUED_JOBS:
        raise ValueError("Wait for your current research jobs to finish")
    if kind == "train":
        count = data.get("count", 100000)
        if type(count) is not int or not 5000 <= count <= 100000:
            raise ValueError("Training requires 5,000–100,000 candles per asset")
        if Job.objects.filter(kind="train", status__in=["queued", "running"], params__symbol=symbol).exists():
            raise ValueError("Training is already queued or running for this asset")
        params = {"symbol": symbol, "count": count, "cutoff": timezone.now().timestamp(), "config": validate_config(data.get("config"))}
        job = Job.objects.create(owner=request.user, kind=kind, params=params)
        return JsonResponse({"id": str(job.id), "status": job.status}, status=202)
    if kind == "backtest":
        mode = data.get("mode")
        if mode not in ("candles", "replay"):
            raise ValueError("Choose candle research or recorded event replay")
        config = validate_config(data.get("config"))
        fast, slow = data.get("fast_values", [config["fast"]]), data.get("slow_values", [config["slow"]])
        candidate_configs(config, fast, slow)
        start, end = float(data["start"]), float(data["end"])
        if not 0 < start < end <= timezone.now().timestamp():
            raise ValueError("Choose valid past start/end timestamps")
        strategy = data.get("selection_strategy", "trend")
        if strategy not in ("trend", "rsi", "ai_trend") or (mode == "candles" and strategy == "ai_trend"):
            raise ValueError("AI strategy selection requires recorded event replay")
        params = {"symbol": symbol, "mode": mode, "config": config, "fast_values": fast, "slow_values": slow,
                  "start": start, "end": end, "selection_strategy": strategy}
    else:
        raise ValueError("Unknown research job")
    job = Job.objects.create(owner=request.user, kind=kind, params=params)
    return JsonResponse({"id": str(job.id), "status": job.status}, status=202)


@require_POST
@api
def resume_history(request, job_id):
    with transaction.atomic():
        job = Job.objects.select_for_update().get(pk=job_id, owner=request.user, kind="history")
        if job.status != "failed":
            raise ValueError("Only a failed history import needs manual resuming")
        if Job.objects.filter(owner=request.user, status__in=["queued", "running"]).count() >= MAX_QUEUED_JOBS:
            raise ValueError("Wait for your current research jobs to finish")
        job.status, job.error, job.finished_at = "queued", "", None
        job.save(update_fields=["status", "error", "finished_at"])
    return JsonResponse({"id": str(job.id), "status": job.status}, status=202)


@require_POST
@api
def runs(request):
    data = body(request)
    if set(data) - {"symbol", "config", "validation_id"}:
        raise ValueError("Only paper experiments can be created here; unexpected fields rejected")
    validation = Run.objects.get(pk=data["validation_id"], owner=request.user) if data.get("validation_id") else None
    raw = validation.config if validation else data.get("config")
    run = create_paper(request.user, symbol_value(data.get("symbol")), raw, validation)
    return JsonResponse(run_summary(run), status=201)


@require_GET
@api
def run_detail(request, run_id):
    run = Run.objects.get(pk=run_id, owner=request.user)
    return JsonResponse(run_detail_data(run))


def run_detail_data(run):
    return {**run_summary(run), "results": run.results, "curve": run.state.get("curve", []),
        "wallets": run.state.get("wallets", {}), "last_event_id": run.last_event_id,
        "latest_signals": run.state.get("latest_signals", {}),
        "market_model": {"id": str(run.market_model_id), "version": run.state.get("market", {}).get("ml_model", {}).get("version")} if run.market_model_id else None,
        "decisions": list(run.decisions.order_by("-id").values("strategy", "at", "action", "reason", "features", "event_id")[:100]),
        "fills": list(run.fills.order_by("-id").values("strategy", "at", "side", "quantity", "price", "fee", "pnl", "reason", "details")[:100]),
        "orders": list(run.orders.order_by("-created_at").values("client_id", "kind", "status", "error", "created_at")[:30])}


@require_POST
@api
def control(request, run_id):
    data = body(request)
    with transaction.atomic():
        run = Run.objects.select_for_update().get(pk=run_id, owner=request.user)
        if run.mode not in ("paper", "live") or run.status not in ("running", "reconciling"):
            raise ValueError("This run is no longer active")
        action = data.get("action")
        if action == "pause":
            run.entries_paused = True
        elif action == "resume":
            if run.flatten_requested or run.status == "reconciling":
                raise ValueError("A stopping or unresolved run cannot resume entries")
            if run.mode == "live" and not settings.LIVE_TRADING_ENABLED:
                raise ValueError("Live execution is disabled on the server")
            run.entries_paused = False
        elif action == "stop":
            run.entries_paused, run.flatten_requested = True, True
        else:
            raise ValueError("Unknown control action")
        run.save(update_fields=["entries_paused", "flatten_requested"])
    return JsonResponse(run_summary(run))


@require_GET
@api
def live_readiness(request, run_id):
    run = Run.objects.select_related("validation").get(pk=run_id, owner=request.user)
    return JsonResponse(readiness(run))


@require_GET
@api
def export_fills(request, run_id):
    run = Run.objects.get(pk=run_id, owner=request.user)
    output = io.StringIO()
    fields = ["strategy", "at", "side", "quantity", "price", "fee", "pnl", "reason"]
    writer = csv.writer(output)
    writer.writerow(fields)
    for fill in run.fills.order_by("id").values_list(*fields).iterator():
        writer.writerow(fill)
    response = HttpResponse(output.getvalue(), content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="striangle-{run.id}-fills.csv"'
    return response


@require_GET
@api
def export_events(request):
    symbol = symbol_value(request.GET.get("symbol", "BTCUSDT"))
    after = int(request.GET.get("after", "0"))
    if after < 0:
        raise ValueError("after must be a non-negative event ID")
    rows = Event.objects.filter(symbol=symbol, id__gt=after).order_by("id")[:100000]

    def lines():
        for row in rows.iterator(chunk_size=1000):
            yield json.dumps({"id": row.id, "source": row.source, "kind": row.kind, "symbol": row.symbol,
                "event_at": row.event_at.isoformat(), "received_at": row.received_at.isoformat(), "available_at": row.available_at.isoformat(), "payload": row.payload})+"\n"
    response = StreamingHttpResponse(lines(), content_type="application/x-ndjson")
    response["Content-Disposition"] = f'attachment; filename="striangle-{symbol}-events.jsonl"'
    return response


@require_GET
@api
def export_candles(request):
    symbol = symbol_value(request.GET.get("symbol", "BTCUSDT"))
    rows = Candle.objects.filter(symbol=symbol, interval="1m", fetched_at__lte=timezone.now()).order_by("opened_at")

    def lines():
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["time", "open", "high", "low", "close", "volume", "closed_at", "fetched_at", "source", "symbol", "interval"])
        yield buffer.getvalue()
        for row in rows.iterator(chunk_size=1000):
            buffer.seek(0)
            buffer.truncate(0)
            writer.writerow([row.opened_at.isoformat(), *(row.payload[k] for k in ("open", "high", "low", "close", "volume")),
                             row.closed_at.isoformat(), row.fetched_at.isoformat(), "binance_spot", symbol, "1m"])
            yield buffer.getvalue()
    response = StreamingHttpResponse(lines(), content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="striangle-{symbol}-1m-training.csv"'
    return response
