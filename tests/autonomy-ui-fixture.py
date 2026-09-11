"""Explicitly synthetic display fixture, used only by the isolated browser test."""
import random

from django.conf import settings
from django.utils import timezone

from trading.auto_config import margin_config
from trading.auto_rules import make_candidate
from trading.auto_signals import strategies
from trading.auto_paper import tick
from trading.autonomy import configure
from trading.margin import metrics, new_wallet
from trading.models import AutoCycle
from trading.tests.test_ml import artifact

policy = configure()
cfg, now = margin_config(policy.config), timezone.now()
report = {"source": "synthetic_ui_fixture", "allocated_symbol": None, "allocation_reason": "Synthetic UI fixture; no performance evidence.",
    "method": "Synthetic display fixture only.", "assumptions": "No execution.", "evidence": "No performance claim.",
    "forward_method": "Synthetic portfolios.", "fitting": {}, "comparisons": {}, "by_asset": {}, "leaders": [],
    "candidate_count": 528, "search_trial_count": 384}
artifacts = {}
for index, symbol in enumerate(settings.SYMBOLS):
    candidate = make_candidate(random.Random(index+5), cfg)
    score = metrics(new_wallet(cfg["capital"]), cfg)
    row = {"symbol": symbol, "candidate": candidate, "metrics": score, "holdout": score, "origin": "scikit_search"}
    baseline = row | {"candidate": strategies()[0] | {"leverage": 1}, "origin": "baseline"}
    report["by_asset"][symbol] = row
    report["comparisons"][symbol] = {"selected": row, "searched": row, "baseline": baseline}
    report["leaders"].append(row)
    report["fitting"][symbol] = {"optimization": {"rounds": [{"round": i, "trials": i*24,
        "model_guided": 16 if i > 1 else 0, "exploration": 8 if i > 1 else 24, "best_objective": 0} for i in range(1, 5)]}}
    artifacts[symbol] = {f"{h}:{side}": artifact() for h in (15, 60) for side in (1, -1)}
report["champion"], report["holdout"] = report["leaders"][0], score
cycle = AutoCycle.objects.create(status="ready", config=policy.config, cutoff=now, report=report, artifacts=artifacts)
tick()
