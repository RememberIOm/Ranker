"""Static generalized Plackett–Luce MAP fit and joint Laplace uncertainty.

A ballot is an ordered partition of two or three items. Each choice selects a
nonempty tied subset with log weight mean(skill) + log(tie prevalence).
Independent N(0, initial_sigma²) skills and N(0, 1) log tie prevalences make the
objective strictly convex. No online updates or pseudo-independent rank breaking.
"""

from collections import Counter
from dataclasses import dataclass
from functools import cached_property, lru_cache
from itertools import combinations
import math
from typing import Iterable

import numpy as np
from numpy.typing import NDArray
from scipy.integrate import cubature
from scipy.optimize import minimize
from scipy.sparse import coo_matrix, csc_matrix, diags
from scipy.sparse.linalg import splu
from scipy.special import logsumexp, softmax

ALGORITHM_VERSION = "gpl-map-v2"
Ranking = tuple[tuple[int, ...], ...]


class FitConvergenceError(RuntimeError):
    """A fit must converge before replacing any saved rating."""


def canonical_ranking(groups: Iterable[Iterable[int]]) -> Ranking:
    result = tuple(tuple(sorted(group)) for group in groups)
    ids = [i for group in result for i in group]
    if (
        not result
        or any(not group for group in result)
        or len(ids) not in (2, 3)
        or len(set(ids)) != len(ids)
        or any(type(i) is not int or not 0 < i <= 2**63 - 1 for i in ids)
    ):
        raise ValueError("서로 다른 2~3개 항목의 완전 순위 또는 동률이 필요합니다.")
    return result


def ballot_ranking(ids: list[int], vote: str | dict[str, str]) -> Ranking | None:
    """Validate UI semantics, including callers that bypass request validation."""
    if len(ids) not in (2, 3) or len(set(ids)) != len(ids):
        raise ValueError("대결 항목은 서로 달라야 합니다.")
    if vote == "skip":
        return None
    if len(ids) == 2:
        a, b = ids
        choices = {"1": ((a,), (b,)), "2": ((b,), (a,)), "draw": ((a, b),)}
        if not isinstance(vote, str) or vote not in choices:
            raise ValueError("알 수 없는 투표 값입니다.")
        return canonical_ranking(choices[vote])
    if not isinstance(vote, dict):
        raise ValueError("3개 대결의 순위가 필요합니다.")
    roles: dict[int, str] = {}
    for key, role in vote.items():
        try:
            iid = int(key)
        except (TypeError, ValueError):
            raise ValueError("숫자가 아닌 항목 ID입니다.") from None
        if iid in roles or iid not in ids or role not in {"best", "worst", "tied"}:
            raise ValueError("중복되거나 알 수 없는 항목·역할입니다.")
        roles[iid] = role
    best = [i for i, role in roles.items() if role == "best"]
    worst = [i for i, role in roles.items() if role == "worst"]
    tied = [i for i, role in roles.items() if role == "tied"]
    if len(best) == len(worst) == 1 and not tied:
        middle = [i for i in ids if i not in roles]
        return canonical_ranking([best, middle, worst])
    if len(best) == 1 and not worst and len(tied) == 2:
        return canonical_ranking([best, tied])
    if len(worst) == 1 and not best and len(tied) == 2:
        return canonical_ranking([tied, worst])
    if not best and not worst and len(tied) == 3:
        return canonical_ranking([tied])
    raise ValueError("최고·최하 또는 명시적인 동률을 선택해주세요.")


@lru_cache(maxsize=2)
def choice_features(size: int) -> tuple[tuple[tuple[int, ...], ...], NDArray]:
    subsets = tuple(
        group for n in range(1, size + 1) for group in combinations(range(size), n)
    )
    features = np.zeros((len(subsets), size + 2))
    for row, group in enumerate(subsets):
        features[row, list(group)] = 1 / len(group)
        if len(group) > 1:
            features[row, size + len(group) - 2] = 1
    return subsets, features


class RankingLikelihood:
    """Vectorized choice stages, with at most five parameters per stage."""

    def __init__(self, observations: list[tuple[Ranking, int]], prior_variance: float):
        self.item_ids = sorted(
            {i for groups, _ in observations for group in groups for i in group}
        )
        index = {iid: i for i, iid in enumerate(self.item_ids)}
        n = len(index)
        self.prior_precision = np.array([1 / prior_variance] * n + [1.0, 1.0])
        stages = []
        for groups, count in observations:
            remaining = sorted(i for group in groups for i in group)
            for group in groups:
                if len(remaining) == 1:
                    break
                size = len(remaining)
                subsets, features = choice_features(size)
                chosen = tuple(remaining.index(i) for i in group)
                selected = subsets.index(chosen)
                indices = [index[i] for i in remaining] + [n, n + 1]
                stages.append((indices, features, selected, count))
                remaining = [i for i in remaining if i not in group]
        self.indices = np.zeros((len(stages), 5), dtype=int)
        self.features = np.zeros((len(stages), 7, 5))
        self.offsets = np.full((len(stages), 7), -np.inf)
        self.chosen = np.zeros((len(stages), 5))
        self.counts = np.array([stage[3] for stage in stages], dtype=float)
        for row, (indices, features, selected, _) in enumerate(stages):
            width, options = len(indices), len(features)
            self.indices[row, :width] = indices
            self.features[row, :options, :width] = features
            self.offsets[row, :options] = 0
            self.chosen[row, :width] = features[selected]

    def _probabilities(self, x: NDArray) -> tuple[NDArray, NDArray]:
        scores = np.einsum("sod,sd->so", self.features, x[self.indices]) + self.offsets
        return scores, softmax(scores, axis=1)

    def value_gradient(self, x: NDArray) -> tuple[float, NDArray]:
        scores, probs = self._probabilities(x)
        local = np.einsum("so,sod->sd", probs, self.features) - self.chosen
        gradient = x * self.prior_precision
        np.add.at(
            gradient, self.indices.ravel(), (local * self.counts[:, None]).ravel()
        )
        observed = np.einsum("sd,sd->s", self.chosen, x[self.indices])
        value = 0.5 * np.dot(x * self.prior_precision, x)
        value += np.dot(self.counts, logsumexp(scores, axis=1) - observed)
        return float(value), gradient

    def precision(self, x: NDArray) -> csc_matrix:
        _, probs = self._probabilities(x)
        average = np.einsum("so,sod->sd", probs, self.features)
        blocks = (
            np.einsum("so,soi,soj->sij", probs, self.features, self.features)
            - np.einsum("si,sj->sij", average, average)
        ) * self.counts[:, None, None]
        rows = np.broadcast_to(self.indices[:, :, None], blocks.shape).ravel()
        cols = np.broadcast_to(self.indices[:, None, :], blocks.shape).ravel()
        result = coo_matrix(
            (blocks.ravel(), (rows, cols)), shape=(len(x), len(x))
        ).tocsc()
        result += diags(self.prior_precision, format="csc")
        result.eliminate_zeros()
        return result


@dataclass
class Posterior:
    item_ids: tuple[int, ...]
    location: NDArray
    precision: csc_matrix
    prior_variance: float

    @cached_property
    def index(self) -> dict[int, int]:
        return {iid: i for i, iid in enumerate(self.item_ids)}

    @cached_property
    def factor(self):
        try:
            return splu(self.precision)
        except RuntimeError as exc:
            raise FitConvergenceError(
                "결합 불확실성을 계산할 수 없습니다. 저장하지 않았습니다."
            ) from exc

    def mean(self, iid: int) -> float:
        return float(self.location[self.index[iid]]) if iid in self.index else 0.0

    def covariance(self, ids: list[int]) -> NDArray:
        """Joint covariance of requested skills followed by the two tie parameters.

        Unobserved items retain independent priors; solving selected columns avoids
        constructing a dense inverse of every item in a large ranking.
        """
        if len(set(ids)) != len(ids):
            raise ValueError("중복된 항목입니다.")
        n = len(self.item_ids)
        indices = [self.index.get(i) for i in ids] + [n, n + 1]
        known = [(row, idx) for row, idx in enumerate(indices) if idx is not None]
        rhs = np.zeros((n + 2, len(known)))
        rhs[[idx for _, idx in known], np.arange(len(known))] = 1
        solved = self.factor.solve(rhs)
        result = np.zeros((len(ids) + 2, len(ids) + 2))
        rows, cols = zip(*known)
        result[np.ix_(rows, rows)] = solved[list(cols), :]
        for row, idx in enumerate(indices):
            if idx is None:
                result[row, row] = self.prior_variance
        return (result + result.T) / 2

    def marginal_variances(self) -> dict[int, float]:
        # Bounded RHS memory even when many observed items are present.
        values = {}
        n = len(self.item_ids)
        for start in range(0, n, 64):
            stop = min(start + 64, n)
            rhs = np.zeros((n + 2, stop - start))
            rhs[np.arange(start, stop), np.arange(stop - start)] = 1
            solved = self.factor.solve(rhs)
            for j, idx in enumerate(range(start, stop)):
                values[self.item_ids[idx]] = float(solved[idx, j])
        return values

    def to_dict(self) -> dict:
        coo = self.precision.tocoo()
        return {
            "algorithm_version": ALGORITHM_VERSION,
            "item_ids": list(self.item_ids),
            "location": self.location.tolist(),
            "prior_variance": self.prior_variance,
            "rows": coo.row.tolist(),
            "cols": coo.col.tolist(),
            "values": coo.data.tolist(),
        }

    @classmethod
    def from_dict(cls, state: dict) -> "Posterior":
        n = len(state["location"])
        return cls(
            tuple(state["item_ids"]),
            np.array(state["location"]),
            coo_matrix(
                (state["values"], (state["rows"], state["cols"])), shape=(n, n)
            ).tocsc(),
            state["prior_variance"],
        )


def fit_rankings(
    rankings: Iterable[Ranking],
    *,
    counts: Iterable[int] | None = None,
    initial_sigma: float = 2.0,
) -> Posterior:
    if not math.isfinite(initial_sigma) or not 0.1 <= initial_sigma <= 10:
        raise ValueError("초기 표준편차는 0.1~10이어야 합니다.")
    groups = [canonical_ranking(r) for r in rankings]
    weights = list(counts) if counts is not None else [1] * len(groups)
    if len(weights) != len(groups) or any(type(w) is not int or w < 1 for w in weights):
        raise ValueError("관측 수는 양의 정수여야 합니다.")
    aggregated = Counter()
    for ranking, count in zip(groups, weights):
        aggregated[ranking] += count
    objective = RankingLikelihood(sorted(aggregated.items()), initial_sigma**2)
    x = np.zeros(len(objective.item_ids) + 2)
    if groups:
        result = minimize(
            objective.value_gradient,
            x,
            jac=True,
            method="L-BFGS-B",
            options={"gtol": 1e-9, "ftol": 1e-14, "maxiter": 1000, "maxls": 50},
        )
        x = result.x
        _, gradient = objective.value_gradient(x)
        if (
            not np.isfinite(x).all()
            or not np.isfinite(gradient).all()
            or np.max(np.abs(gradient)) > 5e-5
        ):
            raise FitConvergenceError(
                "평점 계산이 수렴하지 않았습니다. 저장하지 않았습니다."
            )
    return Posterior(
        tuple(objective.item_ids), x, objective.precision(x), initial_sigma**2
    )


def ranking_probability(
    mus: dict[int, float], log_ties: tuple[float, float], ranking: Ranking
) -> float:
    ranking = canonical_ranking(ranking)
    remaining = sorted(i for group in ranking for i in group)
    logp = 0.0
    for group in ranking:
        if len(remaining) == 1:
            break
        subsets, features = choice_features(len(remaining))
        scores = features @ np.array([mus[i] for i in remaining] + list(log_ties))
        selected = subsets.index(tuple(remaining.index(i) for i in group))
        logp += scores[selected] - logsumexp(scores)
        remaining = [i for i in remaining if i not in group]
    return float(np.exp(logp))


def predict_pairs(posterior: Posterior, pairs: list[tuple[int, int]]) -> NDArray:
    """Adaptive joint-Laplace predictive integration for one or more pairs.

    Integrates skill differences and log tie prevalence with their covariance.
    Gaussian tails beyond ten standard deviations contribute less than 4e-23.
    Numerical tolerance does not imply empirical probability calibration.
    """
    if not pairs:
        return np.empty((0, 3))
    if any(a == b for a, b in pairs):
        raise ValueError("서로 다른 항목을 비교해야 합니다.")
    ids = sorted({i for pair in pairs for i in pair})
    index = {iid: i for i, iid in enumerate(ids)}
    covariance = posterior.covariance(ids)
    a = np.array([index[pair[0]] for pair in pairs])
    b = np.array([index[pair[1]] for pair in pairs])
    delta = np.array([posterior.mean(x) - posterior.mean(y) for x, y in pairs])
    tie = float(posterior.location[-2])
    tie_sd = math.sqrt(max(0.0, covariance[-2, -2]))
    slope = (
        (covariance[a, -2] - covariance[b, -2]) / tie_sd
        if tie_sd
        else np.zeros(len(pairs))
    )
    difference_sd = np.sqrt(
        np.maximum(
            0.0, covariance[a, a] + covariance[b, b] - 2 * covariance[a, b] - slope**2
        )
    )

    def integrand(points):
        d = delta + points[:, 0, None] * slope + points[:, 1, None] * difference_sd
        log_tie = np.broadcast_to(tie + tie_sd * points[:, 0, None], d.shape)
        density = np.exp(-np.sum(points**2, axis=1) / 2) / (2 * math.pi)
        return (
            softmax(np.stack((d / 2, log_tie, -d / 2), axis=-1), axis=-1)
            * density[:, None, None]
        )

    integral = cubature(
        integrand,
        [-10.0, -10.0],
        [10.0, 10.0],
        atol=2e-9,
        rtol=2e-8,
        max_subdivisions=2000,
    )
    if integral.status != "converged" or not np.isfinite(integral.estimate).all():
        raise FitConvergenceError("예측 확률 적분이 수렴하지 않았습니다.")
    result = np.maximum(integral.estimate, 0.0)
    return result / result.sum(axis=1, keepdims=True)


def predict_pair(posterior: Posterior, a: int, b: int) -> tuple[float, float, float]:
    return tuple(float(p) for p in predict_pairs(posterior, [(a, b)])[0])


def expected_information(locations: NDArray, size: int) -> NDArray:
    """Expected full-ballot Fisher information at the supplied MAP locations."""
    _, features = choice_features(size)
    probabilities = softmax(locations @ features.T, axis=1)
    average = probabilities @ features
    info = np.einsum("bo,oi,oj->bij", probabilities, features, features) - np.einsum(
        "bi,bj->bij", average, average
    )
    if size == 3:
        # Only a singleton first choice leaves a second informative stage.
        for first in range(3):
            remaining = [i for i in range(3) if i != first] + [3, 4]
            sub = expected_information(locations[:, remaining], 2)
            info[:, np.array(remaining)[:, None], remaining] += (
                probabilities[:, first, None, None] * sub
            )
    return info
