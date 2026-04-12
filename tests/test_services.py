import math
import tempfile
import unittest
from pathlib import Path

import store
from services import (
    bt_update,
    composite_rating,
    display_rating,
    display_uncertainty,
    get_match_pair,
    get_match_probabilities,
    hierarchical_shrinkage,
    sigmoid,
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


class TestSigmoid(unittest.TestCase):
    def test_zero(self) -> None:
        self.assertAlmostEqual(sigmoid(0.0), 0.5)

    def test_large_positive(self) -> None:
        self.assertAlmostEqual(sigmoid(100.0), 1.0, places=5)

    def test_large_negative(self) -> None:
        self.assertAlmostEqual(sigmoid(-100.0), 0.0, places=5)

    def test_clamp_extreme(self) -> None:
        """극단값에서도 오버플로우 없이 동작"""
        self.assertEqual(sigmoid(1000.0), 1.0 / (1.0 + math.exp(-500.0)))
        self.assertGreater(sigmoid(-1000.0), 0.0)


class TestBTUpdate(ServiceTestBase):
    async def test_winner_gains_loser_loses(self) -> None:
        mu_a, sq_a, mu_b, sq_b = bt_update(0.0, 4.0, 0.0, 4.0, 1.0)
        self.assertGreater(mu_a, 0.0)
        self.assertLess(mu_b, 0.0)

    async def test_symmetric_draw_no_mu_change(self) -> None:
        """동일 μ, outcome=0.5 → μ 변화 없음 (symmetric)"""
        mu_a, sq_a, mu_b, sq_b = bt_update(0.0, 4.0, 0.0, 4.0, 0.5)
        self.assertAlmostEqual(mu_a, 0.0, places=10)
        self.assertAlmostEqual(mu_b, 0.0, places=10)

    async def test_variance_always_decreases(self) -> None:
        """모든 outcome에서 σ² 감소"""
        for outcome in [1.0, 0.5, 0.0]:
            _, sq_a, _, sq_b = bt_update(0.5, 4.0, -0.3, 3.0, outcome)
            self.assertLess(sq_a, 4.0)
            self.assertLess(sq_b, 3.0)

    async def test_high_uncertainty_bigger_update(self) -> None:
        """높은 σ² → 큰 μ 변화"""
        mu_high, _, _, _ = bt_update(0.0, 10.0, 0.0, 10.0, 1.0)
        mu_low, _, _, _ = bt_update(0.0, 0.1, 0.0, 0.1, 1.0)
        self.assertGreater(abs(mu_high), abs(mu_low))

    async def test_sigma_floor(self) -> None:
        """σ² ≥ 0.01 보장"""
        # 아주 작은 sigma에서도 floor 유지
        _, sq_a, _, sq_b = bt_update(0.0, 0.01, 0.0, 0.01, 1.0)
        self.assertGreaterEqual(sq_a, 0.01)
        self.assertGreaterEqual(sq_b, 0.01)

    async def test_convergence(self) -> None:
        """반복 승리 → μ_a >> μ_b"""
        mu_a, sq_a, mu_b, sq_b = 0.0, 4.0, 0.0, 4.0
        for _ in range(50):
            mu_a, sq_a, mu_b, sq_b = bt_update(mu_a, sq_a, mu_b, sq_b, 1.0)
        self.assertGreater(mu_a, 0.5)
        self.assertLess(mu_b, -0.5)


class TestHierarchicalShrinkage(ServiceTestBase):
    async def test_pulls_toward_cross_mean(self) -> None:
        s = await self._make_store()
        item = {
            "mu": {"story": 2.0, "visual": 0.0, "ost": 0.0, "voice": 0.0, "char": 0.0, "fun": 0.0},
            "sigma_sq": {"story": 1.0, "visual": 1.0, "ost": 1.0, "voice": 1.0, "char": 1.0, "fun": 1.0},
        }
        old_story_mu = item["mu"]["story"]
        hierarchical_shrinkage(s, item)
        # story가 2.0 → cross_mean 방향(< 2.0)으로 이동
        self.assertLess(item["mu"]["story"], old_story_mu)

    async def test_zero_strength_no_change(self) -> None:
        s = await self._make_store()
        await s.update_settings({"hierarchical_strength": 0.0})
        item = {
            "mu": {"story": 2.0, "visual": 0.0, "ost": 0.0, "voice": 0.0, "char": 0.0, "fun": 0.0},
            "sigma_sq": {"story": 1.0, "visual": 1.0, "ost": 1.0, "voice": 1.0, "char": 1.0, "fun": 1.0},
        }
        hierarchical_shrinkage(s, item)
        self.assertAlmostEqual(item["mu"]["story"], 2.0)


class TestDisplayConversion(ServiceTestBase):
    async def test_mu_zero_gives_center(self) -> None:
        s = await self._make_store()
        self.assertAlmostEqual(display_rating(s, 0.0), s.settings["display_center"])

    async def test_uncertainty_positive(self) -> None:
        s = await self._make_store()
        u = display_uncertainty(s, 4.0)
        self.assertGreater(u, 0.0)
        self.assertAlmostEqual(u, 2.0 * s.settings["display_scale"])


class TestDrawProbability(ServiceTestBase):
    async def test_prior_at_zero_battles(self) -> None:
        s = await self._make_store()
        result = get_match_probabilities(s, 0.0, 4.0, 0.0, 4.0, battles=0, draws=0)
        self.assertGreater(result["draw"], 20.0)

    async def test_empirical_dominates_at_high_battles(self) -> None:
        s = await self._make_store()
        result_low = get_match_probabilities(s, 0.0, 4.0, 0.0, 4.0, battles=200, draws=20)
        result_default = get_match_probabilities(s, 0.0, 4.0, 0.0, 4.0, battles=0, draws=0)
        self.assertLess(result_low["draw"], result_default["draw"])

    async def test_smooth_transition(self) -> None:
        s = await self._make_store()
        draws = 7
        results = []
        for b in range(15, 25):
            r = get_match_probabilities(s, 0.0, 4.0, 0.0, 4.0, battles=b, draws=draws)
            results.append(r["draw"])
        for i in range(len(results) - 1):
            self.assertLess(abs(results[i + 1] - results[i]), 5.0)

    async def test_clamping(self) -> None:
        s = await self._make_store()
        result = get_match_probabilities(s, 0.0, 4.0, 0.0, 4.0, battles=100, draws=100)
        self.assertLessEqual(result["draw"], 100.0)
        result = get_match_probabilities(s, 0.0, 4.0, 0.0, 4.0, battles=100, draws=0)
        self.assertGreaterEqual(result["draw"], 0.0)


class TestMatchmaking(ServiceTestBase):
    async def test_adaptive_sample_size(self) -> None:
        self.assertEqual(min(max(2, int(math.sqrt(4))), 10), 2)
        self.assertEqual(min(max(2, int(math.sqrt(100))), 10), 10)

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
