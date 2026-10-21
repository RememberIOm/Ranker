"""Static generalized Plackett–Luce MAP fit and joint Laplace uncertainty.

A ballot is an ordered partition of two or three items. Each choice selects a
nonempty tied subset with log weight mean(skill) + log(tie prevalence).
Independent N(0, initial_sigma²) skills and N(0, 1) log tie prevalences make the
objective strictly convex. No online updates or pseudo-independent rank breaking.
"""

import math
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from functools import cached_property, lru_cache
from itertools import combinations

import numpy as np
from numpy.typing import NDArray
from scipy.integrate import cubature
from scipy.linalg import LinAlgError, cho_factor, cho_solve, solve_triangular
from scipy.special import logsumexp, softmax

ALGORITHM_VERSION = "gpl-map-v2"
Ranking = tuple[tuple[int, ...], ...]


_BAD_BALLOT = "대결 정보가 올바르지 않습니다. 새로고침한 뒤 다시 투표하세요."


class FitConvergenceError(RuntimeError):
    """A fit must converge before replacing any saved rating."""


def canonical_ranking(groups: Iterable[Iterable[int]]) -> Ranking:
    result = tuple(tuple(sorted(group)) for group in groups)
    ids = [*group for group in result]
    if (
        not result
        or any(not group for group in result)
        or len(ids) not in (2, 3)
        or len(set(ids)) != len(ids)
        or any(type(i) is not int or not 0 < i <= 2**63 - 1 for i in ids)
    ):
        raise ValueError(_BAD_BALLOT)
    return result


def ballot_ranking(ids: list[int], vote: str | dict[str, str]) -> Ranking | None:
    """Validate UI semantics, including callers that bypass request validation."""
    if len(ids) not in (2, 3) or len(set(ids)) != len(ids):
        raise ValueError(_BAD_BALLOT)
    if vote == "skip":
        return None
    if len(ids) == 2:
        a, b = ids
        choices = {"1": ((a,), (b,)), "2": ((b,), (a,)), "draw": ((a, b),)}
        if not isinstance(vote, str) or vote not in choices:
            raise ValueError(_BAD_BALLOT)
        return canonical_ranking(choices[vote])
    if not isinstance(vote, dict):
        # Callers treat every malformed ballot as a validation error.
        raise ValueError(_BAD_BALLOT)  # noqa: TRY004
    roles: dict[int, str] = {}
    for key, role in vote.items():
        try:
            iid = int(key)
        except TypeError, ValueError:
            raise ValueError(_BAD_BALLOT) from None
        if iid in roles or iid not in ids or role not in {"best", "worst", "tied"}:
            raise ValueError(_BAD_BALLOT)
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
    raise ValueError("최하 항목이나 동률을 마저 고르세요.")


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
            {*group for groups, _ in observations for group in groups}
        )
        index = {iid: i for i, iid in enumerate(self.item_ids)}
        n = len(index)
        self.prior_precision = np.array([1 / prior_variance] * n + [1.0, 1.0])
        stages = []
        for groups, count in observations:
            remaining = sorted(*group for group in groups)
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

    def precision(self, x: NDArray) -> NDArray:
        """Dense Hessian. Random ballots link items so widely that a sparse
        factorization fills in 40-75% of the matrix and only adds overhead."""
        _, probs = self._probabilities(x)
        average = np.einsum("so,sod->sd", probs, self.features)
        blocks = (
            np.einsum("so,soi,soj->sij", probs, self.features, self.features)
            - np.einsum("si,sj->sij", average, average)
        ) * self.counts[:, None, None]
        rows = np.broadcast_to(self.indices[:, :, None], blocks.shape)
        cols = np.broadcast_to(self.indices[:, None, :], blocks.shape)
        result = np.diag(self.prior_precision)
        np.add.at(result, (rows, cols), blocks)
        return result


@dataclass
class Posterior:
    """Laplace approximation. `factor` is the lower Cholesky factor of the
    precision; only its lower triangle is meaningful."""

    item_ids: tuple[int, ...]
    location: NDArray
    factor: NDArray
    prior_variance: float

    @cached_property
    def index(self) -> dict[int, int]:
        return {iid: i for i, iid in enumerate(self.item_ids)}

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
        solved = cho_solve((self.factor, True), rhs)
        result = np.zeros((len(ids) + 2, len(ids) + 2))
        rows, cols = zip(*known)
        result[np.ix_(rows, rows)] = solved[list(cols), :]
        for row, idx in enumerate(indices):
            if idx is None:
                result[row, row] = self.prior_variance
        return (result + result.T) / 2

    def marginal_variances(self) -> dict[int, float]:
        # With precision = LLᵀ, the covariance diagonal is the column norms of L⁻¹.
        # Column blocks keep the temporaries far below one n×n array.
        size = len(self.location)
        variances = np.empty(size)
        for start in range(0, size, 256):
            stop = min(start + 256, size)
            rhs = np.zeros((size, stop - start))
            rhs[np.arange(start, stop), np.arange(stop - start)] = 1
            block = solve_triangular(self.factor, rhs, lower=True)
            variances[start:stop] = np.einsum("ij,ij->j", block, block)
        return dict(zip(self.item_ids, variances[:-2].tolist()))

    def sample(self, ids: list[int], samples: int, rng: np.random.Generator) -> NDArray:
        """Joint draws of the requested skills, shaped (samples, len(ids)).

        location + L⁻ᵀz has covariance (LLᵀ)⁻¹, so the dense covariance is never
        formed. Unobserved items keep independent prior draws.
        """
        noise = rng.standard_normal((len(self.location), samples))
        joint = self.location[:, None] + solve_triangular(
            self.factor, noise, lower=True, trans="T"
        )
        draws = rng.standard_normal((samples, len(ids))) * math.sqrt(
            self.prior_variance
        )
        for col, iid in enumerate(ids):
            if iid in self.index:
                draws[:, col] = joint[self.index[iid]]
        return draws

    def to_dict(self) -> dict:
        """The factor is not saved; restore_posterior rebuilds it."""
        return {
            "algorithm_version": ALGORITHM_VERSION,
            "item_ids": list(self.item_ids),
            "location": self.location.tolist(),
            "prior_variance": self.prior_variance,
        }


def _cholesky(precision: NDArray) -> NDArray | None:
    """Lower factor, overwriting the precision so one n×n array stays alive."""
    try:
        return cho_factor(precision, lower=True, overwrite_a=True)[0]
    except LinAlgError, ValueError:
        return None


def _newton(objective: RankingLikelihood, x: NDArray) -> tuple[NDArray, NDArray]:
    """Damped Newton with the exact Hessian of the strictly convex objective.

    Returns the optimum and the Cholesky factor of its precision. Stops when the
    Newton decrement, an affine-invariant estimate of the remaining objective gap,
    is negligible. Absolute gradient thresholds grow with counts.
    """
    value, gradient = objective.value_gradient(x)
    for _ in range(100):
        factor = _cholesky(objective.precision(x))
        if factor is None:
            break
        step = cho_solve((factor, True), -gradient)
        decrement = float(-gradient @ step)
        if not math.isfinite(decrement):
            break
        if decrement <= 1e-13 * max(1.0, abs(value)):
            # The last step squares the remaining error, so warm and cold starts
            # land on the same optimum up to rounding.
            x = x + step
            factor = _cholesky(objective.precision(x))
            if factor is None:
                break
            return x, factor
        scale = 1.0
        while scale > 1e-12:
            candidate = x + scale * step
            new_value, new_gradient = objective.value_gradient(candidate)
            if new_value <= value - 0.25 * scale * decrement:
                break
            scale /= 2
        else:
            break
        x, value, gradient = candidate, new_value, new_gradient
    raise FitConvergenceError("점수를 계산하지 못해 저장하지 않았습니다.")


def _objective(
    rankings: Iterable[Ranking], counts: Iterable[int] | None, prior_variance: float
) -> RankingLikelihood:
    groups = [canonical_ranking(r) for r in rankings]
    weights = list(counts) if counts is not None else [1] * len(groups)
    if len(weights) != len(groups) or any(type(w) is not int or w < 1 for w in weights):
        raise ValueError("관측 수는 양의 정수여야 합니다.")
    aggregated = Counter()
    for ranking, count in zip(groups, weights, strict=True):
        aggregated[ranking] += count
    return RankingLikelihood(sorted(aggregated.items()), prior_variance)


def fit_rankings(
    rankings: Iterable[Ranking],
    *,
    counts: Iterable[int] | None = None,
    initial_sigma: float = 2.0,
    start: dict | None = None,
) -> Posterior:
    """MAP fit. `start` is a previous fit's saved state; after one added or
    removed ballot its location is about three Newton steps from the optimum."""
    if not math.isfinite(initial_sigma) or not 0.1 <= initial_sigma <= 10:
        raise ValueError("초기 표준편차는 0.1~10이어야 합니다.")
    objective = _objective(rankings, counts, initial_sigma**2)
    x = np.zeros(len(objective.item_ids) + 2)
    if start is not None:
        previous = dict(zip(start["item_ids"], start["location"]))
        x[:-2] = [previous.get(iid, 0.0) for iid in objective.item_ids]
        x[-2:] = start["location"][-2:]
    x, factor = _newton(objective, x)
    return Posterior(tuple(objective.item_ids), x, factor, initial_sigma**2)


def restore_posterior(
    state: dict, rankings: Iterable[Ranking], *, counts: Iterable[int]
) -> Posterior:
    """Rebuild a saved fit; the precision is the exact Hessian at its location."""
    objective = _objective(rankings, counts, state["prior_variance"])
    if objective.item_ids != list(state["item_ids"]):
        raise ValueError("평점 모형과 관측의 항목이 다릅니다.")
    location = np.array(state["location"])
    factor = _cholesky(objective.precision(location))
    if factor is None:
        raise FitConvergenceError("점수를 계산하지 못해 저장하지 않았습니다.")
    return Posterior(
        tuple(objective.item_ids), location, factor, state["prior_variance"]
    )


def ranking_probability(
    mus: dict[int, float], log_ties: tuple[float, float], ranking: Ranking
) -> float:
    ranking = canonical_ranking(ranking)
    remaining = sorted(*group for group in ranking)
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
    ids = sorted({*pair for pair in pairs})
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
        raise FitConvergenceError("예상 확률을 계산하지 못했습니다.")
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
