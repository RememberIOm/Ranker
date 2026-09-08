"""Display and matchmaking based on the fitted joint ranking model."""

from __future__ import annotations

import math
import random
from itertools import combinations
from typing import TYPE_CHECKING, Any

import numpy as np

from ranker.rating_engine import expected_information, predict_pair
from scipy.special import roots_legendre

if TYPE_CHECKING:
    from ranker.store import DataStore


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
    return dict(zip(("win_a", "draw", "win_b"), (float(x) / 10 for x in ticks)))


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
        cross = covariance[np.arange(n)[None, :, None], indices[:, None, :]]
        fisher = expected_information(means[indices], size)
        reduction = np.linalg.solve(np.eye(size + 2) + fisher @ local, fisher)
        weight = criterion["weight"] / total_weight
        composite_mean += weight * means[:n]
        composite_covariance += weight**2 * covariance[:n, :n]
        resolved += (
            weight**2
            * store.response_rate(key)
            * np.einsum("bni,bij,bmj->bnm", cross, reduction, cross, optimize=True)
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
    nodes, weights = roots_legendre(12)
    for node, weight in zip(nodes, weights):
        theta = angle * (node + 1) / 2
        integral += weight * np.exp(-z_squared / (1 + np.sin(theta)))
    scores = np.mean(angle * integral / (4 * math.pi), axis=1)
    return scores / (size - 1)


def _select_match(
    store: DataStore, size: int, focus_id: int | None
) -> tuple[dict, ...] | None:
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
    options = (
        [(0, *tail) for tail in combinations(range(1, len(pool)), size - 1)]
        if focus is not None
        else list(combinations(range(len(pool)), size))
    )
    if len(options) > _MATCH_CANDIDATES:
        options = random.sample(options, _MATCH_CANDIDATES)
    recent = store.recent_votes(5)
    if store.active_round:
        recent.append(store.active_round)
    recent_pairs = set()
    for payload in recent:
        ids = [
            payload[k]
            for k in ("item1_id", "item2_id", "item3_id")
            if payload.get(k) is not None
        ]
        recent_pairs.update(frozenset(pair) for pair in combinations(ids, 2))
    repeats = [
        sum(
            frozenset((pool[i]["id"], pool[j]["id"])) in recent_pairs
            for i, j in combinations(candidate, 2)
        )
        for candidate in options
    ]
    least = min(repeats)
    options = [
        candidate for candidate, repeated in zip(options, repeats) if repeated == least
    ]
    scores = candidate_utilities(store, pool, options)
    best = float(scores.max())
    tied = np.flatnonzero(np.isclose(scores, best, rtol=1e-10, atol=1e-14))
    selected = [pool[i] for i in options[int(random.choice(tied))]]
    random.shuffle(selected)
    return tuple(selected)


def get_match_pair(
    store: DataStore, focus_id: int | None = None
) -> tuple[dict | None, dict | None]:
    selected = _select_match(store, 2, focus_id)
    return selected if selected else (None, None)


def get_match_triple(
    store: DataStore, focus_id: int | None = None
) -> tuple[dict | None, dict | None, dict | None]:
    selected = _select_match(store, 3, focus_id)
    return selected if selected else (None, None, None)


def get_item_ranks(store: DataStore) -> tuple[dict[int, int], int]:
    scores = sorted(
        ((composite_rating(store, item), item["id"]) for item in store.items),
        key=lambda x: x[0],
        reverse=True,
    )
    ranks = {}
    previous = None
    rank = 0
    for position, (score, iid) in enumerate(scores, 1):
        if previous is None or score != previous:
            rank = position
        ranks[iid] = rank
        previous = score
    return ranks, len(scores)


def get_item_rank(store: DataStore, item_id: int) -> tuple[int, int]:
    ranks, total = get_item_ranks(store)
    return ranks.get(item_id, total), total
