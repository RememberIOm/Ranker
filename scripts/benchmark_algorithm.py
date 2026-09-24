"""Local latency and bounded-search diagnostics; no production DB access.

uv run python -m scripts.benchmark_algorithm --output docs/algorithm_benchmark_v2.json
"""

import argparse
import json
import random
import resource
import time
from collections import Counter
from itertools import combinations
from pathlib import Path

import numpy as np

from ranker.rating_engine import ALGORITHM_VERSION, fit_rankings
from ranker.services import candidate_utilities, get_match_pair, get_match_triple
from scripts.evaluate_algorithm import SimulationStore


def make_store(n, ballots, mode, seed):
    rng = np.random.default_rng(seed)
    ranks = [
        tuple((int(i),) for i in rng.choice(np.arange(1, n + 1), mode, replace=False))
        for _ in range(ballots)
    ]
    store = SimulationStore(n, 1)
    store.observations["0"] = Counter(ranks)
    store.exposures["0"] = ballots
    start = time.perf_counter()
    fit = fit_rankings(ranks)
    fit.marginal_variances()
    elapsed = time.perf_counter() - start
    store.fits["0"] = fit
    return store, elapsed


def run():
    result = {
        "algorithm_version": ALGORITHM_VERSION,
        "runtime": [],
        "search_quality": [],
        "limitations": [
            "One criterion, local machine, synthetic random full rankings.",
            "Peak RSS is cumulative for this process, not per-request allocation.",
            "Search regret measures the local design utility, not real ranking accuracy.",
            "No guarantee at the maximum item/criterion/backup limits.",
        ],
    }
    for n, ballots in [(50, 300), (100, 600), (500, 2000), (1000, 3000)]:
        store, fit_seconds = make_store(n, ballots, 3, 42)
        random.seed(42)
        start = time.perf_counter()
        get_match_triple(store)
        result["runtime"].append(
            {
                "items": n,
                "ballots": ballots,
                "mode": 3,
                "fit_and_marginals_seconds": fit_seconds,
                "matching_seconds": time.perf_counter() - start,
                "process_peak_rss_mib": resource.getrusage(
                    resource.RUSAGE_SELF
                ).ru_maxrss
                / 1024,
            }
        )
        print("runtime", n, flush=True)
    # Full-space reference evaluated in bounded batches to avoid a huge tensor.
    for n, mode, ballots in [(24, 3, 200), (96, 2, 400)]:
        ratios = []
        for seed in range(5):
            store, _ = make_store(n, ballots, mode, seed + 80)
            candidates = list(combinations(range(n), mode))
            best = max(
                float(
                    candidate_utilities(
                        store, store.items, candidates[start : start + 64]
                    ).max()
                )
                for start in range(0, len(candidates), 64)
            )
            random.seed(seed + 80)
            picker = get_match_pair if mode == 2 else get_match_triple
            selected = tuple(sorted(item["id"] - 1 for item in picker(store)))
            chosen = float(candidate_utilities(store, store.items, [selected])[0])
            ratios.append(chosen / best)
        result["search_quality"].append(
            {
                "items": n,
                "mode": mode,
                "ballots": ballots,
                "seeds": list(range(80, 85)),
                "full_candidate_count": len(candidates),
                "chosen_over_full_best_utility": ratios,
                "mean_ratio": float(np.mean(ratios)),
                "minimum_ratio": min(ratios),
            }
        )
        print("search quality", n, mode, flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=Path("docs/algorithm_benchmark_v2.json")
    )
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(run(), indent=2, allow_nan=False) + "\n")


if __name__ == "__main__":
    main()
