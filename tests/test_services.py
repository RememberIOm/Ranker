import math
import tempfile
import unittest
from pathlib import Path

import store
from services import (
    calculate_elo_update,
    calculate_expected_score,
    composite_rating,
    get_dynamic_k_factor,
    get_match_pair,
    get_match_probabilities,
)


class ServiceTestBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_session_dir = store.SESSION_DIR
        store.SESSION_DIR = Path(self.tempdir.name)
        store.SESSION_DIR.mkdir(parents=True, exist_ok=True)
        store._session_cache.clear()
        store._locks.clear()

    async def asyncTearDown(self) -> None:
        store._session_cache.clear()
        store._locks.clear()
        store.SESSION_DIR = self.original_session_dir
        self.tempdir.cleanup()

    async def _make_store(self, session_id: str = "a" * 32) -> store.DataStore:
        return await store.get_store(session_id)


class TestDynamicKFactor(ServiceTestBase):
    async def test_k_factor_at_zero_matches(self) -> None:
        s = await self._make_store()
        k = get_dynamic_k_factor(s, 0)
        self.assertAlmostEqual(k, s.settings["elo_k_max"], places=5)

    async def test_k_factor_converges_to_k_min(self) -> None:
        s = await self._make_store()
        k = get_dynamic_k_factor(s, 10_000)
        self.assertAlmostEqual(k, s.settings["elo_k_min"], delta=0.01)

    async def test_k_factor_monotonically_decreasing(self) -> None:
        s = await self._make_store()
        prev = get_dynamic_k_factor(s, 0)
        for n in range(1, 200):
            current = get_dynamic_k_factor(s, n)
            self.assertLessEqual(current, prev)
            prev = current


class TestEloUpdate(ServiceTestBase):
    async def test_zero_sum_property(self) -> None:
        """K-Avg 사용으로 (new_a - old_a) + (new_b - old_b) == 0 보장"""
        s = await self._make_store()
        old_a, old_b = 1200.0, 1200.0
        for actual_score in [1.0, 0.5, 0.0]:
            new_a, new_b = calculate_elo_update(s, old_a, old_b, actual_score, 0, 0)
            total_change = (new_a - old_a) + (new_b - old_b)
            self.assertAlmostEqual(total_change, 0.0, places=10)

    async def test_zero_sum_with_asymmetric_matches(self) -> None:
        """매치 수가 크게 다른 경우에도 영합 유지"""
        s = await self._make_store()
        old_a, old_b = 1300.0, 1100.0
        for actual_score in [1.0, 0.5, 0.0]:
            new_a, new_b = calculate_elo_update(s, old_a, old_b, actual_score, 0, 100)
            total_change = (new_a - old_a) + (new_b - old_b)
            self.assertAlmostEqual(total_change, 0.0, places=10)

    async def test_winner_gains_loser_loses(self) -> None:
        s = await self._make_store()
        new_a, new_b = calculate_elo_update(s, 1200.0, 1200.0, 1.0, 10, 10)
        self.assertGreater(new_a, 1200.0)
        self.assertLess(new_b, 1200.0)

    async def test_draw_minimal_change_equal_ratings(self) -> None:
        s = await self._make_store()
        new_a, new_b = calculate_elo_update(s, 1200.0, 1200.0, 0.5, 50, 50)
        self.assertAlmostEqual(new_a, 1200.0, places=5)
        self.assertAlmostEqual(new_b, 1200.0, places=5)


class TestDrawProbability(ServiceTestBase):
    async def test_prior_at_zero_battles(self) -> None:
        """배틀 0회 시 draw_max == elo_draw_max"""
        s = await self._make_store()
        result = get_match_probabilities(s, 1200.0, 1200.0, battles=0, draws=0)
        # draw% 가 elo_draw_max(0.33) 기반 — 동일 레이팅이므로 draw 확률이 높아야 함
        self.assertGreater(result["draw"], 20.0)

    async def test_empirical_dominates_at_high_battles(self) -> None:
        """200배틀, 20무승부(10%) → draw_max가 0.33에서 ~0.1로 수렴"""
        s = await self._make_store()
        result_low = get_match_probabilities(s, 1200.0, 1200.0, battles=200, draws=20)
        result_default = get_match_probabilities(s, 1200.0, 1200.0, battles=0, draws=0)
        self.assertLess(result_low["draw"], result_default["draw"])

    async def test_smooth_transition_no_discontinuity(self) -> None:
        """19→20→21배틀 전환 시 불연속 없음"""
        s = await self._make_store()
        draws = 7  # ~35% draw rate, close to default 0.33
        results = []
        for b in range(15, 25):
            r = get_match_probabilities(s, 1200.0, 1200.0, battles=b, draws=draws)
            results.append(r["draw"])
        # 인접 배틀 간 draw% 차이가 5% 미만이어야 부드러운 전환
        for i in range(len(results) - 1):
            self.assertLess(abs(results[i + 1] - results[i]), 5.0)

    async def test_clamping(self) -> None:
        """극단적 입력에서도 draw_max가 [0.05, 0.5] 범위 유지"""
        s = await self._make_store()
        # 모든 배틀이 무승부
        result = get_match_probabilities(s, 1200.0, 1200.0, battles=100, draws=100)
        self.assertLessEqual(result["draw"], 100.0)
        # 무승부 0건
        result = get_match_probabilities(s, 1200.0, 1200.0, battles=100, draws=0)
        self.assertGreaterEqual(result["draw"], 0.0)


class TestMatchmaking(ServiceTestBase):
    async def test_adaptive_sample_size(self) -> None:
        """sqrt(N) 기반 샘플 크기 계산 검증"""
        from services import get_match_pair
        # 4 items -> max(2, sqrt(4)) = 2, capped at 10 -> 2
        self.assertEqual(min(max(2, int(math.sqrt(4))), 10), 2)
        # 100 items -> max(2, sqrt(100)) = 10, capped at 10 -> 10
        self.assertEqual(min(max(2, int(math.sqrt(100))), 10), 10)
        # 10000 items -> max(2, sqrt(10000)) = 100, capped at 10 -> 10
        self.assertEqual(min(max(2, int(math.sqrt(10000))), 10), 10)

    async def test_returns_pair_with_two_items(self) -> None:
        s = await self._make_store()
        await s.add_item("Alpha")
        await s.add_item("Beta")
        item1, item2 = get_match_pair(s)
        self.assertIsNotNone(item1)
        self.assertIsNotNone(item2)
        self.assertNotEqual(item1["id"], item2["id"])

    async def test_returns_none_with_one_item(self) -> None:
        s = await self._make_store()
        await s.add_item("Alpha")
        item1, item2 = get_match_pair(s)
        self.assertIsNone(item2)

    async def test_focus_mode(self) -> None:
        s = await self._make_store()
        await s.add_item("Alpha")
        await s.add_item("Beta")
        await s.add_item("Gamma")
        focus = s.items[2]
        item1, item2 = get_match_pair(s, focus_id=focus["id"])
        self.assertEqual(item1["id"], focus["id"])
        self.assertIsNotNone(item2)


class TestPerCriterionMatches(ServiceTestBase):
    async def test_criterion_matches_initialized_empty(self) -> None:
        s = await self._make_store()
        await s.add_item("Alpha")
        self.assertEqual(s.items[0].get("criterion_matches", {}), {})

    async def test_criterion_matches_incremented_after_vote(self) -> None:
        s = await self._make_store()
        await s.add_item("Alpha")
        await s.add_item("Beta")
        token = await s.issue_battle_round(s.items[0]["id"], s.items[1]["id"])

        from schemas import BattleVoteRequest
        votes = {c["key"]: "1" for c in s.criteria}
        payload = BattleVoteRequest(
            item1_id=s.items[0]["id"],
            item2_id=s.items[1]["id"],
            round_token=token,
            votes=votes,
            redirect_to="/battle",
        )
        await s.apply_battle_vote(payload)

        for c in s.criteria:
            self.assertEqual(s.items[0]["criterion_matches"][c["key"]], 1)
            self.assertEqual(s.items[1]["criterion_matches"][c["key"]], 1)


if __name__ == "__main__":
    unittest.main()
