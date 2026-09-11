from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .configuration import dec, fingerprint, validate_config
from .engine import initial_state, metrics, step
from .models import Decision, Event, Fill, Run
from .recording import stamp
from .research import as_event


def create_paper(owner, symbol, raw_config, validation=None):
    config = validate_config(raw_config)
    digest = fingerprint(config, settings.OPENAI_MODEL)
    if validation and (validation.owner_id != owner.id or validation.symbol != symbol or validation.config_hash != digest or validation.status != "completed"):
        raise ValueError("Validation must be your completed research run with identical symbol, model and settings")
    if Run.objects.filter(owner=owner, mode="paper", status="running").count() >= 10:
        raise ValueError("At most ten paper experiments can run at once")
    last = Event.objects.order_by("-id").first()
    state = initial_state(config)
    state["last_id"] = last.id if last else 0
    return Run.objects.create(owner=owner, name=f"{symbol} forward comparison", symbol=symbol, mode="paper",
        config=config, config_hash=digest, state=state, results=metrics(state, config), validation=validation, last_event_id=state["last_id"])


def persist_audit(run, decisions, fills):
    Decision.objects.bulk_create([Decision(run=run, **{**d, "at": stamp(d["at"])}) for d in decisions], ignore_conflicts=True)
    Fill.objects.bulk_create([Fill(run=run, **{**f, "at": stamp(f["at"])}) for f in fills])


def process_run(run_id):
    with transaction.atomic():
        run = Run.objects.select_for_update().get(pk=run_id)
        if run.status not in ("running", "reconciling"):
            return
        rows = list(Event.objects.filter(symbol=run.symbol, id__gt=run.last_event_id).exclude(kind="ai_attempt").order_by("id")[:1500])
        decisions, fills = [], []
        now = timezone.now().timestamp()
        for row in rows:
            ds, fs = step(run.state, as_event(row), run.config, paused=run.entries_paused or run.status == "reconciling",
                          flatten=run.flatten_requested, execution_now=now, execute=run.mode == "paper")
            decisions.extend(ds)
            fills.extend(fs)
            if run.mode == "paper" and row.kind == "candle" and 0 <= now-row.available_at.timestamp() <= 30:
                ai_decision = next((d for d in ds if d["strategy"] == "ai_trend"), None)
                if ai_decision and not ai_decision["features"].get("missing", ["unavailable"]):
                    evidence = run.state.setdefault("evidence", {"fresh_bars": 0, "days": []})
                    evidence["fresh_bars"] += 1
                    day = str(int(now//86400))
                    if day not in evidence["days"]:
                        evidence["days"].append(day)
            run.last_event_id = row.id
        if run.mode == "paper" and run.flatten_requested and all(dec(w["qty"]) == 0 for w in run.state["wallets"].values()):
            run.status, run.ended_at = "stopped", timezone.now()
        run.results = metrics(run.state, run.config)
        persist_audit(run, decisions, fills)
        run.save(update_fields=["state", "results", "last_event_id", "status", "ended_at"])


def readiness(paper, include_environment=True):
    checks = []

    def check(label, ok, detail):
        checks.append({"label": label, "passed": bool(ok), "detail": detail})

    scores = metrics(paper.state, paper.config) if paper.state.get("wallets") else {}
    ai, baseline = scores.get("ai_trend", {}), scores.get("trend", {})
    days = ((paper.ended_at or timezone.now())-paper.started_at).total_seconds()/86400
    check("Forward evidence", paper.mode == "paper" and days >= settings.LIVE_MIN_PAPER_DAYS,
          f"{days:.1f} / {settings.LIVE_MIN_PAPER_DAYS} days since paper start")
    evidence = paper.state.get("evidence", {})
    required_bars = int(settings.LIVE_MIN_PAPER_DAYS*1440*.8)
    check("Observed forward coverage", evidence.get("fresh_bars", 0) >= required_bars and len(evidence.get("days", [])) >= settings.LIVE_MIN_PAPER_DAYS,
          f"{evidence.get('fresh_bars', 0)} / {required_bars} fresh bars with complete AI context, across at least {settings.LIVE_MIN_PAPER_DAYS} UTC days")
    check("Completed paper run", paper.status == "stopped" and ai.get("open_quantity", 1) == 0,
          "Stop and flatten the experiment to freeze its evidence")
    check("Closed AI trades", ai.get("closed_trades", 0) >= settings.LIVE_MIN_CLOSED_TRADES,
          f"{ai.get('closed_trades', 0)} / {settings.LIVE_MIN_CLOSED_TRADES} closed trades")
    check("Net performance", ai.get("return_pct", -1) > 0 and ai.get("return_pct", -1) > baseline.get("return_pct", 0),
          "AI must be positive after costs and outperform the paired trend baseline")
    check("Drawdown", ai.get("max_drawdown_pct", 100) < paper.config["max_drawdown_pct"] and not ai.get("halted"),
          "AI drawdown must remain below its configured limit")
    validation = paper.validation
    held = validation.results.get("holdout", {}).get("metrics", {}).get("ai_trend", {}) if validation else {}
    check("Prior unseen validation", validation and validation.mode == "replay" and validation.ended_at <= paper.started_at
          and validation.config_hash == paper.config_hash and held.get("return_pct", -1) > 0 and held.get("closed_trades", 0) >= 5,
          "A prior, matching event replay with positive AI holdout and at least five closed holdout trades is required")
    check("Frozen configuration", paper.config_hash == fingerprint(paper.config, settings.OPENAI_MODEL),
          "Model, prompt, engine and risk parameters must match the evaluated version")
    if include_environment:
        check("Operator enablement", settings.LIVE_TRADING_ENABLED, "LIVE_TRADING_ENABLED is an explicit server-side switch")
        check("Dedicated account", settings.LIVE_ACCOUNT_DEDICATED and str(paper.owner_id) == settings.LIVE_OWNER_ID,
              "Set a dedicated exchange account and its single LIVE_OWNER_ID")
        check("Exchange credentials", settings.BINANCE_API_KEY and settings.BINANCE_API_SECRET,
              "Exchange keys are loaded only from the server environment")
        check("Withdrawal permissions", settings.LIVE_WITHDRAWALS_DISABLED_CONFIRMED,
              "Disable withdrawals; production also verifies the key permissions with Binance")
        check("Capital cap", 0 < dec(paper.config["capital"]) <= dec(settings.LIVE_MAX_CAPITAL),
              "Paper capital must fit the explicitly configured LIVE_MAX_CAPITAL")
    return {"eligible": all(c["passed"] for c in checks), "checks": checks,
            "note": "These are minimum operational gates, not proof of future profitability."}
