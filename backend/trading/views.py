import csv
import hashlib
import io
import json
from functools import wraps

from django.conf import settings
from django.contrib.auth import authenticate, login, logout
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.http import HttpResponse, JsonResponse, StreamingHttpResponse
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from .configuration import DEFAULTS, fingerprint, validate_config
from .engine import metrics
from .models import Candle, Event, Heartbeat, Job, LoginThrottle, Run
from .research import candidate_configs
from .services import create_paper, readiness


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
        except (ValueError, KeyError, TypeError, ValidationError) as exc:
            return JsonResponse({"error": str(exc)[:300] if isinstance(exc, ValueError) else "Invalid request fields"}, status=400)
    return wrapped


def symbol_value(value):
    if value not in settings.SYMBOLS or value not in ("BTCUSDT", "ETHUSDT"):
        raise ValueError("Choose a configured BTCUSDT or ETHUSDT market")
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


def run_summary(run):
    values = run.results.get("holdout", {}).get("metrics", {}) if run.mode in ("replay", "candles") else run.results
    return {"id": str(run.id), "name": run.name, "symbol": run.symbol, "mode": run.mode, "status": run.status,
            "config": run.config, "metrics": values, "started_at": run.started_at, "ended_at": run.ended_at,
            "entries_paused": run.entries_paused, "flatten_requested": run.flatten_requested, "error": run.error,
            "testnet": run.state.get("testnet"), "validation_id": str(run.validation_id) if run.validation_id else None}


@require_GET
@api
def dashboard(request):
    now = timezone.now()
    sources = []
    for source in Heartbeat.objects.all():
        age = (now-source.updated_at).total_seconds()
        ttl = 600 if source.name in ("ai", "news", "heatmap") else 45
        sources.append({"name": source.name, "status": source.status if age <= ttl else "stale", "detail": source.detail, "at": source.updated_at, "age_seconds": age})
    markets = []
    for symbol in settings.SYMBOLS:
        records = {}
        for kind in ("book", "candle", "flow", "derivatives", "assessment", "heatmap", "liquidation"):
            item = Event.objects.filter(symbol=symbol, kind=kind).order_by("id").last()
            records[kind] = {"id": item.id, "at": item.received_at, "payload": item.payload} if item else None
        first = Event.objects.filter(symbol=symbol).order_by("id").first()
        candles = Candle.objects.filter(symbol=symbol)
        first_bar, last_bar = candles.order_by("opened_at").first(), candles.order_by("opened_at").last()
        markets.append({"symbol": symbol, "records": records, "recording_since": first.received_at if first else None,
                        "candles": candles.count(), "candle_start": first_bar.opened_at if first_bar else None,
                        "candle_end": last_bar.closed_at if last_bar else None})
    news = [{"id": e.id, "at": e.received_at, **e.payload} for e in Event.objects.filter(kind="news").order_by("-id")[:10]]
    return JsonResponse({"now": now, "sources": sources, "markets": markets, "news": news,
        "runs": [run_summary(r) for r in Run.objects.filter(owner=request.user).order_by("-created_at")[:50]],
        "jobs": list(Job.objects.filter(owner=request.user).order_by("-created_at").values("id", "kind", "status", "error", "result", "created_at")[:10]),
        "defaults": DEFAULTS, "ai_configured": bool(settings.OPENAI_API_KEY and settings.OPENAI_MODEL),
        "model": settings.OPENAI_MODEL or None, "live_server_enabled": settings.LIVE_TRADING_ENABLED,
        "execution_environment": "Binance Spot Testnet" if settings.BINANCE_TESTNET else "Binance Spot production"})


@require_POST
@api
def jobs(request):
    data = body(request)
    symbol = symbol_value(data.get("symbol"))
    if Job.objects.filter(owner=request.user, status__in=["queued", "running"]).count() >= 3:
        raise ValueError("Wait for your current research jobs to finish")
    kind = data.get("kind")
    if kind == "history":
        count = data.get("count", 1000)
        if type(count) is not int or not 50 <= count <= 5000:
            raise ValueError("History must contain 50–5,000 candles")
        params = {"symbol": symbol, "count": count}
    elif kind == "backtest":
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
    return JsonResponse({**run_summary(run), "results": run.results, "curve": run.state.get("curve", []),
        "wallets": run.state.get("wallets", {}), "last_event_id": run.last_event_id,
        "decisions": list(run.decisions.order_by("-id").values("strategy", "at", "action", "reason", "features", "event_id")[:100]),
        "fills": list(run.fills.order_by("-id").values("strategy", "at", "side", "quantity", "price", "fee", "pnl", "reason", "details")[:100]),
        "orders": list(run.orders.order_by("-created_at").values("client_id", "kind", "status", "error", "created_at")[:30])})


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
