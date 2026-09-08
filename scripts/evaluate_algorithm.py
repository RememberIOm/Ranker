"""Synthetic inference and production matchmaking evaluation for gpl-map-v2.

uv run python -m scripts.evaluate_algorithm --output docs/algorithm_evaluation_v2.json

Uniform, diagonal-information reference, and production policies share the same
fitted model and potential outcome random numbers. Timing is local wall time;
assumed choice counts are not measured human effort. No real-user claims.
"""

import argparse
from collections import Counter
from itertools import combinations
import json
import math
from pathlib import Path
import random
from statistics import mean, stdev
import time

import numpy as np
from scipy.special import expit, softmax

from ranker.rating_engine import ALGORITHM_VERSION, fit_rankings, predict_pairs
from ranker.services import get_match_pair, get_match_triple


class SimulationStore:
    def __init__(self, n: int, dimensions: int):
        self.items = [{"id": i, "mu": {}, "sigma_sq": {}} for i in range(1, n + 1)]
        self.criteria = [
            {"key": str(k), "weight": float(3 if k == 0 else 1)}
            for k in range(dimensions)
        ]
        self.settings = {"initial_sigma": 2.0}
        self.active_round = None
        self.observations = {str(k): Counter() for k in range(dimensions)}
        self.exposures = Counter()
        self.fits = {str(k): fit_rankings([]) for k in range(dimensions)}
        self.events = []
        self.fit_seconds = 0.0
        self.match_seconds = 0.0

    def get_item(self, iid):
        return next((item for item in self.items if item["id"] == iid), None)

    def posterior(self, key):
        return self.fits[key]

    def response_rate(self, key):
        return (sum(self.observations[key].values()) + 1) / (self.exposures[key] + 2)

    def recent_votes(self, limit=5):
        return self.events[-limit:][::-1]

    def refit(self):
        start = time.perf_counter()
        for key, counts in self.observations.items():
            self.fits[key] = fit_rankings(list(counts), counts=list(counts.values()))
        self.fit_seconds += time.perf_counter() - start


def sample_ranking(truth, selected, uniforms, ties):
    """Independent generative implementation of subset choices, not engine code."""
    remaining, groups = sorted(selected), []
    for uniform in uniforms:
        if len(remaining) == 1:
            groups.append(tuple(remaining))
            break
        subsets, weights = [], []
        for size in range(1, len(remaining) + 1):
            for subset in combinations(remaining, size):
                subsets.append(subset)
                weights.append(
                    sum(truth[i - 1] for i in subset) / size
                    + (ties[size - 2] if size > 1 else 0)
                )
        probs = softmax(weights)
        chosen = subsets[
            min(len(subsets) - 1, int(np.searchsorted(np.cumsum(probs), uniform)))
        ]
        groups.append(chosen)
        remaining = [i for i in remaining if i not in chosen]
        if not remaining:
            break
    return tuple(groups)


def reference_match(store, mode):
    """Old diagonal information *score* at the new MAP, isolating policy effects."""
    pool = store.items
    variances = {
        c["key"]: store.posterior(c["key"]).marginal_variances() for c in store.criteria
    }
    edges = {}
    for a, b in combinations([item["id"] for item in pool], 2):
        score = 0.0
        for c in store.criteria:
            key = c["key"]
            fit = store.posterior(key)
            p = expit(fit.mean(a) - fit.mean(b))
            w = p * (1 - p)
            score += 0.5 * math.log1p(w * variances[key].get(a, 4))
            score += 0.5 * math.log1p(w * variances[key].get(b, 4))
        edges[a, b] = score
    candidates = list(combinations(sorted(item["id"] for item in pool), mode))
    recent = {frozenset(e.values()) for e in store.recent_votes()}
    available = [c for c in candidates if frozenset(c) not in recent] or candidates
    scores = [
        sum(edges[tuple(sorted(pair))] for pair in combinations(c, 2))
        for c in available
    ]
    best = max(scores)
    return random.choice(
        [
            c
            for c, score in zip(available, scores)
            if math.isclose(score, best, abs_tol=1e-12)
        ]
    )


def pair_targets(truth, pairs, ties):
    difference = np.array([truth[a - 1] - truth[b - 1] for a, b in pairs])
    if ties is None:
        p = expit(difference)
        return np.column_stack((p, np.zeros(len(p)), 1 - p))
    return softmax(
        np.column_stack(
            (difference / 2, np.full(len(pairs), ties[0]), -difference / 2)
        ),
        axis=1,
    )


def score_fit(store, truth, ties):
    ids = [item["id"] for item in store.items]
    pairs = list(combinations(ids, 2))
    estimated = np.array(
        [[store.posterior(str(k)).mean(i) for i in ids] for k in range(len(truth))]
    )
    weights = np.array([c["weight"] for c in store.criteria])
    weights /= weights.sum()
    composite = weights @ estimated
    actual = weights @ truth
    criterion_accuracy, coverage, briers, loglosses = [], [], [], []
    unseen = []
    for k, values in enumerate(truth):
        key = str(k)
        fit = store.posterior(key)
        p = predict_pairs(fit, pairs)
        q = pair_targets(values, pairs, ties)
        briers.extend(1 + (p**2).sum(axis=1) - 2 * (p * q).sum(axis=1))
        loglosses.extend(-(q * np.log(np.clip(p, 1e-15, 1))).sum(axis=1))
        cov = fit.covariance(ids)
        counts = Counter()
        for groups, count in store.observations[key].items():
            for group in groups:
                for iid in group:
                    counts[iid] += count
        unseen.extend(counts[i] == 0 for i in ids)
        for a, b in combinations(range(len(ids)), 2):
            d = estimated[k, a] - estimated[k, b]
            target = values[a] - values[b]
            criterion_accuracy.append(0.5 if abs(d) < 1e-9 else float(d * target > 0))
            sd = math.sqrt(max(0, cov[a, a] + cov[b, b] - 2 * cov[a, b]))
            coverage.append(float(abs(d - target) <= 1.96 * sd))
    composite_accuracy = [
        (
            0.5
            if abs(composite[a] - composite[b]) < 1e-9
            else float((composite[a] - composite[b]) * (actual[a] - actual[b]) > 0)
        )
        for a, b in combinations(range(len(ids)), 2)
    ]
    estimated_top = set(np.argsort(composite)[-3:])
    true_top = set(np.argsort(actual)[-3:])
    return {
        "criterion_pair_accuracy": mean(criterion_accuracy),
        "composite_pair_accuracy": mean(composite_accuracy),
        "top3_overlap": len(estimated_top & true_top) / 3,
        "expected_multiclass_brier": float(np.mean(briers)),
        "expected_log_loss": float(np.mean(loglosses)),
        "nominal_95_difference_coverage": mean(coverage),
        "unanswered_item_criterion_fraction": float(np.mean(unseen)),
    }


def evaluate(seed, scenario, mode, ballots, policy):
    rng = np.random.default_rng(seed)
    n, dimensions = 10, 3
    base = rng.normal(size=n)
    truth = np.array(
        [
            (1 if scenario != "opposed" or k % 2 == 0 else -1) * base
            + rng.normal(0, 0.4, n)
            for k in range(dimensions)
        ]
    )
    truth -= truth.mean(axis=1, keepdims=True)
    utilities = rng.gumbel(size=(ballots, dimensions, n))
    choice_uniforms = rng.random((ballots, dimensions, 3))
    response_uniforms = rng.random((ballots, dimensions))
    ties = (math.log(0.8), math.log(0.4)) if scenario == "ties" else None
    store = SimulationStore(n, dimensions)
    all_items = store.items
    # Explicitly seed production's RNG independently of potential user outcomes.
    random.seed(seed + 47000)
    early_loss, repeated, response_counts = [], 0, Counter()
    current_truth = truth
    for step in range(ballots):
        current_truth = (
            -truth if scenario == "drift" and step >= ballots // 2 else truth
        )
        if scenario == "new_items" and step < ballots // 2:
            store.items = all_items[:6]
        elif scenario == "bridge" and step < ballots // 2:
            store.items = all_items[:5] if step % 2 else all_items[5:]
        else:
            store.items = all_items
        # Shared, fixed probes for temporal expected loss, independent of selection.
        if step % 20 == 0:
            if policy == "uniform":
                store.refit()
            pairs = [(1, 2), (3, 4), (7, 10)]
            p = predict_pairs(store.posterior("0"), pairs)
            q = pair_targets(current_truth[0], pairs, ties)
            early_loss.append(
                float(np.mean(-(q * np.log(np.clip(p, 1e-15, 1))).sum(axis=1)))
            )
        start = time.perf_counter()
        if policy == "uniform":
            selected = tuple(
                sorted(random.sample([item["id"] for item in store.items], mode))
            )
        elif policy == "diagonal_reference":
            selected = reference_match(store, mode)
        else:
            picker = get_match_pair if mode == 2 else get_match_triple
            selected = tuple(sorted(item["id"] for item in picker(store)))
        store.match_seconds += time.perf_counter() - start
        recent_pairs = {
            frozenset(pair)
            for e in store.recent_votes()
            for pair in combinations(e.values(), 2)
        }
        repeated += sum(
            frozenset(pair) in recent_pairs for pair in combinations(selected, 2)
        )
        for k in range(dimensions):
            key = str(k)
            store.exposures[key] += 1
            missing = scenario == "missing" and (
                response_uniforms[step, k] < (0.85 if k == 0 else 0.1)
                or (k == 1 and any(i >= 8 for i in selected))
            )
            if missing:
                continue
            response_counts[key] += 1
            if ties is None:
                values = current_truth[k] + utilities[step, k]
                groups = tuple(
                    (i,)
                    for i in sorted(selected, key=lambda i: values[i - 1], reverse=True)
                )
            else:
                groups = sample_ranking(
                    current_truth[k], selected, choice_uniforms[step, k], ties
                )
            store.observations[key][groups] += 1
        store.events.append({f"item{i}_id": iid for i, iid in enumerate(selected, 1)})
        if policy != "uniform":
            store.refit()
    store.items = all_items
    if policy == "uniform":
        store.refit()
    metrics = score_fit(store, current_truth, ties)
    metrics.update(
        {
            "temporal_probe_log_loss": mean(early_loss),
            "recent_pair_repeat_fraction": repeated / (ballots * math.comb(mode, 2)),
            "answered_criterion_ballots": sum(response_counts.values()),
            "fit_seconds": store.fit_seconds,
            "matching_seconds": store.match_seconds,
        }
    )
    return metrics


def diagnostics():
    balanced = fit_rankings([((1,), (2,))] * 10 + [((2,), (1,))] * 10)
    reverse = fit_rankings([((2,), (1,))] * 10 + [((1,), (2,))] * 10)
    disconnected = fit_rankings([((1, 2),)] * 1000 + [((3, 4),)] * 1000)
    cov = disconnected.covariance([1, 3])
    return {
        "balanced_mu_difference": balanced.mean(1) - balanced.mean(2),
        "order_location_max_difference": float(
            np.max(np.abs(balanced.location - reverse.location))
        ),
        "disconnected_cross_difference_sd": math.sqrt(
            cov[0, 0] + cov[1, 1] - 2 * cov[0, 1]
        ),
        "disconnected_exact_common_offset_sd_lower_bound": 2.0,
    }


def run(seeds, quick=False, seed_start=0):
    cases = [
        ("model", scenario, mode, 180 // (mode - 1), "uniform")
        for scenario in ("aligned", "opposed", "ties", "missing", "drift")
        for mode in (2, 3)
    ]
    cases += [
        ("matching", scenario, mode, 60 // (mode - 1), policy)
        for scenario in ("aligned", "missing", "new_items", "bridge")
        for mode in (2, 3)
        for policy in ("uniform", "diagonal_reference", "production")
    ]
    if quick:
        cases = [
            ("smoke", scenario, mode, 12, policy)
            for scenario, mode, policy in [
                ("ties", 3, "uniform"),
                ("aligned", 2, "production"),
                ("missing", 3, "production"),
                ("bridge", 2, "diagonal_reference"),
            ]
        ]
    output = {
        "algorithm_version": ALGORITHM_VERSION,
        "seeds": list(range(seed_start, seed_start + seeds)),
        "items": 10,
        "criteria": 3,
        "weights": [3, 1, 1],
        "diagnostics": diagnostics(),
        "protocol": {
            "generator": "Gumbel/PL or independently implemented generalized PL with ties",
            "comparison": "same priors/model and seeded potential outcomes; different policy-selected observations",
            "budget": "same displayed-criterion choice budget: ballots * (mode-1) * criteria; not measured human time",
            "coverage": "joint Laplace difference covariance, including nuisance tie parameters",
            "scores": "expected 3-class Brier (sum over classes), log loss on all true held-out pair distributions",
            "temporal_probes": "expected log loss at fixed pairs, criterion 0, before each 20th ballot",
            "limitations": [
                "Synthetic only; real-user accuracy and human time are unverified.",
                "GPL/PL generators favor the fitted model family; drift and missingness are stress cases.",
                "Local design utility is not exact information gain or rank-error reduction.",
                "The diagonal reference uses the NEW fitted model, not the historical online engine.",
                "New matching pool/candidate caps are not activated with only ten items.",
                "Seed SD is variability, not a confidence interval.",
            ],
        },
        "results": [],
    }
    for phase, scenario, mode, ballots, policy in cases:
        rows = [
            evaluate(seed, scenario, mode, ballots, policy)
            for seed in range(seed_start, seed_start + seeds)
        ]
        output["results"].append(
            {
                "phase": phase,
                "scenario": scenario,
                "mode": mode,
                "ballots": ballots,
                "policy": policy,
                "assumed_choices": ballots * (mode - 1) * 3,
                "metrics": {
                    key: {
                        "mean": mean(row[key] for row in rows),
                        "seed_sd": stdev(row[key] for row in rows) if seeds > 1 else 0,
                    }
                    for key in rows[0]
                },
                "per_seed": rows,
            }
        )
        print(f"{phase}: {scenario}, {mode}way, {policy} ({seeds} seeds)", flush=True)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seeds", type=int, default=10)
    parser.add_argument("--quick", action="store_true")
    parser.add_argument("--seed-start", type=int, default=0)
    parser.add_argument(
        "--output", type=Path, default=Path("docs/algorithm_evaluation_v2.json")
    )
    args = parser.parse_args()
    if args.seeds < 1:
        parser.error("--seeds must be positive")
    output = run(args.seeds, args.quick, args.seed_start)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(output, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    )
    print(f"Evaluation written to {args.output}", flush=True)


if __name__ == "__main__":
    main()
