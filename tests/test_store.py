import tempfile
from pathlib import Path

import pytest

import store


@pytest.fixture()
async def temp_session():
    tempdir = tempfile.TemporaryDirectory()
    original_session_dir = store.SESSION_DIR
    store.SESSION_DIR = Path(tempdir.name)
    store.SESSION_DIR.mkdir(parents=True, exist_ok=True)
    store._session_cache.clear()
    store._locks.clear()
    yield
    store._session_cache.clear()
    store._locks.clear()
    store.SESSION_DIR = original_session_dir
    tempdir.cleanup()


class TestStoreValidation:
    async def test_import_json_repairs_missing_mu(self, temp_session) -> None:
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

        assert session.items[0]["mu"]["story"] == pytest.approx(0.0)
        assert session.items[0]["sigma_sq"]["story"] > 0

    async def test_delete_session_clears_runtime_cache_and_lock(self, temp_session) -> None:
        session_id = "b" * 32
        session = await store.get_store(session_id)
        await session.save()
        store._get_lock(session_id)

        session.delete_session()

        assert not (store.SESSION_DIR / f"{session_id}.json").exists()
        assert session_id not in store._session_cache
        assert session_id not in store._locks

    async def test_migration_from_elo_format(self, temp_session) -> None:
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
        assert session.items[0]["mu"]["story"] == pytest.approx((1510 - 1400) / 173.72, abs=0.01)
        # visual은 center와 동일 → mu ≈ 0
        assert session.items[0]["mu"]["visual"] == pytest.approx(0.0, abs=0.01)
        # sigma_sq가 존재하고 양수
        assert session.items[0]["sigma_sq"]["story"] > 0
        assert session.items[0]["sigma_sq"]["visual"] > 0
        # criterion_matches가 높을수록 sigma_sq가 작음
        assert session.items[0]["sigma_sq"]["story"] < session.items[0]["sigma_sq"]["visual"]

        # settings도 마이그레이션됨
        assert "draw_prior_max" in session.settings
        assert "elo_k_max" not in session.settings
        assert session.settings["display_center"] == pytest.approx(1400.0)

    async def test_add_item_initializes_mu_sigma(self, temp_session) -> None:
        """새 항목은 mu=0, sigma_sq=initial_sigma² 로 초기화됨"""
        session = await store.get_store("d" * 32)
        await session.add_item("NewItem")
        item = session.items[0]
        initial_sq = session.settings["initial_sigma"] ** 2
        for c in session.criteria:
            assert item["mu"][c["key"]] == pytest.approx(0.0)
            assert item["sigma_sq"][c["key"]] == pytest.approx(initial_sq)

    async def test_set_criteria_syncs_mu_sigma(self, temp_session) -> None:
        """기준 추가/제거 시 mu/sigma_sq 동기화"""
        session = await store.get_store("e" * 32)
        await session.add_item("Alpha")

        new_criteria = [
            {"key": "new_crit", "label": "새기준", "color": "red", "weight": 1.0},
        ]
        await session.set_criteria(new_criteria)

        item = session.items[0]
        # 새 기준 추가됨
        assert "new_crit" in item["mu"]
        assert "new_crit" in item["sigma_sq"]
        # 이전 기준 제거됨
        assert "story" not in item["mu"]
        assert "story" not in item["sigma_sq"]
