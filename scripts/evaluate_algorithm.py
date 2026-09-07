"""합성 취향에서 온라인 BT 근사의 순위·예측·구간 보정을 평가한다.

실제 사용자 정확도나 효율을 보장하지 않는 재현 가능한 진단 도구다.
실행: uv run python scripts/evaluate_algorithm.py --output docs/algorithm_evaluation.json
"""

import argparse
import json
import math
import random
import sys
from itertools import combinations
from pathlib import Path
from statistics import mean, stdev
from types import SimpleNamespace
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rating_engine import ALGORITHM_VERSION, predict_outcomes, sigmoid, update_ratings
from services import hierarchical_shrinkage


def evaluate(
    seed: int, scenario: str, mode: int, ballots: int, shrinkage: float
) -> dict[str, float]:
    """무작위 대결 표본으로 모델만 평가하며 매칭 정책의 성능은 분리한다."""
    rng = random.Random(seed)
    n, dimensions = 12, 6
    base = [rng.gauss(0, 1) for _ in range(n)]
    truth = []
    for key in range(dimensions):
        sign = -1 if scenario == "opposed_criteria" and key % 2 else 1
        values = [sign * value + rng.gauss(0, 0.4) for value in base]
        center = mean(values)
        truth.append([value - center for value in values])
    keys = [str(key) for key in range(dimensions)]
    items = [
        {
            "mu": dict.fromkeys(keys, 0.0),
            "sigma_sq": dict.fromkeys(keys, 4.0),
            "criterion_matches": dict.fromkeys(keys, 0),
        }
        for _ in range(n)
    ]
    settings = SimpleNamespace(
        settings={"hierarchical_strength": shrinkage, "initial_sigma": 2.0},
        criteria=[{"key": key} for key in keys],
    )
    for _ in range(ballots):
        selected = rng.sample(range(n), mode)
        for criterion, key in enumerate(keys):
            # Gumbel 효용의 순위는 PL을 따르고 쌍대 주변 승률은 BT를 따른다.
            utilities = {
                i: truth[criterion][i] - math.log(-math.log(rng.random()))
                for i in selected
            }
            comparisons = [
                (a, b, float(utilities[a] > utilities[b]))
                for a, b in combinations(selected, 2)
            ]
            updated = update_ratings(
                {i: (items[i]["mu"][key], items[i]["sigma_sq"][key]) for i in selected},
                comparisons,
            )
            for i, (mu, variance) in updated.items():
                items[i]["mu"][key] = mu
                items[i]["sigma_sq"][key] = variance
                items[i]["criterion_matches"][key] += mode - 1
        for i in selected:
            hierarchical_shrinkage(settings, items[i])
    accuracy, brier, logloss, coverage = [], [], [], []
    point_brier, point_logloss = [], []
    for criterion, key in enumerate(keys):
        for a, b in combinations(range(n), 2):
            difference = items[a]["mu"][key] - items[b]["mu"][key]
            actual = truth[criterion][a] - truth[criterion][b]
            spread = math.sqrt(items[a]["sigma_sq"][key] + items[b]["sigma_sq"][key])
            p = predict_outcomes(
                items[a]["mu"][key],
                items[a]["sigma_sq"][key],
                items[b]["mu"][key],
                items[b]["sigma_sq"][key],
                0,
                1.5,
            )[0]
            target = sigmoid(actual)
            point = sigmoid(difference)
            accuracy.append(0.5 if difference == 0 else float(difference * actual > 0))
            coverage.append(float(abs(difference - actual) <= 1.96 * spread))
            for prediction, bs, ls in (
                (p, brier, logloss),
                (point, point_brier, point_logloss),
            ):
                prediction = max(1e-12, min(1 - 1e-12, prediction))
                bs.append(target * (1 - prediction) ** 2 + (1 - target) * prediction**2)
                ls.append(
                    -target * math.log(prediction)
                    - (1 - target) * math.log(1 - prediction)
                )
    return {
        "pair_order_accuracy": mean(accuracy),
        "expected_brier": mean(brier),
        "expected_log_loss": mean(logloss),
        "point_expected_brier": mean(point_brier),
        "point_expected_log_loss": mean(point_logloss),
        "nominal_95_pair_difference_coverage": mean(coverage),
    }


def run(seeds: int) -> dict[str, Any]:
    """동일 투표수·쌍수·가정한 판단 비용 비교를 구분하여 기록한다."""
    arms = [
        ("independent_2way", 2, 300, 0.0),
        ("legacy_shrinkage_2way", 2, 300, 5.0),
        ("3way_same_pair_count", 3, 100, 0.0),
        ("3way_same_assumed_decision_cost", 3, 150, 0.0),
        ("3way_same_ballots", 3, 300, 0.0),
    ]
    output = {
        "algorithm_version": ALGORITHM_VERSION,
        "seeds": list(range(seeds)),
        "items": 12,
        "criteria": 6,
        "matching": "uniform random, not production EIG",
        "outcomes": "Gumbel/Plackett-Luce, no draws or missing criteria",
        "evaluation": "all true pair probabilities; expected proper scores, no sampled test labels",
        "decision_cost_assumption": "one choice per criterion in 2way, best+worst two choices in 3way; not measured time",
        "limitations": [
            "Synthetic diagnostics do not establish real-user accuracy or confidence calibration.",
            "95 percent intervals use independent diagonal variance; no covariance correction.",
            "Coverage targets pair differences, avoiding the unidentifiable absolute skill location.",
            "Draw heuristics, production matching, missing judgments and preference drift are not evaluated.",
            "One 3way ballot creates correlated pair outcomes; pair-count equality is not information equality.",
            "Uncertainty-aware and point predictions share fitted ratings; comparison only evaluates readout.",
        ],
        "results": [],
    }
    for scenario in ("aligned_criteria", "opposed_criteria"):
        for name, mode, ballots, strength in arms:
            rows = [
                evaluate(seed, scenario, mode, ballots, strength)
                for seed in range(seeds)
            ]
            output["results"].append(
                {
                    "scenario": scenario,
                    "arm": name,
                    "ballots": ballots,
                    "pair_outcomes_per_criterion": ballots * math.comb(mode, 2),
                    "assumed_choices_all_criteria": ballots * (mode - 1) * 6,
                    "metrics": {
                        key: {
                            "mean": mean(row[key] for row in rows),
                            "seed_sd": stdev(row[key] for row in rows)
                            if seeds > 1
                            else 0,
                        }
                        for key in rows[0]
                    },
                }
            )
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seeds", type=int, default=20)
    parser.add_argument(
        "--output", type=Path, default=Path("docs/algorithm_evaluation.json")
    )
    args = parser.parse_args()
    if args.seeds < 1:
        parser.error("--seeds는 1 이상이어야 합니다.")
    output = run(args.seeds)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n")
    print(
        f"평가 완료: {args.output} ({args.seeds} seeds, {len(output['results'])} arms)"
    )


if __name__ == "__main__":
    main()
