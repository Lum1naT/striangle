"""Versioned price features and cheap, deterministic inference from JSON weights.

No API calls, pickle loading, training dependencies or fitting in the trading loop.
"""
import math

FEATURE_VERSION = "price-v1"
MODEL_VERSION = "logistic-price-v1"
LOOKBACK = 61
HORIZON = 15
FEATURE_NAMES = ["return_1m", "return_5m", "return_15m", "return_30m", "return_60m",
                 "volatility_15m", "volatility_60m", "distance_sma_15m", "distance_sma_60m", "rsi_14m"]
COST_KEYS = ("fee_bps", "slippage_bps", "spread_bps")


def feature_vector(closes):
    if len(closes) < LOOKBACK:
        return None
    closes = closes[-LOOKBACK:]
    if any(not math.isfinite(p) or p <= 0 for p in closes):
        return None
    changes = [100 * math.log(b/a) for a, b in zip(closes, closes[1:])]
    values = [100 * math.log(closes[-1]/closes[-1-n]) for n in (1, 5, 15, 30, 60)]
    for n in (15, 60):
        window = changes[-n:]
        avg = sum(window)/n
        values.append(math.sqrt(sum((x-avg)**2 for x in window)/n))
    values.extend(100 * (closes[-1]/(sum(closes[-n:])/n)-1) for n in (15, 60))
    delta = [b-a for a, b in zip(closes[-15:-1], closes[-14:])]
    up, down = sum(max(d, 0) for d in delta), -sum(min(d, 0) for d in delta)
    values.append((up-down)/(up+down) if up+down else 0)
    return values


def predict_vector(artifact, values):
    if artifact.get("feature_version") != FEATURE_VERSION or artifact.get("algorithm") != MODEL_VERSION:
        return None
    if values is None:
        return None
    contributions = [(x-m)/s*w for x, m, s, w in zip(values, artifact["mean"], artifact["scale"], artifact["weights"], strict=True)]
    logit = artifact["intercept"] + sum(contributions)
    probability = 1/(1+math.exp(-max(-700, min(700, logit))))
    drivers = sorted(zip(FEATURE_NAMES, contributions), key=lambda item: -abs(item[1]))[:3]
    return {"probability": probability, "threshold": artifact["threshold"],
        "horizon_minutes": artifact["horizon_minutes"], "model_version": artifact.get("version"),
        "drivers": [{"feature": name, "log_odds_contribution": value} for name, value in drivers],
        "inputs": dict(zip(FEATURE_NAMES, values, strict=True))}


def prediction(market, at, config):
    artifact = market.get("ml_model")
    if not artifact:
        return {"unavailable": "Train a market model, then start a new paper run."}
    if at >= artifact["expires_at"]:
        return {"unavailable": "Model history is over seven days old; retrain and start a new paper run."}
    if any(config[k] != artifact["config"][k] for k in COST_KEYS):
        return {"unavailable": "Trading costs differ from this model's evaluation; retrain with these costs."}
    closed = market.get("last_closed_at")
    if closed is None or not 0 <= at-closed <= 90:
        return {"unavailable": "Waiting for a fresh, completed candle."}
    cache = market.get("ml_prediction", {})
    if cache.get("closed_at") == closed and cache.get("model_version") == artifact.get("version"):
        return cache
    result = predict_vector(artifact, feature_vector(market.get("closes", [])))
    if result is None:
        return {"unavailable": "Market model needs 61 consecutive closes and a supported feature version."}
    result["closed_at"] = closed
    market["ml_prediction"] = result
    return result


def model_signal(f, holding):
    if holding:
        return "hold", "Model position is open; the 15-minute horizon and protective exits remain active."
    model = f.get("ml", {})
    if model.get("unavailable") or "probability" not in model:
        return "hold", model.get("unavailable", "No trained market model is attached to this run.")
    p, threshold = model["probability"], model["threshold"]
    reason = f"Model estimates {p:.1%} chance of a cost-covering 15-minute move; entry threshold {threshold:.1%}."
    if p < threshold:
        return "hold", reason
    drivers = ", ".join(d["feature"].replace("_", " ") for d in model["drivers"])
    return "buy", reason + " Largest model contributions: " + drivers + "."
