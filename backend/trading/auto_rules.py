"""Portable entry/exit combinations. Only named numeric rules, never generated code."""
import hashlib
import json

from .auto_config import candidate_config
from .ml import feature_vector, predict_vector

RULE_VERSION = "entry-exit-v1"
ENTRY_KINDS = ("trend", "rsi", "breakout", "ml")
EXIT_KINDS = ("reverse", "rsi", "momentum", "confidence", "time")


def model_keys(candidate):
    if candidate and (candidate["family"] == "ml" or candidate["family"] == "combination"
            and (candidate["entry_kind"] == "ml" or candidate["exit_kind"] == "confidence")):
        return [candidate["model_key"]]
    return []


def describe(candidate):
    c = candidate
    direction = "Long" if c["side"] == 1 else "Short"
    above, below = ("above", "below") if c["side"] == 1 else ("below", "above")
    entries = {
        "trend": f"SMA {c['fast']} {above} SMA {c['slow']}",
        "rsi": f"RSI 14 {below} {c['rsi_entry']}",
        "breakout": f"close {above} the previous {c['lookback']}-close range",
        "ml": f"{c['model_horizon']}-minute model probability ≥ {c['threshold']:.0%}",
    }
    conditions = [entries[c["entry_kind"]]]
    if c["trend_filter"] and c["entry_kind"] != "trend":
        conditions.append(entries["trend"])
    if c["momentum_filter"]:
        conditions.append(f"5-minute return {'positive' if c['side'] == 1 else 'negative'}")
    if c["volatility_cap"]:
        conditions.append(f"15-minute return volatility ≤ {c['volatility_cap']:g}%")
    exits = {"reverse": "the primary entry rule signals the opposite direction",
        "rsi": f"RSI 14 {above} {c['rsi_exit']}",
        "momentum": "5-minute momentum turns against the position",
        "confidence": f"model probability drops below {c['exit_threshold']:.0%}",
        "time": "the holding limit is reached"}
    if c["entry_kind"] == "ml":
        exits["reverse"] = f"model probability drops below {c['exit_threshold']:.0%}"
    return (direction + " when " + " AND ".join(conditions) + ".",
        f"Exit when {exits[c['exit_kind']]}, or after {c['horizon']} minutes, "
        f"or at the {c['stop_pct']:g}% stop / {c['take_pct']:g}% profit target.")


def make_candidate(rng, cfg):
    """Sample a bounded hypothesis; normalize unused knobs before deduplication."""
    entry, exit_kind, side = rng.choice(ENTRY_KINDS), rng.choice(EXIT_KINDS), rng.choice((1, -1))
    c = {"family": "combination", "rule_version": RULE_VERSION, "entry_kind": entry, "exit_kind": exit_kind,
        "side": side, "fast": rng.choice((5, 10, 20)), "slow": rng.choice((30, 60)),
        "rsi_entry": rng.choice((20, 30, 40)), "rsi_exit": rng.choice((45, 50, 60)),
        "lookback": rng.choice((10, 20, 40)), "model_horizon": rng.choice((15, 60)),
        "threshold": rng.choice((.35, .45, .55, .65)), "exit_threshold": rng.choice((.25, .35, .45)),
        "trend_filter": rng.choice((False, True)), "momentum_filter": rng.choice((False, True)),
        "volatility_cap": rng.choice((0, .1, .2, .4)), "horizon": rng.choice((15, 30, 60, 120, 240)),
        "stop_pct": round(max(.1, cfg["stop_pct"]*rng.choice((.25, .5, .75, 1))), 8),
        "take_pct": round(max(.1, cfg["take_pct"]*rng.choice((.25, .5, .75, 1))), 8),
        "leverage": rng.randint(1, cfg["max_leverage"])}
    if side == -1:
        c["rsi_entry"], c["rsi_exit"] = 100-c["rsi_entry"], 100-c["rsi_exit"]
    if entry == "trend":
        c["trend_filter"] = False
    if entry != "trend" and not c["trend_filter"]:
        c.update(fast=10, slow=30)
    if entry != "rsi":
        c["rsi_entry"] = 30 if side == 1 else 70
    if exit_kind != "rsi":
        c["rsi_exit"] = 50
    if entry != "breakout":
        c["lookback"] = 20
    if entry != "ml":
        c["threshold"] = .55
    if exit_kind != "confidence":
        c["exit_threshold"] = .35
    if entry != "ml" and exit_kind != "confidence":
        c["model_horizon"] = 15
    c["model_key"] = f"{c['model_horizon']}:{side}"
    candidate_config(cfg, c)
    c["id"] = "combo-" + hashlib.sha256(json.dumps(c, sort_keys=True).encode()).hexdigest()[:16]
    c["name"] = f"{'Long' if side == 1 else 'Short'} {entry} / {exit_kind} · {c['horizon']} min"
    c["entry_description"], c["exit_description"] = describe(c)
    return c


def encoding(c):
    """One-hot categories and numeric parameters for the research-only surrogate."""
    return {**{f"entry_{k}": int(c["entry_kind"] == k) for k in ENTRY_KINDS},
        **{f"exit_{k}": int(c["exit_kind"] == k) for k in EXIT_KINDS},
        **{k: float(c[k]) for k in ("side", "fast", "slow", "rsi_entry", "rsi_exit", "lookback", "model_horizon",
            "threshold", "exit_threshold", "trend_filter", "momentum_filter", "volatility_cap", "horizon", "stop_pct", "take_pct", "leverage")}}


def evaluate(candidate, closes, values, probability=None, *, explain=False):
    """The exact same completed-candle predicate is used in research and execution."""
    c, side = candidate, candidate["side"]
    if c.get("rule_version") != RULE_VERSION:
        raise ValueError("Unsupported entry/exit rule version")
    if values is None:
        return {"enter": 0, "exit_long": False, "exit_short": False,
            "reason": "Waiting for 61 consecutive completed candles."}
    rsi, momentum, volatility = (values[-1]+1)*50, values[1], values[5]
    fast, slow = sum(closes[-c["fast"]:])/c["fast"], sum(closes[-c["slow"]:])/c["slow"]
    trend = 1 if fast > slow else -1 if fast < slow else 0
    entry = c["entry_kind"]
    if entry == "trend":
        primary, reverse = trend == side, trend == -side
    elif entry == "rsi":
        primary = side*(rsi-c["rsi_entry"]) <= 0
        reverse = side*(rsi-(100-c["rsi_entry"])) >= 0
    elif entry == "breakout":
        high, low = max(closes[-c["lookback"]-1:-1]), min(closes[-c["lookback"]-1:-1])
        direction = 1 if closes[-1] > high else -1 if closes[-1] < low else 0
        primary, reverse = direction == side, direction == -side
    elif entry == "ml":
        primary = probability is not None and probability >= c["threshold"]
        # A directional classifier falling below its entry threshold does not
        # imply an opposite-direction prediction; confidence is a separate exit.
        reverse = probability is not None and probability < c["exit_threshold"]
    else:
        raise ValueError("Unsupported primary entry rule")
    checks = {"primary_entry": primary, "trend_filter": not c["trend_filter"] or trend == side,
        "momentum_filter": not c["momentum_filter"] or side*momentum > 0,
        "volatility_filter": not c["volatility_cap"] or volatility <= c["volatility_cap"]}
    exit_rule = {"reverse": reverse, "rsi": side*(rsi-c["rsi_exit"]) >= 0,
        "momentum": side*momentum <= 0, "confidence": probability is not None and probability < c["exit_threshold"], "time": False}[c["exit_kind"]]
    enter = all(checks.values()) and not exit_rule
    result = {"enter": side if enter else 0, "exit_long": side == 1 and exit_rule, "exit_short": side == -1 and exit_rule}
    if explain:
        blocked = ", ".join(k.replace("_", " ") for k, passed in checks.items() if not passed)
        result["reason"] = ("Entry conditions met. " if enter else "Exit condition met. " if exit_rule else f"Waiting for {blocked}. ") + c["entry_description"] + " " + c["exit_description"]
        result["inputs"] = {"rsi_14": rsi, "return_5m_pct": momentum, "volatility_15m_pct": volatility,
            "fast_sma": fast, "slow_sma": slow, "probability": probability, "entry_checks": checks,
            "exit_condition_met": exit_rule, "rule_id": c["id"]}
    return result


def live_signal(candidate, closes, artifacts):
    values = feature_vector(closes)
    prediction = predict_vector(artifacts[candidate["model_key"]], values) if model_keys(candidate) else None
    result = evaluate(candidate, closes, values, prediction["probability"] if prediction else None, explain=True)
    if prediction:
        result.update(probability=prediction["probability"], drivers=prediction["drivers"], model_version=prediction["model_version"])
    return result
