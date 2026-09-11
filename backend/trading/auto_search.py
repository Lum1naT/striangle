"""Bounded chronological strategy search. Final holdout never ranks candidates."""
import hashlib
import json
import warnings

from .auto_config import AUTO_VERSION, margin_config
from .auto_signals import strategies
from .margin import accrue_funding, close_position, entry_quantity, liquidatable, mark, metrics, new_wallet, open_position
from .ml import FEATURE_NAMES, FEATURE_VERSION, MODEL_VERSION, feature_vector


def price_features(bars):
    closes, previous, vectors = [], None, []
    for bar in bars:
        if previous is not None and bar["opened_at"]-previous != 60:
            closes = []
        previous = bar["opened_at"]
        closes = (closes+[bar["close"]])[-61:]
        vectors.append(feature_vector(closes))
    return vectors


def fit_predictors(bars, config, selection_start, test_start):
    import numpy as np
    import sklearn
    from sklearn.exceptions import ConvergenceWarning
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import log_loss
    from sklearn.preprocessing import StandardScaler
    from threadpoolctl import threadpool_limits

    vectors = price_features(bars)
    x = np.asarray([row if row is not None else [float("nan")]*10 for row in vectors], dtype=float)
    valid = np.isfinite(x).all(axis=1)
    times = np.asarray([b["opened_at"] for b in bars])
    opens = np.asarray([b["open"] for b in bars])
    fee, slip, half = config["fee_bps"]/10000, config["slippage_bps"]/10000, config["spread_bps"]/20000
    artifacts, predictions, reports = {}, {}, {}
    for horizon in (15, 60):
        n = len(bars)-horizon-1
        indices = np.arange(n)
        label_end = times[indices+horizon+1]
        known = valid[:n] & (label_end-times[:n] == (horizon+1)*60)
        train = known & (label_end < selection_start)
        validation = known & (times[:n]+60 >= selection_start) & (label_end < test_start)
        if min(int(train.sum()), int(validation.sum())) < 500:
            raise ValueError("Not enough consecutive data for purged training and validation")
        with threadpool_limits(limits=1), warnings.catch_warnings():
            warnings.simplefilter("error", ConvergenceWarning)
            scaler = StandardScaler().fit(x[:n][train])
            scaled = scaler.transform(np.nan_to_num(x))
            entry, exit_price = opens[indices+1], opens[indices+horizon+1]
            allowance = entry*config["funding_bps_8h"]/10000*horizon/480
            for side in (1, -1):
                if side == 1:
                    net = exit_price*(1-half)*(1-slip)*(1-fee)-entry*(1+half)*(1+slip)*(1+fee)-allowance
                else:
                    net = entry*(1-half)*(1-slip)*(1-fee)-exit_price*(1+half)*(1+slip)*(1+fee)-allowance
                y = (net > 0).astype(int)
                if len(np.unique(y[train])) < 2:
                    raise ValueError("Both target classes are required for autonomous model fitting")
                ranked = []
                for strength in (.01, .1, 1):
                    fitted = LogisticRegression(C=strength, l1_ratio=0, solver="lbfgs", max_iter=500, tol=1e-7).fit(scaled[:n][train], y[train])
                    loss = float(log_loss(y[validation], fitted.predict_proba(scaled[:n][validation])[:, 1], labels=[0, 1]))
                    ranked.append((loss, strength, fitted))
                ranked.sort(key=lambda item: (item[0], item[1]))
                loss, strength, model = ranked[0]
                key = f"{horizon}:{side}"
                artifact = {"algorithm": MODEL_VERSION, "feature_version": FEATURE_VERSION, "features": FEATURE_NAMES,
                    "mean": scaler.mean_.tolist(), "scale": scaler.scale_.tolist(), "weights": model.coef_[0].tolist(),
                    "intercept": float(model.intercept_[0]), "C": strength, "threshold": .55,
                    "horizon_minutes": horizon, "direction": side, "sklearn_version": sklearn.__version__, "auto_version": AUTO_VERSION}
                artifact["version"] = hashlib.sha256(json.dumps(artifact, sort_keys=True).encode()).hexdigest()
                artifacts[key] = artifact
                predictions[key] = np.where(valid, model.predict_proba(scaled)[:, 1], -1).tolist()
                reports[key] = {"train_samples": int(train.sum()), "validation_samples": int(validation.sum()),
                    "last_training_label": float(label_end[train][-1]), "last_validation_label": float(label_end[validation][-1]),
                    "C": strength, "validation_log_loss": loss,
                    "candidates": [{"C": c, "validation_log_loss": score} for score, c, _ in ranked]}
    return artifacts, predictions, reports, vectors


def signal_series(bars, candidate, predictions, vectors):
    # Precompute once per strategy, then share across all tested leverage levels.
    entries, exits_long, exits_short = [], [], []
    closes = [b["close"] for b in bars]
    for i, values in enumerate(vectors):
        side, exit_long, exit_short = 0, False, False
        if values is not None:
            family = candidate["family"]
            if family == "ml":
                side = candidate["side"] if predictions[candidate["model_key"]][i] >= candidate["threshold"] else 0
            elif family == "trend":
                fast, slow = candidate["fast"], candidate["slow"]
                a, b = sum(closes[i-fast+1:i+1])/fast, sum(closes[i-slow+1:i+1])/slow
                side = 1 if a > b else -1 if a < b else 0
            elif family == "rsi":
                rsi = (values[-1]+1)*50
                side, exit_long, exit_short = (1 if rsi <= 30 else -1 if rsi >= 70 else 0), rsi >= 50, rsi <= 50
            else:
                side = 1 if closes[i] > max(closes[i-20:i]) else -1 if closes[i] < min(closes[i-20:i]) else 0
            if family != "rsi":
                exit_long, exit_short = side == -1, side == 1
        entries.append(side); exits_long.append(exit_long); exits_short.append(exit_short)
    return entries, exits_long, exits_short


def backtest(bars, signals, candidate, cfg, start, end):
    """Spot/futures candle price proxy, next-open fills, conservative liquidation."""
    wallet = new_wallet(cfg["capital"])
    pending, previous, last_bar = 0, None, None
    half, slip = cfg["spread_bps"]/20000, cfg["slippage_bps"]/10000
    entries, exits_long, exits_short = signals

    def execution(price, side):
        return price*(1+side*half)*(1+side*slip)

    def close(price, at, liquidated=False):
        return close_position(wallet, execution(price, -wallet["side"]), at, cfg, liquidated=liquidated)

    for i, bar in enumerate(bars):
        at = bar["opened_at"]
        if at < start:
            continue
        if bar["closed_at"] > end:
            break
        if previous is not None and at-previous != 60:
            pending = 0
        previous, last_bar = at, bar
        exited = False
        accrue_funding(wallet, at, cfg)
        if wallet["quantity"]:
            side = wallet["side"]
            move = side*(bar["open"]/wallet["entry"]-1)
            if liquidatable(wallet, bar["open"], cfg):
                close(bar["open"], at, True); exited = True
            elif move <= -cfg["stop_pct"]/100 or move >= cfg["take_pct"]/100 or pending == 2:
                close(bar["open"], at); exited = True
        if not exited and not wallet["quantity"] and pending in (-1, 1) and not wallet["halted"] and bar["closed_at"] < end:
            price = execution(bar["open"], pending)
            qty = entry_quantity(wallet, price, candidate["leverage"], cfg)
            open_position(wallet, pending, qty, price, candidate["leverage"], at, cfg)
        pending = 0
        accrue_funding(wallet, bar["closed_at"], cfg)
        if wallet["quantity"]:
            side = wallet["side"]
            worst, best = (bar["low"], bar["high"]) if side == 1 else (bar["high"], bar["low"])
            if liquidatable(wallet, worst, cfg):
                close(worst, bar["closed_at"], True); exited = True
            elif side*(worst/wallet["entry"]-1) <= -cfg["stop_pct"]/100:
                close(wallet["entry"]*(1-side*cfg["stop_pct"]/100), bar["closed_at"]); exited = True
            elif side*(best/wallet["entry"]-1) >= cfg["take_pct"]/100:
                close(wallet["entry"]*(1+side*cfg["take_pct"]/100), bar["closed_at"]); exited = True
        mark(wallet, bar["close"], bar["closed_at"], cfg)
        if wallet["quantity"]:
            timed = bar["closed_at"]-wallet["entry_at"] >= candidate["horizon"]*60
            reverse = exits_long[i] if wallet["side"] == 1 else exits_short[i]
            if timed or reverse or wallet["halted"]:
                pending = 2
        elif not exited and not wallet["halted"]:
            pending = entries[i]
    if last_bar and wallet["quantity"]:
        close(last_bar["close"], last_bar["closed_at"])
    return metrics(wallet, cfg)


def rank_candidates(rows, minimum_trades, max_drawdown):
    eligible = [r for r in rows if r["metrics"]["closed_trades"] >= minimum_trades
        and r["metrics"]["liquidations"] == 0 and r["metrics"]["max_drawdown_pct"] < max_drawdown and not r["metrics"]["halted"]]
    return sorted(eligible, key=lambda r: (-round(r["metrics"]["return_pct"], 8), round(r["metrics"]["max_drawdown_pct"], 8), r["candidate"]["leverage"], r["symbol"], r["candidate"]["id"]))


def search_asset(symbol, bars, policy, selection_start, test_start, progress=lambda text: None, stop=None):
    cfg = margin_config(policy)
    artifacts, predictions, fitting, vectors = fit_predictors(bars, cfg, selection_start, test_start)
    rows = []
    for candidate in strategies():
        if stop is not None and stop.is_set():
            raise InterruptedError("Research worker is stopping")
        progress(f"{symbol}: testing {candidate['name']} at 1–{policy['max_leverage']}x")
        signals = signal_series(bars, candidate, predictions, vectors)
        for leverage in range(1, policy["max_leverage"]+1):
            proposal = candidate | {"leverage": leverage}
            scores = backtest(bars, signals, proposal, cfg, selection_start, test_start)
            rows.append({"symbol": symbol, "candidate": proposal, "metrics": scores})
    return artifacts, fitting, rows
