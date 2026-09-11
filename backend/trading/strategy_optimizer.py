"""Sequential, scikit-learn guided hypothesis search on development data only.

The regressor predicts backtest objectives, not asset prices or future profits.
Neither later selection scores nor holdout results are fed back to this search.
"""
import hashlib
import random

from .auto_rules import encoding, make_candidate

SEARCH_VERSION = "extra-trees-search-v1"
TRIAL_BUDGET = 96
POOL_SIZE = 2048
BATCH_SIZE = 24
FINALISTS = 12


def objective(folds, minimum_trades, max_drawdown):
    trades = sum(f["closed_trades"] for f in folds)
    worst_return = min(f["return_pct"] for f in folds)
    drawdown = max(f["max_drawdown_pct"] for f in folds)
    failed = any(f["liquidations"] or f["halted"] for f in folds) or drawdown >= max_drawdown
    score = worst_return-.25*drawdown-2*max(0, minimum_trades-trades)-(100 if failed else 0)
    return score, trades >= minimum_trades and not failed


def optimize(symbol, bars, cfg, predictions, vectors, search_start, search_end, minimum_trades,
        progress=lambda text: None, stop=None, *, trial_budget=TRIAL_BUDGET):
    import numpy as np
    import sklearn
    from sklearn.ensemble import ExtraTreesRegressor
    from threadpoolctl import threadpool_limits
    from .auto_search import backtest, signal_series

    if not 24 <= trial_budget <= 256:
        raise ValueError("Strategy search budget must be between 24 and 256")
    seed = int(hashlib.sha256((SEARCH_VERSION+symbol).encode()).hexdigest()[:8], 16)
    rng, pool, seen = random.Random(seed), [], set()
    while len(pool) < POOL_SIZE:
        candidate = make_candidate(rng, cfg)
        if candidate["id"] not in seen:
            pool.append(candidate); seen.add(candidate["id"])
    encoded = [encoding(c) for c in pool]
    names = list(encoded[0])
    x = np.array([[r[k] for k in names] for r in encoded], dtype=float)
    midpoint = (search_start+(search_end-search_start)/2)//60*60
    windows = [(search_start, midpoint), (midpoint, search_end)]
    if midpoint <= search_start or midpoint >= search_end:
        raise ValueError("Not enough history for chronological search folds")
    remaining, evaluated, trials, rounds = list(range(len(pool))), [], [], []
    regressor = None
    while len(trials) < trial_budget:
        if stop is not None and stop.is_set():
            raise InterruptedError("Research worker is stopping")
        round_number = len(rounds)+1
        batch_size = min(BATCH_SIZE, trial_budget-len(trials))
        estimates = {}
        if not trials:
            chosen = [(i, "initial_exploration") for i in rng.sample(remaining, batch_size)]
        else:
            with threadpool_limits(limits=1):
                regressor = ExtraTreesRegressor(n_estimators=64, max_depth=8, min_samples_leaf=2,
                    random_state=seed+round_number, n_jobs=1).fit(x[evaluated], [t["objective"] for t in trials])
                tree_predictions = np.array([tree.predict(x[remaining]) for tree in regressor.estimators_])
                mean, spread = tree_predictions.mean(axis=0), tree_predictions.std(axis=0)
            # Tree disagreement is an exploration heuristic, not a calibrated
            # confidence interval. Keep one third of each batch random.
            ranked = sorted(range(len(remaining)), key=lambda j: (-(mean[j]+.5*spread[j]), remaining[j]))
            guided = [remaining[j] for j in ranked[:batch_size-batch_size//3]]
            random_picks = rng.sample([i for i in remaining if i not in guided], batch_size-len(guided))
            chosen = [(i, "model_guided") for i in guided] + [(i, "exploration") for i in random_picks]
            estimates = {i: {"predicted_objective": float(mean[j]), "tree_disagreement": float(spread[j])}
                for j, i in enumerate(remaining) if i in guided}
        progress(f"{symbol}: scikit-learn entry/exit search, round {round_number}; {len(trials)}/{trial_budget} combinations tested")
        for index, method in chosen:
            if stop is not None and stop.is_set():
                raise InterruptedError("Research worker is stopping")
            candidate = pool[index]
            signals = signal_series(bars, candidate, predictions, vectors, start=search_start, end=search_end)
            scores = [backtest(bars, signals, candidate, cfg, a, b) for a, b in windows]
            score, eligible = objective(scores, minimum_trades, cfg["max_drawdown_pct"])
            trials.append({"candidate": candidate, "round": round_number, "proposal": method,
                "surrogate": estimates.get(index), "folds": scores, "objective": score, "eligible": eligible})
            evaluated.append(index); remaining.remove(index)
        rounds.append({"round": round_number, "trials": len(trials), "model_guided": sum(m == "model_guided" for _, m in chosen),
            "exploration": sum(m != "model_guided" for _, m in chosen),
            "best_objective": max(t["objective"] for t in trials), "eligible": sum(t["eligible"] for t in trials)})
    ranked_trials = sorted((t for t in trials if t["eligible"]), key=lambda t: (-t["objective"], t["candidate"]["leverage"], t["candidate"]["id"]))
    finalists = [t["candidate"] for t in ranked_trials[:FINALISTS]]
    importance = sorted(zip(names, regressor.feature_importances_ if regressor else np.zeros(len(names))), key=lambda item: -item[1])
    report = {"version": SEARCH_VERSION, "algorithm": "ExtraTreesRegressor", "sklearn_version": sklearn.__version__, "seed": seed,
        "pool_size": len(pool), "trial_budget": trial_budget, "trial_count": len(trials), "rounds": rounds,
        "search_start": search_start, "search_end": search_end, "folds": [{"start": a, "end": b} for a, b in windows],
        "finalist_ids": [c["id"] for c in finalists], "trials": trials,
        "parameter_importance": [{"parameter": k, "importance": float(v)} for k, v in importance[:8]],
        "objective": "Worst of two chronological development-fold net returns minus 0.25 times the larger drawdown; "
            "subtract 2 percentage points per missing minimum trade and 100 for a liquidation, risk halt or drawdown-limit breach. "
            "Only candidates meeting the development trade and risk gates become finalists.",
        "method": "24 initial random combinations, then batches with two-thirds ranked by predicted objective plus half the tree disagreement "
            "and one-third random exploration. ExtraTrees is refitted only to completed development backtests. "
            "The best 12 eligible combinations are frozen before later validation. Parameter importance describes this surrogate, not causation or future profitability."}
    return finalists, report
