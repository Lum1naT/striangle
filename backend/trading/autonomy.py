"""Durable daily research scheduling and immutable prospective paper cycles."""
import hashlib
import json
import logging
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .auto_config import AUTO_VERSION, margin_config, validate_auto
from .auto_search import backtest, price_features, rank_candidates, search_asset, signal_series
from .ml import predict_vector
from .models import AutoCycle, AutoRecord, AutonomyPolicy, Candle, FuturesCandle, Job

log = logging.getLogger(__name__)


def configure(config=None, enabled=True):
    if type(enabled) is not bool:
        raise ValueError("enabled must be a boolean")
    validated = validate_auto(config)
    with transaction.atomic():
        policy, _ = AutonomyPolicy.objects.get_or_create(pk=1)
        policy = AutonomyPolicy.objects.select_for_update().get(pk=1)
        changed, was_enabled = policy.config != validated, policy.enabled
        policy.config, policy.enabled = validated, enabled
        if not enabled or changed:
            AutoCycle.objects.filter(status="paper").update(status="closing")
            AutoCycle.objects.filter(status__in=["queued", "ready"]).update(status="cancelled")
        if enabled and (changed or not was_enabled):
            policy.next_run_at = timezone.now()
        policy.status = "Waiting for the research worker" if enabled else "Disabled; open paper positions will flatten on fresh futures books"
        policy.save()
    return policy


def schedule_cycle(now=None):
    now = now or timezone.now()
    with transaction.atomic():
        policy = AutonomyPolicy.objects.select_for_update().filter(pk=1).first()
        if not policy or not policy.enabled or now < policy.next_run_at:
            return None
        if AutoCycle.objects.filter(status__in=["queued", "training", "ready"]).exists():
            return None
        config = validate_auto(policy.config)
        if policy.last_cutoff:
            minimum = config["min_new_candles"]
            for symbol in settings.SYMBOLS:
                count = Candle.objects.filter(symbol=symbol, interval="1m", closed_at__gt=policy.last_cutoff,
                    closed_at__lte=now, fetched_at__lte=now).count()
                if count < minimum:
                    policy.status = f"Waiting for fresh data: {symbol} has {count}/{minimum} new candles"
                    policy.next_run_at = now+timedelta(minutes=5)
                    policy.save(update_fields=["status", "next_run_at"])
                    return None
        cycle = AutoCycle.objects.create(config=config, cutoff=now)
        Job.objects.create(kind="autotrain", params={"cycle_id": str(cycle.id)})
        policy.next_run_at = now+timedelta(hours=config["interval_hours"])
        policy.status = f"Research cycle {str(cycle.id)[:8]} queued"
        policy.save(update_fields=["next_run_at", "status"])
        return cycle


def history_query(table, symbol, cutoff):
    query = table.objects.filter(symbol=symbol, fetched_at__lte=cutoff, closed_at__lte=cutoff)
    if table is Candle:
        query = query.filter(interval="1m")
    return query.order_by("-opened_at")


def load_bars(table, symbol, cutoff, count):
    rows = history_query(table, symbol, cutoff).values("payload", "fetched_at")[:count]
    bars, sha = [], hashlib.sha256()
    for row in rows.iterator(chunk_size=2000):
        sha.update(json.dumps([row["payload"], row["fetched_at"].isoformat()], sort_keys=True).encode())
        bars.append({k: float(v) for k, v in row["payload"].items() if k in ("opened_at", "closed_at", "open", "high", "low", "close")})
    bars.reverse()
    return bars, sha.hexdigest()


def run_cycle(cycle_id, progress=lambda text: None, stop=None):
    from .locking import single_worker
    cycle = AutoCycle.objects.get(pk=cycle_id)
    if cycle.status not in ("queued", "training"):
        raise ValueError("This research cycle is no longer queued")
    cycle.status = "training"
    cycle.save(update_fields=["status"])
    policy, cutoff = validate_auto(cycle.config), cycle.cutoff
    cfg = margin_config(policy)
    count = policy["history_candles"]
    with single_worker("model-training", stop):
        # Venue history becomes the preferred training source as the recorder
        # accumulates it. The existing spot dataset remains explicitly a proxy.
        enough = all(history_query(FuturesCandle, s, cutoff).count() >= min(count, 10000) for s in settings.SYMBOLS)
        table = FuturesCandle if enough else Candle
        source = "bybit_perpetual_candles" if enough else "binance_spot_proxy"
        ranges = {}
        for symbol in settings.SYMBOLS:
            times = list(history_query(table, symbol, cutoff).values_list("opened_at", "closed_at")[:count])
            if len(times) < 5000:
                raise ValueError(f"Import at least 5,000 completed candles for {symbol}")
            ranges[symbol] = {"start": times[-1][0].timestamp(), "end": times[0][1].timestamp(), "count": len(times)}
            del times
        start = max(r["start"] for r in ranges.values())
        end = min(r["end"] for r in ranges.values())
        if cutoff.timestamp()-end > 7*86400:
            raise ValueError("The shared training history is more than seven days old; record or import recent candles")
        selection_start = (start+(end-start)*.6)//60*60
        test_start = (start+(end-start)*.8)//60*60
        artifacts, fits, candidates, provenance = {}, {}, [], {}
        for symbol in settings.SYMBOLS:
            progress(f"Loading {symbol}; training four directional/horizon models")
            bars, sha = load_bars(table, symbol, cutoff, count)
            bars = [b for b in bars if b["opened_at"] >= start and b["closed_at"] <= end]
            models, fitting, rows = search_asset(symbol, bars, policy, selection_start, test_start, progress, stop)
            artifacts[symbol], fits[symbol] = models, fitting
            provenance[symbol] = {"sha256": sha, "candles": len(bars), "source": source}
            candidates.extend(rows)
            del bars
        ranked = rank_candidates(candidates, policy["min_validation_trades"], cfg["max_drawdown_pct"])
        by_asset = {symbol: next((r for r in ranked if r["symbol"] == symbol), None) for symbol in settings.SYMBOLS}
        champion = ranked[0] if ranked else None
        holdout, allocated = None, None
        if champion:
            progress("Evaluating the fixed winner on the final historical period; no re-ranking")
            symbol, candidate = champion["symbol"], champion["candidate"]
            bars, _ = load_bars(table, symbol, cutoff, count)
            bars = [b for b in bars if b["opened_at"] >= start and b["closed_at"] <= end]
            vectors = price_features(bars)
            predictions = {}
            if candidate["family"] == "ml":
                model_key = candidate["model_key"]
                predictions[model_key] = [(predict_vector(artifacts[symbol][model_key], v) or {}).get("probability", -1) for v in vectors]
            signals = signal_series(bars, candidate, predictions, vectors)
            holdout = backtest(bars, signals, candidate, cfg, test_start, end)
            qualified = rank_candidates([champion | {"metrics": holdout}], policy["min_validation_trades"], cfg["max_drawdown_pct"])
            if champion["metrics"]["return_pct"] > 0 and holdout["return_pct"] > 0 and qualified:
                allocated = symbol
        report = {"version": AUTO_VERSION, "source": source, "provenance": provenance, "fitting": fits,
            "train_start": start, "selection_start": selection_start, "test_start": test_start, "test_end": end,
            "candidate_count": len(candidates), "eligible_count": len(ranked), "candidates": candidates,
            "leaders": ranked[:24], "by_asset": by_asset, "champion": champion, "holdout": holdout,
            "allocated_symbol": allocated, "allocation_reason": "Winner passed the predefined historical checks; future paper evidence is still required." if allocated else "Cash: no candidate passed positive validation and holdout returns, trade-count and drawdown checks. Asset challengers continue in separate virtual portfolios.",
            "method": "Training uses the first 60%; C is selected on the next 20%. Twelve ML/rule strategies per asset are compared at every integer leverage from 1 to the configured cap. Highest validation net return wins among candidates meeting trade-count, drawdown and zero-liquidation limits; ties favor lower drawdown then lower leverage. Only that fixed winner is evaluated on the last 20%. No fallback candidate is selected using holdout results.",
            "assumptions": "Historical candle execution: next-open fills, fees/spread/adverse slippage, isolated margin, fixed maintenance-margin rate, and adverse funding accrual on entry notional for both long and short. Liquidation takes priority when touched within an ambiguous candle, then stop before take profit. Candle prices proxy historical mark prices; exchange tiers, ADL and historical depth are not reconstructed. Spot history also omits the futures basis.",
            "forward_method": "Four independent virtual challenger portfolios start after model freezing. They use recorded live Bybit futures depth and mark prices. Funding remains the declared adverse accrual allowance, not actual settled funding. The designated capital stance uses one preselected portfolio or cash; challenger balances are not combined as an allocated account.",
            "evidence": "Historical windows may overlap previous research and are not fresh evidence on every retraining. Only the period after each cycle is frozen is its prospective test. Past returns cannot guarantee the most profitable future strategy. No real exchange orders are enabled."}
        cycle.artifacts, cycle.report = artifacts, report
        current_policy = AutonomyPolicy.objects.get(pk=1)
        cycle.status = "ready" if current_policy.enabled and current_policy.config == policy else "cancelled"
        cycle.save(update_fields=["artifacts", "report", "status"])
        if cycle.status == "ready":
            AutonomyPolicy.objects.filter(pk=1, enabled=True, config=policy).update(last_cutoff=cutoff, status="Research complete; waiting for forward paper activation")
        log.info("auto_search_completed %s", json.dumps({"cycle": str(cycle.id), "candidates": len(candidates),
            "source": source, "champion": champion, "holdout": holdout, "allocated_symbol": allocated}))
        return {"cycle_id": str(cycle.id), "candidate_count": len(candidates), "allocated_symbol": allocated}


def summary(full=True):
    from .margin import metrics
    policy = AutonomyPolicy.objects.filter(pk=1).first()
    if not policy:
        return {"enabled": False, "config": validate_auto(), "status": "Not enabled", "cycle": None, "latest": None}
    active = AutoCycle.objects.defer("artifacts", "report").filter(status__in=["paper", "closing"]).order_by("created_at").first()
    data = {"enabled": policy.enabled, "config": policy.config, "status": policy.status,
        "next_run_at": policy.next_run_at, "cycle": None}
    if active:
        cfg = margin_config(active.config)
        data["cycle"] = {"id": str(active.id), "status": active.status, "forward_start": active.forward_start,
            "forward_end": active.forward_end, "selected_symbol": active.state.get("allocated_symbol"),
            "portfolios": {s: {"candidate": p["candidate"], "metrics": metrics(p["wallet"], cfg),
                "signal": p.get("signal"), "book_at": p["market"].get("book_at"), "mark_at": p["market"].get("mark_at")} for s, p in active.state.get("portfolios", {}).items()},
            "last_event_id": active.last_event_id, "processed_at": active.state.get("processed_at")}
    if full:
        latest = AutoCycle.objects.defer("artifacts", "state").order_by("created_at").last()
        data["latest"] = {"id": str(latest.id), "status": latest.status, "cutoff": latest.cutoff,
            "error": latest.error, "report": {k: v for k, v in latest.report.items() if k != "candidates"}} if latest else None
        data["records"] = list(AutoRecord.objects.filter(cycle=active).order_by("-id").values("symbol", "kind", "at", "payload")[:30]) if active else []
        data["history"] = [{"id": str(c.id), "status": c.status, "start": c.forward_start, "end": c.finished_at,
            "result": c.state.get("final_results", {}), "selected_symbol": c.state.get("allocated_symbol")}
            for c in AutoCycle.objects.defer("report", "artifacts").filter(status="completed").order_by("-created_at")[:8]]
    return data
