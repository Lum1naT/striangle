"""CPU training on immutable candle snapshots, isolated from order processing."""
import hashlib
import json
import logging
import warnings

from django.utils import timezone

from .configuration import ENGINE_VERSION, validate_config
from .ml import FEATURE_NAMES, FEATURE_VERSION, HORIZON, LOOKBACK, MODEL_VERSION, feature_vector
from .models import Candle, MarketModel
from .recording import stamp

MIN_CANDLES = 5000
MAX_CANDLES = 100000


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def samples(bars, config):
    """A label buys at the next open and exits 15 minutes later, after both costs."""
    fee, slip, spread = (config[k]/10000 for k in ("fee_bps", "slippage_bps", "spread_bps"))
    factor = (1-spread/2)*(1-slip)*(1-fee)/((1+spread/2)*(1+slip)*(1+fee))
    xs, ys, times, ends = [], [], [], []
    closes = [float(b["close"]) for b in bars]
    for i in range(LOOKBACK-1, len(bars)-HORIZON-1):
        first, last = bars[i-LOOKBACK+1], bars[i+HORIZON+1]
        # Strict ordering is checked by fit_model; equality implies no missing bars.
        if last["opened_at"]-first["opened_at"] != (LOOKBACK+HORIZON)*60:
            continue
        values = feature_vector(closes[i-LOOKBACK+1:i+1])
        if values is None:
            continue
        net = float(last["open"])/float(bars[i+1]["open"])*factor-1
        xs.append(values)
        ys.append(int(net > 0))
        times.append(bars[i]["closed_at"])
        ends.append(last["opened_at"])
    return xs, ys, times, ends


def fit_model(bars, config, progress=lambda stage: None):
    # Only the research process imports scientific libraries. The web/trader
    # load the exported numeric coefficients with the standard library.
    import numpy as np
    import sklearn
    from sklearn.exceptions import ConvergenceWarning
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import brier_score_loss, log_loss, roc_auc_score
    from sklearn.preprocessing import StandardScaler
    from threadpoolctl import threadpool_limits
    from .research import bar_test

    if not MIN_CANDLES <= len(bars) <= MAX_CANDLES:
        raise ValueError("Training requires 5,000–100,000 completed candles per asset")
    if any(b["opened_at"] <= a["opened_at"] for a, b in zip(bars, bars[1:])):
        raise ValueError("Training candles must be unique and strictly chronological")
    if any(b["closed_at"]-b["opened_at"] != 60 for b in bars):
        raise ValueError("Training requires completed 1-minute candles")
    val_index, test_index = int(len(bars)*.6), int(len(bars)*.8)
    val_start, test_start = bars[val_index]["opened_at"], bars[test_index]["opened_at"]
    progress("Building causal price features")
    xs, ys, times, ends = samples(bars, config)
    x, y, t, label_end = np.asarray(xs), np.asarray(ys), np.asarray(times), np.asarray(ends)
    del xs, ys, times, ends
    masks = {"train": label_end < val_start,
             "validation": (t >= val_start) & (label_end < test_start), "test": t >= test_start}
    if any(int(mask.sum()) < 500 for mask in masks.values()):
        raise ValueError("Each chronological partition needs 500 valid samples after gap filtering and label purging")
    train, validation, test = (masks[k] for k in ("train", "validation", "test"))
    if len(np.unique(y[train])) != 2:
        raise ValueError("Training needs both cost-covering and non-covering moves")
    scaler = StandardScaler().fit(x[train])
    scaled = scaler.transform(x)
    candidates = []
    progress("Fitting three regularized candidates; selecting on validation only")
    with threadpool_limits(limits=1), warnings.catch_warnings():
        warnings.simplefilter("error", ConvergenceWarning)
        for strength in (.01, .1, 1.0):
            fitted = LogisticRegression(C=strength, solver="lbfgs", l1_ratio=0, max_iter=500, tol=1e-7).fit(scaled[train], y[train])
            probability = fitted.predict_proba(scaled[validation])[:, 1]
            candidates.append((float(log_loss(y[validation], probability, labels=[0, 1])), strength, fitted))
    candidates.sort(key=lambda c: (c[0], c[1]))
    selected = candidates[0][2]
    artifact = {"algorithm": MODEL_VERSION, "feature_version": FEATURE_VERSION, "features": FEATURE_NAMES,
        "mean": scaler.mean_.tolist(), "scale": scaler.scale_.tolist(), "weights": selected.coef_[0].tolist(),
        "intercept": float(selected.intercept_[0]), "C": candidates[0][1], "threshold": .55,
        "horizon_minutes": HORIZON, "config": config, "engine_version": ENGINE_VERSION,
        "expires_at": bars[-1]["closed_at"]+7*86400, "sklearn_version": sklearn.__version__}

    def classification(mask):
        p = selected.predict_proba(scaled[mask])[:, 1]
        truth = y[mask]
        prior = float(y[train].mean())
        constant = np.full(len(truth), prior)
        return {"samples": int(mask.sum()), "positive_rate": float(truth.mean()),
            "log_loss": float(log_loss(truth, p, labels=[0, 1])),
            "constant_log_loss": float(log_loss(truth, constant, labels=[0, 1])),
            "brier_score": float(brier_score_loss(truth, p)),
            "constant_brier_score": float(brier_score_loss(truth, constant)),
            "auc": float(roc_auc_score(truth, p)) if len(np.unique(truth)) == 2 else None,
            "predicted_entries": int((p >= artifact["threshold"]).sum())}

    progress("Evaluating frozen model on final held-out candles, including costs")
    # No refit on validation/test. All baseline settings and the 55% threshold
    # are specified before viewing test results. Fresh portfolio for each period.
    evaluated = bar_test(bars[test_index:], config, model=artifact,
        warmup=bars[max(0, test_index-201):test_index])
    partitions = {name: {"samples": int(mask.sum()), "decision_start": float(t[mask][0]),
        "decision_end": float(t[mask][-1]), "last_label_at": float(label_end[mask][-1])} for name, mask in masks.items()}
    scores = evaluated["metrics"]
    edge = scores["ml"]["return_pct"] > max(0, scores["trend"]["return_pct"], scores["rsi"]["return_pct"])
    report = {"candles": len(bars), "valid_samples": len(y), "partitions": partitions,
        "data_start": bars[0]["opened_at"], "data_end": bars[-1]["closed_at"],
        "validation": classification(validation), "test": classification(test),
        "candidates": [{"C": c, "validation_log_loss": loss} for loss, c, _ in candidates],
        "selection": "60% fit / 20% validation / 20% test. Overlapping outcome labels purged at boundaries. Scaler and weights fit on training only; C selected by validation log loss; entry threshold fixed at 55%. No refit after test.",
        "label": "Next-open long return over 15 minutes exceeds fees, spread and adverse slippage on both sides. Protective exits can change the actual trade outcome.",
        "holdout": {"metrics": scores, "curve": evaluated["curve"], "assumptions": evaluated["assumptions"]},
        "assessment": "Positive candle result above both baselines; forward evidence still required." if edge else "No demonstrated edge over both baselines after costs; collect forward paper evidence.",
        "scope": "Price features only. Historical candles are assumed available at close for this offline benchmark. Import receipt times are retained in snapshot provenance. No historical news, order books, flow, funding or liquidations are reconstructed. Live paper adds fresh depth, flow, funding and liquidation filters, plus an adverse-news veto when configured; its results can differ.",
        "caution": "Overlapping samples are not independent trials. Repeated training after viewing this test contaminates it. Future paper data is the next unseen evaluation. Models are paper-only."}
    return artifact, report


def train_symbol(symbol, count=100000, *, config=None, cutoff=None, progress=lambda stage: None):
    from django.conf import settings
    from .locking import single_worker
    if symbol not in settings.SYMBOLS:
        raise ValueError("Choose a configured asset")
    if type(count) is not int or not MIN_CANDLES <= count <= MAX_CANDLES:
        raise ValueError("Training requires 5,000–100,000 candles")
    cutoff = cutoff or timezone.now()
    if cutoff > timezone.now():
        raise ValueError("Training cutoff must be in the past")
    config = validate_config(config)
    # Serializes CLI and queued training on the same CPU budget. Does not hold a
    # database transaction open while fitting or block market/order workers.
    with single_worker("model-training"):
        progress("Loading a fixed snapshot of recorded history")
        query = Candle.objects.filter(symbol=symbol, interval="1m", fetched_at__lte=cutoff,
            closed_at__lte=cutoff).order_by("-opened_at").values("payload", "fetched_at")[:count]
        bars, source_hash = [], hashlib.sha256()
        for row in query.iterator(chunk_size=2000):
            source_hash.update(json.dumps([row["payload"], row["fetched_at"].isoformat()], sort_keys=True).encode())
            bars.append({k: float(v) for k, v in row["payload"].items() if k in ("opened_at", "closed_at", "open", "high", "low", "close")})
        bars.reverse()
        artifact, report = fit_model(bars, config, progress)
        provenance = {"symbol": symbol, "cutoff": cutoff.timestamp(), "data_sha256": source_hash.hexdigest()}
        artifact.update(provenance)
        artifact["version"] = digest(artifact)
        report["provenance"] = provenance
        model = MarketModel.objects.create(symbol=symbol, version=artifact["version"], artifact=artifact,
            report=report, data_end=stamp(bars[-1]["closed_at"]))
        logging.getLogger(__name__).info("market_model_saved %s", json.dumps({"symbol": symbol,
            "version": model.version, "candles": len(bars), "test": report["test"],
            "holdout_metrics": report["holdout"]["metrics"], "assessment": report["assessment"]}))
        progress("Model saved for new paper runs")
        return model


def model_summary(model):
    report = model.report | {"holdout": {k: v for k, v in model.report["holdout"].items() if k != "curve"}}
    return {"id": str(model.id), "symbol": model.symbol, "version": model.version,
        "created_at": model.created_at, "data_end": model.data_end,
        "expires_at": model.artifact["expires_at"], "threshold": model.artifact["threshold"],
        "horizon_minutes": model.artifact["horizon_minutes"], "config": model.artifact["config"],
        "report": report}
