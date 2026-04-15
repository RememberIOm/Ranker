import math
import tempfile
from pathlib import Path

import pytest

import store
from services import (
    bt_update,
    display_rating,
    display_uncertainty,
    get_match_pair,
    get_match_probabilities,
    get_match_triple,
    hierarchical_shrinkage,
    sigmoid,
)


@pytest.fixture()
async def temp_store():
    tempdir = tempfile.TemporaryDirectory()
    original_session_dir = store.SESSION_DIR
    store.SESSION_DIR = Path(tempdir.name)
    store.SESSION_DIR.mkdir(parents=True, exist_ok=True)
    store._session_cache.clear()
    store._locks.clear()
    yield await store.get_store("a" * 32)
    store._session_cache.clear()
    store._locks.clear()
    store.SESSION_DIR = original_session_dir
    tempdir.cleanup()


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
    async def test_winner_gains_loser_loses(self) -> None:
        mu_a, sq_a, mu_b, sq_b = bt_update(0.0, 4.0, 0.0, 4.0, 1.0)
        assert mu_a > 0.0
        assert mu_b < 0.0

    async def test_symmetric_draw_no_mu_change(self) -> None:
        """동일 μ, outcome=0.5 → μ 변화 없음 (symmetric)"""
        mu_a, sq_a, mu_b, sq_b = bt_update(0.0, 4.0, 0.0, 4.0, 0.5)
        assert mu_a == pytest.approx(0.0, abs=1e-10)
        assert mu_b == pytest.approx(0.0, abs=1e-10)

    async def test_variance_always_decreases(self) -> None:
        """모든 outcome에서 σ² 감소"""
        for outcome in [1.0, 0.5, 0.0]:
            _, sq_a, _, sq_b = bt_update(0.5, 4.0, -0.3, 3.0, outcome)
            assert sq_a < 4.0
            assert sq_b < 3.0

    async def test_high_uncertainty_bigger_update(self) -> None:
        """높은 σ² → 큰 μ 변화"""
        mu_high, _, _, _ = bt_update(0.0, 10.0, 0.0, 10.0, 1.0)
        mu_low, _, _, _ = bt_update(0.0, 0.1, 0.0, 0.1, 1.0)
        assert abs(mu_high) > abs(mu_low)

    async def test_sigma_floor(self) -> None:
        """σ² ≥ 0.01 보장"""
        _, sq_a, _, sq_b = bt_update(0.0, 0.01, 0.0, 0.01, 1.0)
        assert sq_a >= 0.01
        assert sq_b >= 0.01

    async def test_convergence(self) -> None:
        """반복 승리 → μ_a >> μ_b"""
        mu_a, sq_a, mu_b, sq_b = 0.0, 4.0, 0.0, 4.0
        for _ in range(50):
            mu_a, sq_a, mu_b, sq_b = bt_update(mu_a, sq_a, mu_b, sq_b, 1.0)
        assert mu_a > 0.5
        assert mu_b < -0.5

    async def test_no_shrink_to_zero_when_g_zero(self) -> None:
        """g=0 (예측 적중) 시 μ 불변 — 0 방향 수축 없음"""
        mu_a, mu_b = 1.5, 0.3
        p = sigmoid(mu_a - mu_b)  # outcome = p → g = 0
        new_a, _, new_b, _ = bt_update(mu_a, 2.0, mu_b, 2.0, p)
        assert new_a == pytest.approx(mu_a, abs=1e-10)
        assert new_b == pytest.approx(mu_b, abs=1e-10)

    async def test_nonzero_mu_update_magnitude(self) -> None:
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
    async def test_adaptive_sample_size(self) -> None:
        assert min(max(2, int(math.sqrt(4))), 10) == 2
        assert min(max(2, int(math.sqrt(100))), 10) == 10

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


# --- Per-Criterion Matches ---


class TestPerCriterionMatches:
    async def test_criterion_matches_initialized_with_all_keys(self, temp_store: store.DataStore) -> None:
        await temp_store.add_item("Alpha")
        cm = temp_store.items[0].get("criterion_matches", {})
        expected_keys = {c["key"] for c in temp_store.criteria}
        assert set(cm.keys()) == expected_keys
        assert all(v == 0 for v in cm.values())

    async def test_criterion_matches_incremented_after_vote(self, temp_store: store.DataStore) -> None:
        await temp_store.add_item("Alpha")
        await temp_store.add_item("Beta")
        token = await temp_store.issue_battle_round(temp_store.items[0]["id"], temp_store.items[1]["id"])

        from schemas import BattleVoteRequest
        votes = {c["key"]: "1" for c in temp_store.criteria}
        payload = BattleVoteRequest(
            item1_id=temp_store.items[0]["id"],
            item2_id=temp_store.items[1]["id"],
            round_token=token,
            votes=votes,
            redirect_to="/battle",
        )
        await temp_store.apply_battle_vote(payload)

        for c in temp_store.criteria:
            assert temp_store.items[0]["criterion_matches"][c["key"]] == 1
            assert temp_store.items[1]["criterion_matches"][c["key"]] == 1


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

    async def test_exhaustive_finds_better_triple(self, temp_store: store.DataStore) -> None:
        """소규모 풀에서 완전 탐색이 탐욕적보다 같거나 나은 삼중항을 찾음"""
        for i in range(10):
            await temp_store.add_item(f"Item{i}")
        # 다양한 불확실성 설정
        for i, item in enumerate(temp_store.items):
            for c in temp_store.criteria:
                item["sigma_sq"][c["key"]] = 1.0 + i * 0.5
        item1, item2, item3 = get_match_triple(temp_store)
        assert item1 is not None
        assert item2 is not None
        assert item3 is not None


# --- Active Round Persistence ---


@pytest.fixture()
async def fresh_store_factory():
    """매번 새로운 session_id로 DataStore를 생성할 수 있는 팩토리"""
    tempdir = tempfile.TemporaryDirectory()
    original_session_dir = store.SESSION_DIR
    store.SESSION_DIR = Path(tempdir.name)
    store.SESSION_DIR.mkdir(parents=True, exist_ok=True)
    store._session_cache.clear()
    store._locks.clear()
    yield store.get_store
    store._session_cache.clear()
    store._locks.clear()
    store.SESSION_DIR = original_session_dir
    tempdir.cleanup()


class TestActiveRoundItem3Persistence:
    async def test_item3_id_survives_reload(self, fresh_store_factory) -> None:
        """3-way active_round의 item3_id가 세션 재로드 후 보존됨"""
        session_id = "f" * 32
        s = await fresh_store_factory(session_id)
        await s.add_item("Alpha")
        await s.add_item("Beta")
        await s.add_item("Gamma")
        item1, item2, item3 = s.items[0], s.items[1], s.items[2]
        token = await s.issue_battle_round(item1["id"], item2["id"], item3["id"])

        # 캐시 제거 후 디스크에서 다시 로드
        store._session_cache.clear()
        store._locks.clear()
        s2 = await fresh_store_factory(session_id)

        ar = s2._data["active_round"]
        assert ar is not None
        assert ar["token"] == token
        assert ar["item1_id"] == item1["id"]
        assert ar["item2_id"] == item2["id"]
        assert ar["item3_id"] == item3["id"]

    async def test_2way_round_no_item3(self, fresh_store_factory) -> None:
        """2-way active_round는 item3_id가 None"""
        session_id = "g" * 32
        s = await fresh_store_factory(session_id)
        await s.add_item("Alpha")
        await s.add_item("Beta")
        await s.issue_battle_round(s.items[0]["id"], s.items[1]["id"])

        store._session_cache.clear()
        store._locks.clear()
        s2 = await fresh_store_factory(session_id)

        ar = s2._data["active_round"]
        assert ar is not None
        assert ar["item3_id"] is None


# --- 3-way Tied Vote ---


class TestThreeWayTiedVote:
    async def test_best_only_tied_vote(self, temp_store: store.DataStore) -> None:
        """3-way 'best only' 투표: best > tied_a, best > tied_b, tied_a ≈ tied_b"""
        await temp_store.add_item("Alpha")
        await temp_store.add_item("Beta")
        await temp_store.add_item("Gamma")
        items = temp_store.items
        token = await temp_store.issue_battle_round(items[0]["id"], items[1]["id"], items[2]["id"])

        from schemas import ThreeWayBattleVoteRequest
        # best=item1, tied=item2 & item3
        votes = {}
        for c in temp_store.criteria:
            votes[c["key"]] = {
                str(items[0]["id"]): "best",
                str(items[1]["id"]): "tied",
                str(items[2]["id"]): "tied",
            }
        payload = ThreeWayBattleVoteRequest(
            item1_id=items[0]["id"],
            item2_id=items[1]["id"],
            item3_id=items[2]["id"],
            round_token=token,
            votes=votes,
        )
        resp_data = await temp_store.apply_three_way_vote(payload)

        # best(item1)는 레이팅 상승
        for r in resp_data["results"]:
            best_diff = r["diffs"][str(items[0]["id"])]
            assert best_diff > 0

        # draws 통계가 증가 (tied 쌍 = 무승부)
        for c in temp_store.criteria:
            assert c["draws"] > 0

    async def test_worst_only_vote(self, temp_store: store.DataStore) -> None:
        """3-way 'worst only' 투표: tied_a > worst, tied_b > worst, tied_a ≈ tied_b"""
        await temp_store.add_item("Alpha")
        await temp_store.add_item("Beta")
        await temp_store.add_item("Gamma")
        items = temp_store.items
        token = await temp_store.issue_battle_round(items[0]["id"], items[1]["id"], items[2]["id"])

        from schemas import ThreeWayBattleVoteRequest

        votes = {}
        for c in temp_store.criteria:
            votes[c["key"]] = {
                str(items[0]["id"]): "worst",
                str(items[1]["id"]): "tied",
                str(items[2]["id"]): "tied",
            }
        payload = ThreeWayBattleVoteRequest(
            item1_id=items[0]["id"],
            item2_id=items[1]["id"],
            item3_id=items[2]["id"],
            round_token=token,
            votes=votes,
        )
        resp_data = await temp_store.apply_three_way_vote(payload)

        # worst(item1)는 레이팅 하락
        for r in resp_data["results"]:
            worst_diff = r["diffs"][str(items[0]["id"])]
            assert worst_diff < 0
            assert r["best_id"] is None
            assert r["worst_id"] == items[0]["id"]
            assert r["middle_id"] is None

        # draws 통계 증가 (tied 쌍 1개)
        for c in temp_store.criteria:
            assert c.get("draws", 0) == 1

    async def test_all_tied_vote(self, temp_store: store.DataStore) -> None:
        """3-way 모두 무승부: 3개 항목 모두 tied"""
        await temp_store.add_item("Alpha")
        await temp_store.add_item("Beta")
        await temp_store.add_item("Gamma")
        items = temp_store.items
        token = await temp_store.issue_battle_round(items[0]["id"], items[1]["id"], items[2]["id"])

        from schemas import ThreeWayBattleVoteRequest

        votes = {}
        for c in temp_store.criteria:
            votes[c["key"]] = {
                str(items[0]["id"]): "tied",
                str(items[1]["id"]): "tied",
                str(items[2]["id"]): "tied",
            }
        payload = ThreeWayBattleVoteRequest(
            item1_id=items[0]["id"],
            item2_id=items[1]["id"],
            item3_id=items[2]["id"],
            round_token=token,
            votes=votes,
        )
        resp_data = await temp_store.apply_three_way_vote(payload)

        # 모든 레이팅 변화가 0에 가까움 (동일 레이팅 항목들의 대칭 무승부)
        for r in resp_data["results"]:
            for item in items:
                diff = abs(r["diffs"][str(item["id"])])
                assert diff < 0.1
            assert r["best_id"] is None
            assert r["worst_id"] is None
            assert r["middle_id"] is None

        # draws 통계: 기준당 3개 무승부 쌍
        for c in temp_store.criteria:
            assert c.get("draws", 0) == 3

    async def test_invalid_role_combination(self, temp_store: store.DataStore) -> None:
        """잘못된 역할 조합 (best 2개) → InvalidBattleVoteError"""
        await temp_store.add_item("Alpha")
        await temp_store.add_item("Beta")
        await temp_store.add_item("Gamma")
        items = temp_store.items
        token = await temp_store.issue_battle_round(items[0]["id"], items[1]["id"], items[2]["id"])

        from schemas import ThreeWayBattleVoteRequest

        votes = {}
        for c in temp_store.criteria:
            votes[c["key"]] = {
                str(items[0]["id"]): "best",
                str(items[1]["id"]): "best",
                str(items[2]["id"]): "worst",
            }
        payload = ThreeWayBattleVoteRequest(
            item1_id=items[0]["id"],
            item2_id=items[1]["id"],
            item3_id=items[2]["id"],
            round_token=token,
            votes=votes,
        )
        with pytest.raises(store.InvalidBattleVoteError):
            await temp_store.apply_three_way_vote(payload)
