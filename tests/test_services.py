import math
from itertools import combinations

import pytest

import store
from services import (
    _triple_eig,
    bt_update,
    composite_rating,
    display_rating,
    display_uncertainty,
    get_item_rank,
    get_match_pair,
    get_match_probabilities,
    get_match_triple,
    hierarchical_shrinkage,
    sigmoid,
)


# --- Sigmoid ---


class TestSigmoid:
    def test_zero(self) -> None:
        assert sigmoid(0.0) == pytest.approx(0.5)

    def test_large_positive(self) -> None:
        assert sigmoid(100.0) == pytest.approx(1.0, abs=1e-5)

    def test_large_negative(self) -> None:
        assert sigmoid(-100.0) == pytest.approx(0.0, abs=1e-5)

    def test_clamp_extreme(self) -> None:
        """극단값에서도 오버플로우 없이 동작"""
        assert sigmoid(1000.0) == 1.0 / (1.0 + math.exp(-500.0))
        assert sigmoid(-1000.0) > 0.0


# --- BT Update ---


class TestBTUpdate:
    def test_winner_gains_loser_loses(self) -> None:
        mu_a, sq_a, mu_b, sq_b = bt_update(0.0, 4.0, 0.0, 4.0, 1.0)
        assert mu_a > 0.0
        assert mu_b < 0.0

    def test_symmetric_draw_no_mu_change(self) -> None:
        """동일 μ, outcome=0.5 → μ 변화 없음 (symmetric)"""
        mu_a, sq_a, mu_b, sq_b = bt_update(0.0, 4.0, 0.0, 4.0, 0.5)
        assert mu_a == pytest.approx(0.0, abs=1e-10)
        assert mu_b == pytest.approx(0.0, abs=1e-10)

    def test_variance_always_decreases(self) -> None:
        """모든 outcome에서 σ² 감소"""
        for outcome in [1.0, 0.5, 0.0]:
            _, sq_a, _, sq_b = bt_update(0.5, 4.0, -0.3, 3.0, outcome)
            assert sq_a < 4.0
            assert sq_b < 3.0

    def test_high_uncertainty_bigger_update(self) -> None:
        """높은 σ² → 큰 μ 변화"""
        mu_high, _, _, _ = bt_update(0.0, 10.0, 0.0, 10.0, 1.0)
        mu_low, _, _, _ = bt_update(0.0, 0.1, 0.0, 0.1, 1.0)
        assert abs(mu_high) > abs(mu_low)

    def test_sigma_floor(self) -> None:
        """σ² ≥ 0.01 보장"""
        _, sq_a, _, sq_b = bt_update(0.0, 0.01, 0.0, 0.01, 1.0)
        assert sq_a >= 0.01
        assert sq_b >= 0.01

    def test_convergence(self) -> None:
        """반복 승리 → μ_a >> μ_b"""
        mu_a, sq_a, mu_b, sq_b = 0.0, 4.0, 0.0, 4.0
        for _ in range(50):
            mu_a, sq_a, mu_b, sq_b = bt_update(mu_a, sq_a, mu_b, sq_b, 1.0)
        assert mu_a > 0.5
        assert mu_b < -0.5

    def test_no_shrink_to_zero_when_g_zero(self) -> None:
        """g=0 (예측 적중) 시 μ 불변 — 0 방향 수축 없음"""
        mu_a, mu_b = 1.5, 0.3
        p = sigmoid(mu_a - mu_b)  # outcome = p → g = 0
        new_a, _, new_b, _ = bt_update(mu_a, 2.0, mu_b, 2.0, p)
        assert new_a == pytest.approx(mu_a, abs=1e-10)
        assert new_b == pytest.approx(mu_b, abs=1e-10)

    def test_nonzero_mu_update_magnitude(self) -> None:
        """μ≠0에서 업데이트 크기가 충분히 큼 (회귀 테스트)"""
        mu_a, _, _, _ = bt_update(1.0, 2.0, 0.0, 2.0, 1.0)
        # 올바른 공식: mu_a = 1.0 + g/prec_new ≈ 1.197
        # 이전 버그 공식은 ≈ 1.024 를 반환했음
        assert mu_a > 1.1


# --- Hierarchical Shrinkage ---


class TestHierarchicalShrinkage:
    async def test_pulls_toward_cross_mean(self, temp_store: store.DataStore) -> None:
        item = {
            "mu": {"story": 2.0, "visual": 0.0, "ost": 0.0, "voice": 0.0, "char": 0.0, "fun": 0.0},
            "sigma_sq": {"story": 1.0, "visual": 1.0, "ost": 1.0, "voice": 1.0, "char": 1.0, "fun": 1.0},
        }
        old_story_mu = item["mu"]["story"]
        hierarchical_shrinkage(temp_store, item)
        # story가 2.0 → cross_mean 방향(< 2.0)으로 이동
        assert item["mu"]["story"] < old_story_mu

    async def test_preserves_sigma_sq(self, temp_store: store.DataStore) -> None:
        """계층적 축소는 σ²를 변경하지 않음 — 불확실성 감소는 bt_update만"""
        item = {
            "mu": {"story": 2.0, "visual": 0.0, "ost": 0.0, "voice": 0.0, "char": 0.0, "fun": 0.0},
            "sigma_sq": {"story": 1.0, "visual": 1.0, "ost": 1.0, "voice": 1.0, "char": 1.0, "fun": 1.0},
        }
        old_sigmas = dict(item["sigma_sq"])
        hierarchical_shrinkage(temp_store, item)
        for k in old_sigmas:
            assert item["sigma_sq"][k] == pytest.approx(old_sigmas[k])

    async def test_zero_strength_no_change(self, temp_store: store.DataStore) -> None:
        await temp_store.update_settings({"hierarchical_strength": 0.0})
        item = {
            "mu": {"story": 2.0, "visual": 0.0, "ost": 0.0, "voice": 0.0, "char": 0.0, "fun": 0.0},
            "sigma_sq": {"story": 1.0, "visual": 1.0, "ost": 1.0, "voice": 1.0, "char": 1.0, "fun": 1.0},
        }
        hierarchical_shrinkage(temp_store, item)
        assert item["mu"]["story"] == pytest.approx(2.0)


# --- Display Conversion ---


class TestDisplayConversion:
    async def test_mu_zero_gives_center(self, temp_store: store.DataStore) -> None:
        assert display_rating(temp_store, 0.0) == pytest.approx(temp_store.settings["display_center"])

    async def test_uncertainty_positive(self, temp_store: store.DataStore) -> None:
        u = display_uncertainty(temp_store, 4.0)
        assert u > 0.0
        assert u == pytest.approx(2.0 * temp_store.settings["display_scale"])


# --- Draw Probability ---


class TestDrawProbability:
    async def test_prior_at_zero_battles(self, temp_store: store.DataStore) -> None:
        result = get_match_probabilities(temp_store, 0.0, 4.0, 0.0, 4.0, battles=0, draws=0)
        assert result["draw"] > 20.0

    async def test_empirical_dominates_at_high_battles(self, temp_store: store.DataStore) -> None:
        result_low = get_match_probabilities(temp_store, 0.0, 4.0, 0.0, 4.0, battles=200, draws=20)
        result_default = get_match_probabilities(temp_store, 0.0, 4.0, 0.0, 4.0, battles=0, draws=0)
        assert result_low["draw"] < result_default["draw"]

    async def test_smooth_transition(self, temp_store: store.DataStore) -> None:
        draws = 7
        results = []
        for b in range(15, 25):
            r = get_match_probabilities(temp_store, 0.0, 4.0, 0.0, 4.0, battles=b, draws=draws)
            results.append(r["draw"])
        for i in range(len(results) - 1):
            assert abs(results[i + 1] - results[i]) < 5.0

    async def test_clamping(self, temp_store: store.DataStore) -> None:
        result = get_match_probabilities(temp_store, 0.0, 4.0, 0.0, 4.0, battles=100, draws=100)
        assert result["draw"] <= 100.0
        result = get_match_probabilities(temp_store, 0.0, 4.0, 0.0, 4.0, battles=100, draws=0)
        assert result["draw"] >= 0.0


# --- Matchmaking ---


class TestMatchmaking:
    async def test_returns_pair_with_two_items(self, temp_store: store.DataStore) -> None:
        await temp_store.add_item("Alpha")
        await temp_store.add_item("Beta")
        item1, item2 = get_match_pair(temp_store)
        assert item1 is not None
        assert item2 is not None
        assert item1["id"] != item2["id"]

    async def test_returns_none_with_one_item(self, temp_store: store.DataStore) -> None:
        await temp_store.add_item("Alpha")
        item1, item2 = get_match_pair(temp_store)
        assert item2 is None

    async def test_focus_mode(self, temp_store: store.DataStore) -> None:
        await temp_store.add_item("Alpha")
        await temp_store.add_item("Beta")
        await temp_store.add_item("Gamma")
        focus = temp_store.items[2]
        item1, item2 = get_match_pair(temp_store, focus_id=focus["id"])
        assert item1["id"] == focus["id"]
        assert item2 is not None


# --- Adaptive Hierarchical Shrinkage ---


class TestAdaptiveHierarchicalShrinkage:
    async def test_more_matches_less_shrinkage(self, temp_store: store.DataStore) -> None:
        """관측 수가 많은 기준은 축소가 적게 적용됨"""
        item = {
            "mu": {"story": 2.0, "visual": 2.0, "ost": 0.0, "voice": 0.0, "char": 0.0, "fun": 0.0},
            "sigma_sq": {"story": 1.0, "visual": 1.0, "ost": 1.0, "voice": 1.0, "char": 1.0, "fun": 1.0},
            "criterion_matches": {"story": 50, "visual": 0, "ost": 0, "voice": 0, "char": 0, "fun": 0},
        }
        hierarchical_shrinkage(temp_store, item)
        # story(50회)는 visual(0회)보다 덜 축소 → cross_mean에서 더 멀리 유지
        assert item["mu"]["story"] > item["mu"]["visual"]


# --- Triple Matchmaking ---


class TestTripleMatchmaking:
    async def test_returns_triple_with_three_items(self, temp_store: store.DataStore) -> None:
        await temp_store.add_item("Alpha")
        await temp_store.add_item("Beta")
        await temp_store.add_item("Gamma")
        item1, item2, item3 = get_match_triple(temp_store)
        assert item1 is not None
        assert item2 is not None
        assert item3 is not None
        ids = {item1["id"], item2["id"], item3["id"]}
        assert len(ids) == 3

    async def test_returns_none_with_two_items(self, temp_store: store.DataStore) -> None:
        await temp_store.add_item("Alpha")
        await temp_store.add_item("Beta")
        item1, item2, item3 = get_match_triple(temp_store)
        assert item1 is None

    async def test_exhaustive_finds_optimal_triple(self, temp_store: store.DataStore) -> None:
        """소규모 풀에서 완전 탐색이 전역 최적 삼중항을 반환"""
        for i in range(10):
            await temp_store.add_item(f"Item{i}")
        # 다양한 불확실성 설정으로 EIG 차이를 유도
        for i, item in enumerate(temp_store.items):
            for c in temp_store.criteria:
                item["sigma_sq"][c["key"]] = 1.0 + i * 0.5
        item1, item2, item3 = get_match_triple(temp_store)
        assert item1 is not None and item2 is not None and item3 is not None

        # C(10,3) = 120 조합 완전탐색으로 최적성 검증
        criteria_keys = [c["key"] for c in temp_store.criteria]
        initial_sq = temp_store.settings["initial_sigma"] ** 2
        result_eig = _triple_eig(item1, item2, item3, criteria_keys, initial_sq)
        for a, b, c in combinations(temp_store.items, 3):
            assert _triple_eig(a, b, c, criteria_keys, initial_sq) <= result_eig + 1e-12

    async def test_triple_focus_mode(self, temp_store: store.DataStore) -> None:
        """focus_id 지정 시 item1이 해당 항목이고 3개 모두 서로 다름"""
        for i in range(5):
            await temp_store.add_item(f"Item{i}")
        focus = temp_store.items[2]
        item1, item2, item3 = get_match_triple(temp_store, focus_id=focus["id"])
        assert item1 is not None and item2 is not None and item3 is not None
        assert item1["id"] == focus["id"]
        assert len({item1["id"], item2["id"], item3["id"]}) == 3


# --- Composite Rating ---


class TestCompositeRating:
    async def test_uniform_weights_equals_mean(self, temp_store: store.DataStore) -> None:
        """모든 weight=1.0일 때 display_rating 평균과 일치"""
        # 모든 기준 weight를 1.0으로 통일
        for c in temp_store.criteria:
            c["weight"] = 1.0
        await temp_store.add_item("Alpha")
        item = temp_store.items[0]
        # 기준별 다른 mu 설정
        keys = [c["key"] for c in temp_store.criteria]
        for i, k in enumerate(keys):
            item["mu"][k] = float(i) * 0.5

        expected = sum(display_rating(temp_store, item["mu"][k]) for k in keys) / len(keys)
        assert composite_rating(temp_store, item) == pytest.approx(expected)

    async def test_custom_weights(self, temp_store: store.DataStore) -> None:
        """비균일 weight에서 가중 평균 정확성 검증"""
        # 기준 2개만 사용, 나머지 weight=0 대신 아주 작은 값
        await temp_store.set_criteria([
            {"key": "a", "label": "A", "color": "blue", "weight": 2.0},
            {"key": "b", "label": "B", "color": "red", "weight": 1.0},
        ])
        await temp_store.add_item("Alpha")
        item = temp_store.items[0]
        item["mu"]["a"] = 1.0
        item["mu"]["b"] = 0.0

        # 가중 평균: (display(1.0)*2 + display(0.0)*1) / 3
        expected = (display_rating(temp_store, 1.0) * 2 + display_rating(temp_store, 0.0) * 1) / 3
        assert composite_rating(temp_store, item) == pytest.approx(expected)

    async def test_zero_mu_gives_center(self, temp_store: store.DataStore) -> None:
        """모든 mu=0.0 → display_center 반환"""
        await temp_store.add_item("Alpha")
        item = temp_store.items[0]
        assert composite_rating(temp_store, item) == pytest.approx(temp_store.settings["display_center"])


# --- Item Rank ---


class TestGetItemRank:
    async def test_single_item_rank_one(self, temp_store: store.DataStore) -> None:
        await temp_store.add_item("Alpha")
        rank, total = get_item_rank(temp_store, temp_store.items[0]["id"])
        assert rank == 1
        assert total == 1

    async def test_rank_ordering(self, temp_store: store.DataStore) -> None:
        """mu가 높은 항목이 더 높은 순위"""
        await temp_store.add_item("Low")
        await temp_store.add_item("High")
        # High에 높은 mu 설정
        for c in temp_store.criteria:
            temp_store.items[1]["mu"][c["key"]] = 2.0
        rank_high, _ = get_item_rank(temp_store, temp_store.items[1]["id"])
        rank_low, _ = get_item_rank(temp_store, temp_store.items[0]["id"])
        assert rank_high < rank_low  # 낮은 rank = 높은 순위

    async def test_missing_item_returns_last(self, temp_store: store.DataStore) -> None:
        """존재하지 않는 ID → (total, total)"""
        await temp_store.add_item("Alpha")
        await temp_store.add_item("Beta")
        rank, total = get_item_rank(temp_store, 9999)
        assert rank == total == 2


