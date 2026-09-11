import hashlib
import json
from decimal import Decimal, InvalidOperation

ENGINE_VERSION = "1.0.0"
PROMPT_VERSION = "news-v1"
STRATEGIES = ("trend", "rsi", "ai_trend")
DEFAULTS = {
    "capital": 10000, "allocation_pct": 10, "risk_per_trade_pct": 0.5,
    "fee_bps": 10, "slippage_bps": 5, "spread_bps": 2,
    "stop_pct": 2, "take_pct": 4, "max_drawdown_pct": 10, "daily_loss_pct": 3,
    "max_spread_bps": 20, "max_participation_pct": 5,
    "fast": 10, "slow": 30, "rsi_period": 14, "oversold": 30, "overbought": 70,
    "max_book_age_seconds": 5, "max_signal_age_seconds": 30,
    "ai_max_age_seconds": 900, "ai_min_score": 0.2,
    "min_imbalance": 0.05, "max_funding_rate": 0.001,
    "require_heatmap": False,
}
BOUNDS = {
    "capital": (10, 10000000), "allocation_pct": (0.1, 100), "risk_per_trade_pct": (0.01, 5),
    "fee_bps": (0, 100), "slippage_bps": (0, 100), "spread_bps": (0, 100),
    "stop_pct": (0.1, 30), "take_pct": (0.1, 100), "max_drawdown_pct": (0.1, 50),
    "daily_loss_pct": (0.1, 20), "max_spread_bps": (0.1, 100), "max_participation_pct": (0.1, 20),
    "fast": (2, 99), "slow": (3, 200), "rsi_period": (2, 100), "oversold": (1, 49), "overbought": (51, 99),
    "max_book_age_seconds": (1, 30), "max_signal_age_seconds": (1, 60),
    "ai_max_age_seconds": (60, 3600), "ai_min_score": (0, 1), "min_imbalance": (-1, 1), "max_funding_rate": (0, 0.01),
}


def dec(value):
    try:
        n = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("Expected a finite number") from None
    if not n.is_finite():
        raise ValueError("Expected a finite number")
    return n


def validate_config(raw=None):
    raw = raw or {}
    if not isinstance(raw, dict) or set(raw) - set(DEFAULTS):
        raise ValueError("Unknown configuration fields")
    result = DEFAULTS | raw
    for key, (lo, hi) in BOUNDS.items():
        value = dec(result[key])
        if not dec(lo) <= value <= dec(hi):
            raise ValueError(f"{key} must be between {lo} and {hi}")
        result[key] = float(value)
    for key in ("fast", "slow", "rsi_period", "max_book_age_seconds", "max_signal_age_seconds", "ai_max_age_seconds"):
        if result[key] != int(result[key]):
            raise ValueError(f"{key} must be an integer")
        result[key] = int(result[key])
    if result["fast"] >= result["slow"]:
        raise ValueError("Fast period must be shorter than slow period")
    if not isinstance(result["require_heatmap"], bool):
        raise ValueError("require_heatmap must be a boolean")
    return result


def fingerprint(config, model=""):
    # Capital is deliberately included: no unreviewed sizing changes in a promoted run.
    return hashlib.sha256(json.dumps({"config": config, "engine": ENGINE_VERSION, "model": model, "prompt": PROMPT_VERSION}, sort_keys=True).encode()).hexdigest()
