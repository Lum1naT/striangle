"""Research jobs use frozen chronological partitions and never call an AI API."""
from copy import deepcopy
from itertools import product

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .configuration import fingerprint, validate_config, dec
from .engine import features, initial_state, mark, metrics, signal, simulate_fill, step
from .models import Candle, Event, Job, Run
from .history import HistoryPaused, import_history
from .markets import MAX_RESEARCH_CANDLES
from .providers import ProviderError
from .recording import heartbeat, stamp


def as_event(row):
    return {"id": row.id, "at": max(row.available_at.timestamp(), row.received_at.timestamp()),
            "kind": row.kind, "payload": row.payload, "event_at": row.event_at.timestamp(),
            "received_at": row.received_at.timestamp()}


def replay(events, config, strategies=("trend", "rsi", "ai_trend")):
    state = initial_state(config, strategies)
    counts = {"decisions": 0, "fills": 0, "assessments": 0, "gaps": 0}
    for event in events:
        # Assessments are immutable, historical records. Never re-evaluate past
        # headlines with today's model or add today's heatmap to historical bars.
        ds, fs = step(state, event, config)
        counts["decisions"] += len(ds)
        counts["fills"] += len(fs)
        counts["assessments"] += int(event["kind"] == "assessment")
        counts["gaps"] += int(event["kind"] == "gap")
    return {"metrics": metrics(state, config), "curve": state["curve"], "coverage": counts,
            "end_positions": "Open positions are marked at the final recorded bid with estimated exit costs, not invented closing fills."}


def bar_test(bars, config):
    state = initial_state(config, ("trend", "rsi"))
    closes, previous, fills = [], None, []
    half_spread = dec(config["spread_bps"])/20000

    def book(price):
        return {"bids": [[str(dec(price)*(1-half_spread)), "1e24"]], "asks": [[str(dec(price)*(1+half_spread)), "1e24"]]}

    def fill(wallet, side, price, at, reason, name):
        row = simulate_fill(wallet, side, book(price), config, at, reason)
        if row:
            row["strategy"] = name
            row["details"]["model"] = "candle_assumptions_not_recorded_depth"
            fills.append(row)

    for i, bar in enumerate(bars):
        if previous is not None and bar["opened_at"]-previous != 60:
            closes = []
            for wallet in state["wallets"].values():
                wallet["pending"] = None
        previous = bar["opened_at"]
        for name, wallet in state["wallets"].items():
            exited = False
            if dec(wallet["qty"]) > 0:
                stop = dec(wallet["entry"])*(1-dec(config["stop_pct"])/100)
                target = dec(wallet["entry"])*(1+dec(config["take_pct"])/100)
                if dec(bar["open"]) <= stop or dec(bar["open"]) >= target:
                    fill(wallet, "sell", bar["open"], bar["opened_at"], "Opening gap protective exit", name)
                    exited = True
            pending = wallet.get("pending")
            wallet["pending"] = None
            if not exited and pending == "sell" and dec(wallet["qty"]) > 0:
                fill(wallet, "sell", bar["open"], bar["opened_at"], "Prior closed-bar exit signal", name)
                exited = True
            elif not exited and pending == "buy" and dec(wallet["qty"]) == 0 and not wallet["halted"] and i < len(bars)-1:
                fill(wallet, "buy", bar["open"], bar["opened_at"], "Prior closed-bar entry signal", name)
            if dec(wallet["qty"]) > 0:
                stop = dec(wallet["entry"])*(1-dec(config["stop_pct"])/100)
                target = dec(wallet["entry"])*(1+dec(config["take_pct"])/100)
                if dec(bar["low"]) <= stop:
                    fill(wallet, "sell", stop, bar["closed_at"], "Stop touch; stop wins ambiguous bars", name)
                elif dec(bar["high"]) >= target:
                    fill(wallet, "sell", target, bar["closed_at"], "Target touch", name)
            if i == len(bars)-1 and dec(wallet["qty"]) > 0:
                fill(wallet, "sell", bar["close"], bar["closed_at"], "End of sample liquidation", name)
            mark(wallet, book(bar["close"])["bids"][0][0], bar["closed_at"], config)
        closes = (closes+[float(bar["close"])])[-201:]
        f = features({"closes": closes}, bar["closed_at"], config)
        for name, wallet in state["wallets"].items():
            action, _ = signal(name, f, dec(wallet["qty"]) > 0, config)
            if wallet["halted"] and dec(wallet["qty"]) > 0:
                action = "sell"
            if action != "hold":
                wallet["pending"] = action
        state["curve"].append({"at": bar["closed_at"], **{name: float(w["equity"]) for name, w in state["wallets"].items()}})
    return {"metrics": metrics(state, config), "curve": state["curve"][::max(1, len(bars)//1000)], "fills": fills[-200:],
            "assumptions": "Next-open fills; fixed spread, fees and adverse slippage; stop-first ambiguous bars; no order-book or news history; unleveraged spot; no funding charges."}


def candidate_configs(base, fast_values, slow_values):
    if not isinstance(fast_values, list) or not isinstance(slow_values, list) or not fast_values or not slow_values or len(fast_values)*len(slow_values) > 16:
        raise ValueError("Use nonempty fast/slow lists with at most 16 combinations")
    candidates = []
    for fast, slow in product(fast_values, slow_values):
        if dec(fast) < dec(slow):
            candidate = validate_config(base | {"fast": fast, "slow": slow})
            if candidate not in candidates:
                candidates.append(candidate)
    if not candidates:
        raise ValueError("No valid fast/slow combinations")
    return candidates


def chronological_research(data, configs, mode, selection_strategy):
    if len(data) < 100:
        raise ValueError("At least 100 observations are needed for chronological research")
    # Choose the split by time, so simultaneous observations never cross it.
    time_key = (lambda e: e["at"]) if mode == "replay" else (lambda e: e["opened_at"])
    boundary = time_key(data[int(len(data)*0.7)])
    train = [x for x in data if time_key(x) < boundary]
    holdout = [x for x in data if time_key(x) >= boundary]
    if min(len(train), len(holdout)) < 30:
        raise ValueError("Both chronological partitions need more data")
    test = replay if mode == "replay" else bar_test
    ranked = []
    # Only training is evaluated during selection. Holdout is evaluated once
    # for the selected candidate, not used to rank or iterate the parameter grid.
    for cfg in configs:
        result = test(train, cfg)
        metric = result["metrics"].get(selection_strategy)
        if metric is None:
            raise ValueError("AI strategies require recorded event replay")
        ranked.append({"config": cfg, "train": result, "score": metric["return_pct"]})
    ranked.sort(key=lambda x: (-x["score"], x["train"]["metrics"][selection_strategy]["max_drawdown_pct"]))
    chosen = ranked[0]
    held = test(holdout, chosen["config"])
    return chosen["config"], {"train": chosen["train"], "holdout": held,
        "candidates": [{"config": r["config"], "train_metrics": r["train"]["metrics"]} for r in ranked],
        "selected_by": f"{selection_strategy} training return; holdout never used for ranking",
        "selection_strategy": selection_strategy,
        "train_start": time_key(train[0]), "train_end": time_key(train[-1]), "holdout_start": boundary,
        "holdout_end": time_key(holdout[-1]), "train_count": len(train), "holdout_count": len(holdout),
        "caution": "Repeatedly choosing configurations after viewing holdout results contaminates that holdout. Paper trading begins on future data with frozen settings."}


def perform_job(job, stop=None):
    params = job.params
    symbol = params["symbol"]
    if job.kind == "history":
        def checkpoint(progress):
            job.result = progress
            job.save(update_fields=["result"])
            transaction.on_commit(lambda: heartbeat("research", "running", f"Importing {symbol}: {progress['candles']:,}/{progress['requested']:,} candles"))
        return import_history(symbol, params.get("count", 100000), end_ms=params.get("end_ms"),
            checkpoint=job.result, save_checkpoint=checkpoint, stop=stop)
    mode = params["mode"]
    base = validate_config(params.get("config"))
    configs = candidate_configs(base, params.get("fast_values", [base["fast"]]), params.get("slow_values", [base["slow"]]))
    start, end = stamp(params["start"]), stamp(params["end"])
    if end > timezone.now() or start >= end:
        raise ValueError("Choose a past interval with start before end")
    if mode == "replay":
        query = Event.objects.filter(symbol=symbol, available_at__gte=start, available_at__lt=end).exclude(kind="ai_attempt").order_by("id")
        if query.count() > 250000:
            raise ValueError("Replay is limited to 250,000 events per job; choose a shorter interval")
        data = [as_event(e) for e in query]
    else:
        data = list(Candle.objects.filter(symbol=symbol, opened_at__gte=start, closed_at__lte=end).order_by("opened_at").values_list("payload", flat=True)[:MAX_RESEARCH_CANDLES+1])
        if len(data) > MAX_RESEARCH_CANDLES:
            raise ValueError("Candle research is limited to 100,000 bars per job; choose a shorter window or export the full training dataset")
    chosen, results = chronological_research(data, configs, mode, params.get("selection_strategy", "trend"))
    run = Run.objects.create(owner=job.owner, symbol=symbol, name=f"{symbol} {mode} research", mode=mode, status="completed",
        config=chosen, config_hash=fingerprint(chosen, settings.OPENAI_MODEL), results=results,
        started_at=start, ended_at=end)
    return {"run_id": str(run.id)}


def work_one_job(stop=None):
    with transaction.atomic():
        # Single research worker; row locking also makes claiming safe on Postgres.
        job = Job.objects.select_for_update().filter(status="queued").order_by("created_at").first()
        if not job:
            return False
        job.status, job.started_at = "running", timezone.now()
        job.save(update_fields=["status", "started_at"])
    try:
        result = perform_job(job, stop)
        job.result, job.status = result, "completed"
    except HistoryPaused:
        job.status, job.finished_at = "queued", None
        job.save(update_fields=["result", "status", "finished_at"])
        return True
    except Exception as exc:
        job.refresh_from_db(fields=["result"])
        job.status = "failed"
        job.error = str(exc)[:500] if isinstance(exc, (ValueError, ProviderError)) else f"Research failed ({type(exc).__name__}); inspect worker logs."
    job.finished_at = timezone.now()
    job.save(update_fields=["result", "status", "error", "finished_at"])
    return True


def recover_jobs():
    Job.objects.filter(status="running", kind="history").update(status="queued", error="", finished_at=None)
    Job.objects.filter(status="running").exclude(kind="history").update(status="failed", error="Research worker restarted before completion; submit a new job", finished_at=timezone.now())
