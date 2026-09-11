"""Shared, closed-candle strategy rules for search and prospective paper tests."""
from .ml import feature_vector, predict_vector


def strategies():
    rows = []
    for horizon in (15, 60):
        for side in (1, -1):
            for threshold in (.35, .55):
                key = f"ml-{horizon}-{'long' if side == 1 else 'short'}-{int(threshold*100)}"
                rows.append({"id": key, "family": "ml", "horizon": horizon, "side": side,
                    "threshold": threshold, "model_key": f"{horizon}:{side}",
                    "name": f"ML {'long' if side == 1 else 'short'} · {horizon} min · {threshold:.0%}"})
    for fast, slow in ((10, 30), (20, 60)):
        rows.append({"id": f"trend-{fast}-{slow}", "family": "trend", "fast": fast, "slow": slow,
            "horizon": 60, "name": f"Trend {fast}/{slow} · long/short"})
    rows.extend([{"id": "rsi-14", "family": "rsi", "horizon": 60, "name": "RSI 14 · long/short"},
        {"id": "breakout-20", "family": "breakout", "horizon": 60, "name": "20-minute breakout · long/short"}])
    return rows


def signal(candidate, closes, artifacts):
    values = feature_vector(closes)
    if values is None:
        return {"enter": 0, "exit_long": False, "exit_short": False, "reason": "Waiting for 61 consecutive completed candles."}
    side, extra = 0, {}
    family = candidate["family"]
    if family == "ml":
        result = predict_vector(artifacts[candidate["model_key"]] | {"threshold": candidate["threshold"]}, values)
        p = result["probability"]
        side = candidate["side"] if p >= candidate["threshold"] else 0
        reason = f"{candidate['name']}: estimated cost-covering probability {p:.1%}, threshold {candidate['threshold']:.0%}."
        extra = {"probability": p, "drivers": result["drivers"], "inputs": result["inputs"], "model_version": result["model_version"]}
    elif family == "trend":
        fast = sum(closes[-candidate["fast"]:])/candidate["fast"]
        slow = sum(closes[-candidate["slow"]:])/candidate["slow"]
        side = 1 if fast > slow else -1 if fast < slow else 0
        reason = f"{candidate['name']}: fast average {fast:.6g}, slow average {slow:.6g}."
    elif family == "rsi":
        value = (values[-1]+1)*50
        side = 1 if value <= 30 else -1 if value >= 70 else 0
        return {"enter": side, "exit_long": value >= 50, "exit_short": value <= 50, "reason": f"RSI is {value:.1f}; entries at 30/70 and exits at 50."}
    elif family == "breakout":
        high, low = max(closes[-21:-1]), min(closes[-21:-1])
        side = 1 if closes[-1] > high else -1 if closes[-1] < low else 0
        reason = f"Close {closes[-1]:.6g}; prior 20-minute close range {low:.6g}–{high:.6g}."
    else:
        raise ValueError("Unsupported strategy family")
    return {"enter": side, "exit_long": side == -1, "exit_short": side == 1, "reason": reason, **extra}
