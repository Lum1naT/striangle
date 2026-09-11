"""Bounds for the autonomous paper research service, independent of spot runs."""
from .configuration import dec, validate_config

AUTO_VERSION = "auto-v1"
MAX_LEVERAGE = 10
AUTO_DEFAULTS = {"max_leverage": 10, "interval_hours": 24, "history_candles": 100000,
    "min_new_candles": 1440, "min_validation_trades": 5,
    "funding_bps_8h": 1.0, "maintenance_margin_bps": 50.0}
AUTO_BOUNDS = {"max_leverage": (1, 10), "interval_hours": (6, 168), "history_candles": (5000, 100000),
    "min_new_candles": (60, 10080), "min_validation_trades": (1, 100),
    "funding_bps_8h": (0, 100), "maintenance_margin_bps": (10, 500)}


def validate_auto(raw=None):
    raw = {} if raw is None else raw
    if not isinstance(raw, dict) or set(raw)-set(AUTO_DEFAULTS)-{"risk"}:
        raise ValueError("Unknown autonomous research settings")
    config = AUTO_DEFAULTS | raw
    for key, (low, high) in AUTO_BOUNDS.items():
        value = config[key]
        if isinstance(value, bool) or not low <= dec(value) <= high:
            raise ValueError(f"{key} must be between {low} and {high}")
        config[key] = float(value)
        if key not in ("funding_bps_8h", "maintenance_margin_bps"):
            if int(config[key]) != config[key]:
                raise ValueError(f"{key} must be an integer")
            config[key] = int(config[key])
    config["risk"] = validate_config(raw.get("risk"))
    return config


def margin_config(policy):
    return policy["risk"] | {k: policy[k] for k in ("max_leverage", "funding_bps_8h", "maintenance_margin_bps")}


def leverage_value(value, cap=MAX_LEVERAGE):
    if isinstance(value, bool) or not 1 <= dec(value) <= min(MAX_LEVERAGE, cap) or dec(value) != int(dec(value)):
        raise ValueError("Leverage must be an integer within the configured cap and never above 10x")
    return int(value)
