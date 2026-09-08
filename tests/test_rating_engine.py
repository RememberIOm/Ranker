"""Inference contracts: observations, rather than update order, determine ratings."""

from itertools import permutations

import numpy as np
import pytest

from ranker.rating_engine import (
    RankingLikelihood,
    fit_rankings,
    ranking_probability,
    predict_pair,
    expected_information,
)


def approx_derivative(function, x):
    step = np.eye(len(x)) * 1e-5
    return np.stack(
        [
            (np.asarray(function(x + d)) - np.asarray(function(x - d))) / 2e-5
            for d in step
        ],
        axis=-1,
    )


def test_balanced_observations_are_order_independent():
    wins, losses = ((1,), (2,)), ((2,), (1,))
    batches = [
        [wins] * 10 + [losses] * 10,
        [losses] * 10 + [wins] * 10,
        [wins, losses] * 10,
    ]
    fits = [fit_rankings(batch, initial_sigma=2) for batch in batches]
    for fit in fits:
        assert fit.mean(1) == pytest.approx(fit.mean(2), abs=1e-8)
        assert predict_pair(fit, 1, 2)[0] == pytest.approx(predict_pair(fit, 1, 2)[2])
        np.testing.assert_allclose(fit.location, fits[0].location, atol=1e-8)


def test_disconnected_components_keep_unknown_relative_location():
    fit = fit_rankings([((1, 2),)] * 1000 + [((3, 4),)] * 1000)
    cov = fit.covariance([1, 2, 3, 4])[:4, :4]
    assert cov[0, 0] + cov[2, 2] - 2 * cov[0, 2] >= 4 - 1e-8
    assert cov[0, 0] + cov[1, 1] - 2 * cov[0, 1] < 8
    assert fit.mean(999) == 0
    assert fit.covariance([999])[0, 0] == 4


def test_one_win_retains_common_offset_variance():
    fit = fit_rankings([((1,), (2,))])
    cov = fit.covariance([1, 2])
    assert fit.mean(1) > 0 > fit.mean(2)
    assert cov[0, 1] > 0
    assert (cov[0, 0] + cov[1, 1] + 2 * cov[0, 1]) / 4 == pytest.approx(2)


@pytest.mark.parametrize(
    "groups", [((1,), (2,), (3,)), ((1,), (2, 3)), ((1, 2), (3,)), ((1, 2, 3),)]
)
def test_every_three_way_response_has_a_joint_likelihood(groups):
    fit = fit_rankings([groups] * 8)
    assert np.isfinite(fit.location).all()
    for upper, lower in zip(groups, groups[1:]):
        assert min(fit.mean(i) for i in upper) > max(fit.mean(i) for i in lower)
    for group in groups:
        assert max(fit.mean(i) for i in group) - min(fit.mean(i) for i in group) < 1e-7


def test_rank_probabilities_partition_all_thirteen_outcomes():
    mu = {1: 1.3, 2: -0.8, 3: 0.4}
    ranks = [tuple((i,) for i in order) for order in permutations(mu)]
    for i in mu:
        tied = tuple(j for j in mu if j != i)
        ranks.extend([((i,), tied), (tied, (i,))])
    ranks.append(((1, 2, 3),))
    assert sum(ranking_probability(mu, (0.2, -0.4), r) for r in ranks) == pytest.approx(
        1
    )


def test_likelihood_derivatives_and_expected_information():
    objective = RankingLikelihood([(((1,), (2, 3)), 3), (((3,), (1,)), 2)], 4)
    x = np.array([0.8, -0.3, 0.2, -0.7, 0.4])
    _, gradient = objective.value_gradient(x)
    numeric_grad = approx_derivative(
        lambda y: objective.value_gradient(y)[0], x
    ).ravel()
    np.testing.assert_allclose(gradient, numeric_grad, atol=1e-6)
    numeric_hessian = approx_derivative(lambda y: objective.value_gradient(y)[1], x)
    np.testing.assert_allclose(
        objective.precision(x).toarray(), numeric_hessian, atol=1e-6
    )
    info = expected_information(x[None, :], 3)[0]
    assert np.linalg.eigvalsh(info).min() > -1e-10
    np.testing.assert_allclose(info @ np.array([1, 1, 1, 0, 0]), 0, atol=1e-10)


def test_ties_are_learned_and_prediction_is_normalized():
    tied = fit_rankings([((1, 2),)] * 30)
    decisive = fit_rankings([((1,), (2,)), ((2,), (1,))] * 15)
    assert predict_pair(tied, 1, 2)[1] > predict_pair(decisive, 1, 2)[1]
    for sigma in (0.1, 2.0, 10.0):
        fit = fit_rankings([], initial_sigma=sigma)
        p = predict_pair(fit, 1, 2)
        assert min(p) >= 0
        assert sum(p) == pytest.approx(1, abs=1e-10)
        assert p[0] == pytest.approx(p[2], abs=1e-8)


def test_counted_observations_equal_expanded_and_tied_order_is_irrelevant():
    first = fit_rankings([((1,), (2, 3))] * 12)
    second = fit_rankings([((1,), (3, 2))], counts=[12])
    np.testing.assert_allclose(first.location, second.location, atol=1e-10)
    np.testing.assert_allclose(
        first.covariance([1, 2, 3]), second.covariance([1, 2, 3])
    )


def test_full_ballot_fisher_matches_all_thirteen_score_outer_products():
    x = np.array([0.8, -0.3, 0.2, -0.7, 0.4])
    ranks = [tuple((i,) for i in order) for order in permutations([1, 2, 3])]
    for iid in (1, 2, 3):
        tied = tuple(i for i in (1, 2, 3) if i != iid)
        ranks.extend([((iid,), tied), (tied, (iid,))])
    ranks.append(((1, 2, 3),))
    expected = np.zeros((5, 5))
    for ranking in ranks:

        def log_probability(y):
            return np.log(
                ranking_probability(dict(zip([1, 2, 3], y[:3])), tuple(y[3:]), ranking)
            )

        gradient = approx_derivative(log_probability, x)
        expected += np.exp(log_probability(x)) * np.outer(gradient, gradient)
    np.testing.assert_allclose(
        expected_information(x[None, :], 3)[0], expected, atol=1e-8
    )


@pytest.mark.parametrize("sigma", [0.1, 2.0, 10.0])
def test_adaptive_prediction_against_independent_high_order_gaussian_quadrature(sigma):
    from scipy.special import roots_hermitenorm, softmax

    z, w = roots_hermitenorm(1024 if sigma == 10 else 128)
    w /= np.sqrt(2 * np.pi)
    d = np.broadcast_to(np.sqrt(2) * sigma * z[:, None], (len(z), len(z)))
    tie = np.broadcast_to(z[None, :], d.shape)
    point_probabilities = softmax(np.stack([d / 2, tie, -d / 2], axis=-1), axis=-1)
    reference = np.einsum("i,j,ijc->c", w, w, point_probabilities)
    prediction = predict_pair(fit_rankings([], initial_sigma=sigma), 1, 2)
    np.testing.assert_allclose(prediction, reference, atol=2e-8, rtol=0)
