import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

import store


class StoreValidationTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_import_json_repairs_missing_mu(self) -> None:
        """import_json은 _load와 동일한 관대 파싱을 사용하여 누락된 mu/sigma_sq를 자동 보정합니다."""
        session = await store.get_store("a" * 32)

        await session.import_json(
            """
            {
              "criteria": [
                {"key": "story", "label": "스토리", "color": "blue", "weight": 1.0}
              ],
              "items": [
                {"id": 1, "name": "Alpha", "mu": {}, "sigma_sq": {}, "matches_played": 0}
              ]
            }
            """
        )

        self.assertAlmostEqual(session.items[0]["mu"]["story"], 0.0)
        self.assertGreater(session.items[0]["sigma_sq"]["story"], 0)

    async def test_delete_session_clears_runtime_cache_and_lock(self) -> None:
        session_id = "b" * 32
        session = await store.get_store(session_id)
        await session.save()
        store._get_lock(session_id)

        session.delete_session()

        self.assertFalse((store.SESSION_DIR / f"{session_id}.json").exists())
        self.assertNotIn(session_id, store._session_cache)
        self.assertNotIn(session_id, store._locks)

    async def test_migration_from_elo_format(self) -> None:
        """구 Elo 형식 JSON 로드 시 mu/sigma_sq로 자동 마이그레이션"""
        session_id = "c" * 32
        legacy_payload = """
        {
          "settings": {
            "initial_rating": 1400,
            "elo_draw_max": 0.33,
            "elo_draw_scale": 300.0,
            "elo_k_max": 100,
            "elo_k_min": 30,
            "elo_decay_factor": 50
          },
          "criteria": [
            {"key": "story", "label": "스토리", "color": "blue"},
            {"key": "visual", "label": "작화", "color": "purple"}
          ],
          "items": [
            {"id": 1, "name": "Alpha", "ratings": {"story": 1510, "visual": 1400}, "matches_played": 3, "criterion_matches": {"story": 3, "visual": 2}}
          ]
        }
        """
        (store.SESSION_DIR / f"{session_id}.json").write_text(legacy_payload, encoding="utf-8")

        session = await store.get_store(session_id)

        # mu로 변환됨: (1510 - 1400) / 173.72 ≈ 0.633
        self.assertAlmostEqual(session.items[0]["mu"]["story"], (1510 - 1400) / 173.72, places=2)
        # visual은 center와 동일 → mu ≈ 0
        self.assertAlmostEqual(session.items[0]["mu"]["visual"], 0.0, places=2)
        # sigma_sq가 존재하고 양수
        self.assertGreater(session.items[0]["sigma_sq"]["story"], 0)
        self.assertGreater(session.items[0]["sigma_sq"]["visual"], 0)
        # criterion_matches가 높을수록 sigma_sq가 작음
        self.assertLess(session.items[0]["sigma_sq"]["story"], session.items[0]["sigma_sq"]["visual"])

        # settings도 마이그레이션됨
        self.assertIn("draw_prior_max", session.settings)
        self.assertNotIn("elo_k_max", session.settings)
        self.assertAlmostEqual(session.settings["display_center"], 1400.0)

    async def test_add_item_initializes_mu_sigma(self) -> None:
        """새 항목은 mu=0, sigma_sq=initial_sigma² 로 초기화됨"""
        session = await store.get_store("d" * 32)
        await session.add_item("NewItem")
        item = session.items[0]
        initial_sq = session.settings["initial_sigma"] ** 2
        for c in session.criteria:
            self.assertAlmostEqual(item["mu"][c["key"]], 0.0)
            self.assertAlmostEqual(item["sigma_sq"][c["key"]], initial_sq)

    async def test_set_criteria_syncs_mu_sigma(self) -> None:
        """기준 추가/제거 시 mu/sigma_sq 동기화"""
        session = await store.get_store("e" * 32)
        await session.add_item("Alpha")

        new_criteria = [
            {"key": "new_crit", "label": "새기준", "color": "red", "weight": 1.0},
        ]
        await session.set_criteria(new_criteria)

        item = session.items[0]
        # 새 기준 추가됨
        self.assertIn("new_crit", item["mu"])
        self.assertIn("new_crit", item["sigma_sq"])
        # 이전 기준 제거됨
        self.assertNotIn("story", item["mu"])
        self.assertNotIn("story", item["sigma_sq"])
