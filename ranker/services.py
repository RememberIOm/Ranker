"""Display and matchmaking based on the fitted joint ranking model."""

import math
import random
from itertools import combinations
from typing import Any

import numpy as np
from scipy.special import roots_legendre

from ranker.rating_engine import expected_information, predict_pair
from ranker.schemas import vote_ids

# ranker.store가 이 모듈을 import하므로 타입 힌트용 이름은 늦게 불러옵니다.
lazy from ranker.store import DataStore


def display_rating(store: DataStore, mu: float) -> float:
    return mu * store.settings["display_scale"] + store.settings["display_center"]


def display_uncertainty(store: DataStore, sigma_sq: float) -> float:
    return math.sqrt(sigma_sq) * store.settings["display_scale"]


def get_match_probabilities(
    store: DataStore, key: str, a: int, b: int
) -> dict[str, float]:
    probabilities = predict_pair(store.posterior(key), a, b)
    # Largest remainder rounding keeps every value nonnegative and the sum 100%.
    scaled = np.array(probabilities) * 1000
    ticks = np.floor(scaled).astype(int)
    for i in np.argsort(-(scaled - ticks))[: 1000 - int(ticks.sum())]:
        ticks[i] += 1
    return dict(
        zip(("win_a", "draw", "win_b"), (float(x) / 10 for x in ticks), strict=True)
    )


def composite_rating(store: DataStore, item: dict[str, Any]) -> float:
    total_weight = sum(c["weight"] for c in store.criteria)
    if total_weight <= 0:
        return float(store.settings["display_center"])
    return (
        sum(
            display_rating(store, item["mu"][c["key"]]) * c["weight"]
            for c in store.criteria
        )
        / total_weight
    )


# A bounded, uniformly sampled search. These limits bound latency, not confidence.
_MATCH_POOL_SIZE = 64
_MATCH_CANDIDATES = 512
_LEGENDRE_NODES, _LEGENDRE_WEIGHTS = roots_legendre(12)


def candidate_utilities(
    store: DataStore, pool: list[dict], candidates: list[tuple[int, ...]]
) -> np.ndarray:
    """Expected reduction of composite pair-order Brier risk per choice.

    The full-ballot Fisher matrix gives a local Gaussian covariance update. If
    rho is the fraction of difference variance resolved, the expected reduction
    in binary Brier risk is Phi_2(z,z;rho)-Phi(z)^2. Integrate its derivative in
    rho after t=sin(theta). This targets uncertain *orders*, including initially
    equal means. It remains a local design approximation, not exact EIG.

    A Beta-smoothed response fraction discounts persistently skipped criteria;
    this planning estimate is not an outcome-dependent missing-data likelihood.
    """
    if not candidates:
        return np.zeros(0)
    n, size = len(pool), len(candidates[0])
    indices = np.array([(*candidate, n, n + 1) for candidate in candidates])
    scores = np.zeros(len(candidates))
    composite_mean = np.zeros(n)
    composite_covariance = np.zeros((n, n))
    resolved = np.zeros((len(candidates), n, n))
    total_weight = sum(c["weight"] for c in store.criteria)
    if total_weight <= 0:
        return scores
    ids = [item["id"] for item in pool]
    for criterion in store.criteria:
        key = criterion["key"]
        posterior = store.posterior(key)
        covariance = posterior.covariance(ids)
        means = np.array(
            [posterior.mean(iid) for iid in ids] + posterior.location[-2:].tolist()
        )
        local = covariance[indices[:, :, None], indices[:, None, :]]
        cross = covariance[:n, indices].transpose(1, 0, 2)
        fisher = expected_information(means[indices], size)
        reduction = np.linalg.solve(np.eye(size + 2) + fisher @ local, fisher)
        weight = criterion["weight"] / total_weight
        composite_mean += weight * means[:n]
        composite_covariance += weight**2 * covariance[:n, :n]
        resolved += (
            weight**2
            * store.response_rate(key)
            * ((cross @ reduction) @ cross.swapaxes(1, 2))
        )
    a, b = np.triu_indices(n, 1)
    variance = np.maximum(
        1e-15,
        composite_covariance[a, a]
        + composite_covariance[b, b]
        - 2 * composite_covariance[a, b],
    )
    z_squared = (composite_mean[a] - composite_mean[b]) ** 2 / variance
    reduction = resolved[:, a, a] + resolved[:, b, b] - 2 * resolved[:, a, b]
    angle = np.arcsin(np.clip(reduction / variance, 0.0, 1.0))
    # Smooth one-dimensional integral; avoid an array over pairs AND all nodes.
    integral = np.zeros_like(angle)
    for node, weight in zip(_LEGENDRE_NODES, _LEGENDRE_WEIGHTS, strict=True):
        theta = angle * (node + 1) / 2
        integral += weight * np.exp(-z_squared / (1 + np.sin(theta)))
    scores = np.mean(angle * integral / (4 * math.pi), axis=1)
    return scores / (size - 1)


def _sample_candidates(
    pool_size: int, size: int, *, focused: bool
) -> list[tuple[int, ...]]:
    available = pool_size - 1 if focused else pool_size
    pick = size - 1 if focused else size
    if math.comb(available, pick) <= _MATCH_CANDIDATES:
        if focused:
            return [(0, *tail) for tail in combinations(range(1, pool_size), pick)]
        return list(combinations(range(pool_size), pick))
    seen: set[tuple[int, ...]] = set()
    indices = range(1, pool_size) if focused else range(pool_size)
    while len(seen) < _MATCH_CANDIDATES:
        tail = tuple(sorted(random.sample(indices, pick)))
        seen.add((0, *tail) if focused else tail)
    return list(seen)


def select_match(
    store: DataStore, size: int, focus_id: int | None = None
) -> tuple[dict, ...] | None:
    """size개 항목의 대결을 고릅니다. 고를 수 없으면 None입니다."""
    if len(store.items) < size:
        return None
    focus = store.get_item(focus_id) if focus_id is not None else None
    if focus_id is not None and focus is None:
        return None
    others = [item for item in store.items if item["id"] != focus_id]
    limit = _MATCH_POOL_SIZE - bool(focus)
    pool = random.sample(others, min(limit, len(others)))
    if focus is not None:
        pool.insert(0, focus)
    options = _sample_candidates(len(pool), size, focused=focus is not None)
    recent = store.recent_votes()
    if store.active_round:
        recent.append(store.active_round)
    recent_pairs = set()
    for payload in recent:
        recent_pairs.update(
            frozenset(pair) for pair in combinations(vote_ids(payload), 2)
        )
    repeats = [
        sum(
            frozenset((pool[i]["id"], pool[j]["id"])) in recent_pairs
            for i, j in combinations(candidate, 2)
        )
        for candidate in options
    ]
    least = min(repeats)
    options = [
        candidate
        for candidate, repeated in zip(options, repeats, strict=True)
        if repeated == least
    ]
    if len(options) == 1:
        chosen = options[0]
    else:
        scores = candidate_utilities(store, pool, options)
        best = float(scores.max())
        tied = np.flatnonzero(np.isclose(scores, best, rtol=1e-10, atol=1e-14))
        chosen = options[int(random.choice(tied))]
    selected = [pool[i] for i in chosen]
    random.shuffle(selected)
    return tuple(selected)


def competition_ranks(scores: list[float]) -> list[int]:
    """내림차순 정렬된 점수 목록에 공동 순위(1, 1, 3)를 매깁니다."""
    ranks: list[int] = []
    previous: float | None = None
    rank = 0
    for position, score in enumerate(scores, 1):
        if previous is None or score != previous:
            rank = position
        ranks.append(rank)
        previous = score
    return ranks


def get_item_ranks(store: DataStore) -> tuple[dict[int, int], int]:
    scored = sorted(
        ((composite_rating(store, item), item["id"]) for item in store.items),
        key=lambda x: x[0],
        reverse=True,
    )
    ranked = competition_ranks([score for score, _ in scored])
    return (
        {iid: rank for (_, iid), rank in zip(scored, ranked, strict=True)},
        len(scored),
    )


# Dense joint sampling is O(n^3); beyond this the page shows point ranks only.
_RANK_SAMPLE_ITEMS = 1000


def rank_uncertainty(
    store: DataStore, sort_by: str, samples: int = 1000
) -> dict[str, Any] | None:
    """90% rank intervals and adjacent-order confidence from the joint Laplace fit.

    Draws composite scores from the per-criterion Gaussian approximations, which
    are independent across criteria. A fixed seed keeps a page stable on reload.
    """
    items = store.items
    criteria = (
        store.criteria
        if sort_by == "total"
        else [c for c in store.criteria if "criterion:" + c["key"] == sort_by]
    )
    if not 2 <= len(items) <= _RANK_SAMPLE_ITEMS or not criteria:
        return None
    ids = [item["id"] for item in items]
    rng = np.random.default_rng(0)
    total_weight = sum(c["weight"] for c in criteria)
    draws = np.zeros((samples, len(ids)))
    point_scores = np.zeros(len(ids))
    for criterion in criteria:
        key = criterion["key"]
        weight = criterion["weight"] / total_weight
        posterior = store.posterior(key)
        draws += weight * posterior.sample(ids, samples, rng)
        point_scores += weight * np.array([item["mu"][key] for item in items])
    ranks = np.empty((samples, len(ids)), dtype=int)
    np.put_along_axis(
        ranks, np.argsort(-draws, axis=1), np.arange(1, len(ids) + 1), axis=1
    )
    low, high = np.percentile(ranks, [5, 95], axis=0, method="nearest")
    # 실제 랭킹 표에 표시되는 MAP 순서와 동일하게 정렬해 in-sample 편향을 피합니다.
    order = np.array(
        sorted(
            range(len(items)),
            key=lambda i: (-float(point_scores[i]), items[i]["name"], items[i]["id"]),
        )
    )
    adjacent = (draws[:, order[:-1]] > draws[:, order[1:]]).mean(axis=0)
    return {
        "intervals": {
            iid: (int(lo), int(hi)) for iid, lo, hi in zip(ids, low, high, strict=True)
        },
        "adjacent_confidence": float(adjacent.mean()),
    }
