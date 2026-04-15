import json
import os
import time

import pytest

from schemas import BattleVoteRequest
import store


class TestStoreValidation:
    async def test_import_json_repairs_missing_mu(self, temp_store: store.DataStore) -> None:
        """import_json은 _load와 동일한 관대 파싱을 사용하여 누락된 mu/sigma_sq를 자동 보정합니다."""
        await temp_store.import_json(
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

        assert temp_store.items[0]["mu"]["story"] == pytest.approx(0.0)
        assert temp_store.items[0]["sigma_sq"]["story"] > 0

    async def test_delete_session_clears_runtime_cache_and_lock(self, store_factory) -> None:
        session_id = "b" * 32
        session = await store_factory(session_id)
        await session.save()
        store._get_lock(session_id)

        session.delete_session()

        assert not (store.SESSION_DIR / f"{session_id}.json").exists()
        assert session_id not in store._session_cache
        assert session_id not in store._locks

    async def test_migration_from_elo_format(self, store_factory) -> None:
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

        session = await store_factory(session_id)

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

    async def test_add_item_initializes_mu_sigma(self, temp_store: store.DataStore) -> None:
        """새 항목은 mu=0, sigma_sq=initial_sigma² 로 초기화됨"""
        await temp_store.add_item("NewItem")
        item = temp_store.items[0]
        initial_sq = temp_store.settings["initial_sigma"] ** 2
        for c in temp_store.criteria:
            assert item["mu"][c["key"]] == pytest.approx(0.0)
            assert item["sigma_sq"][c["key"]] == pytest.approx(initial_sq)

    async def test_set_criteria_syncs_mu_sigma(self, temp_store: store.DataStore) -> None:
        """기준 추가/제거 시 mu/sigma_sq 동기화"""
        await temp_store.add_item("Alpha")

        new_criteria = [
            {"key": "new_crit", "label": "새기준", "color": "red", "weight": 1.0},
        ]
        await temp_store.set_criteria(new_criteria)

        item = temp_store.items[0]
        # 새 기준 추가됨
        assert "new_crit" in item["mu"]
        assert "new_crit" in item["sigma_sq"]
        # 이전 기준 제거됨
        assert "story" not in item["mu"]
        assert "story" not in item["sigma_sq"]


# --- Per-Criterion Matches ---


class TestPerCriterionMatches:
    async def test_criterion_matches_initialized_with_all_keys(self, temp_store: store.DataStore) -> None:
        await temp_store.add_item("Alpha")
        cm = temp_store.items[0].get("criterion_matches", {})
        expected_keys = {c["key"] for c in temp_store.criteria}
        assert set(cm.keys()) == expected_keys
        assert all(v == 0 for v in cm.values())

    async def test_criterion_matches_incremented_after_vote(self, store_with_items: store.DataStore) -> None:
        s = store_with_items
        token = await s.issue_battle_round(s.items[0]["id"], s.items[1]["id"])
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
            assert s.items[0]["criterion_matches"][c["key"]] == 1
            assert s.items[1]["criterion_matches"][c["key"]] == 1


# --- Active Round Persistence ---


class TestActiveRoundItem3Persistence:
    async def test_item3_id_survives_reload(self, store_factory) -> None:
        """3-way active_round의 item3_id가 세션 재로드 후 보존됨"""
        session_id = "f" * 32
        s = await store_factory(session_id)
        await s.add_item("Alpha")
        await s.add_item("Beta")
        await s.add_item("Gamma")
        item1, item2, item3 = s.items[0], s.items[1], s.items[2]
        token = await s.issue_battle_round(item1["id"], item2["id"], item3["id"])

        # 캐시 제거 후 디스크에서 다시 로드
        store._session_cache.clear()
        store._locks.clear()
        s2 = await store_factory(session_id)

        ar = s2._data["active_round"]
        assert ar is not None
        assert ar["token"] == token
        assert ar["item1_id"] == item1["id"]
        assert ar["item2_id"] == item2["id"]
        assert ar["item3_id"] == item3["id"]

    async def test_2way_round_no_item3(self, store_factory) -> None:
        """2-way active_round는 item3_id가 None"""
        session_id = "g" * 32
        s = await store_factory(session_id)
        await s.add_item("Alpha")
        await s.add_item("Beta")
        await s.issue_battle_round(s.items[0]["id"], s.items[1]["id"])

        store._session_cache.clear()
        store._locks.clear()
        s2 = await store_factory(session_id)

        ar = s2._data["active_round"]
        assert ar is not None
        assert ar["item3_id"] is None


# --- Export / Import ---


class TestExportImportRoundtrip:
    async def test_roundtrip_preserves_data(self, store_with_items: store.DataStore) -> None:
        """항목 추가 + 투표 후 export → import → 데이터 일치"""
        s = store_with_items
        token = await s.issue_battle_round(s.items[0]["id"], s.items[1]["id"])
        votes = {c["key"]: "1" for c in s.criteria}
        payload = BattleVoteRequest(
            item1_id=s.items[0]["id"],
            item2_id=s.items[1]["id"],
            round_token=token,
            votes=votes,
            redirect_to="/battle",
        )
        await s.apply_battle_vote(payload)

        exported = s.export_json()
        original_items = [(i["name"], dict(i["mu"])) for i in s.items]

        # 새 세션에 import
        await s.import_json(exported)

        for i, (name, mu) in enumerate(original_items):
            assert s.items[i]["name"] == name
            for k, v in mu.items():
                assert s.items[i]["mu"][k] == pytest.approx(v)

    async def test_export_returns_valid_json(self, temp_store: store.DataStore) -> None:
        """export_json()이 유효한 JSON을 반환"""
        await temp_store.add_item("Alpha")
        exported = temp_store.export_json()
        parsed = json.loads(exported)
        assert "items" in parsed
        assert "criteria" in parsed
        assert "settings" in parsed


# --- Bulk Add ---


class TestAddItemsBulk:
    async def test_adds_multiple_items(self, temp_store: store.DataStore) -> None:
        count = await temp_store.add_items_bulk(["A", "B", "C"])
        assert count == 3
        assert len(temp_store.items) == 3
        names = [i["name"] for i in temp_store.items]
        assert names == ["A", "B", "C"]
        # 순차 ID 확인
        ids = [i["id"] for i in temp_store.items]
        assert ids == [1, 2, 3]

    async def test_skips_blank_names(self, temp_store: store.DataStore) -> None:
        """빈 이름은 건너뛰고 실제 추가된 개수만 반환"""
        count = await temp_store.add_items_bulk(["A", "", "  ", "B"])
        assert count == 2
        assert len(temp_store.items) == 2

    async def test_empty_list_returns_zero(self, temp_store: store.DataStore) -> None:
        count = await temp_store.add_items_bulk([])
        assert count == 0
        assert len(temp_store.items) == 0


# --- Update Item ---


class TestUpdateItem:
    async def test_update_name(self, temp_store: store.DataStore) -> None:
        await temp_store.add_item("Original")
        result = await temp_store.update_item(temp_store.items[0]["id"], name="Updated")
        assert result is True
        assert temp_store.items[0]["name"] == "Updated"

    async def test_nonexistent_returns_false(self, temp_store: store.DataStore) -> None:
        result = await temp_store.update_item(9999, name="X")
        assert result is False


# --- Delete Item ---


class TestDeleteItem:
    async def test_delete_existing(self, store_with_items: store.DataStore) -> None:
        s = store_with_items
        item_id = s.items[0]["id"]
        result = await s.delete_item(item_id)
        assert result is True
        assert len(s.items) == 1
        assert s.items[0]["name"] == "Beta"

    async def test_delete_nonexistent(self, temp_store: store.DataStore) -> None:
        result = await temp_store.delete_item(9999)
        assert result is False


# --- Cleanup Expired Sessions ---


class TestCleanupExpiredSessions:
    async def test_removes_expired_file(self, store_factory) -> None:
        """만료된 세션 파일 삭제"""
        session_id = "h" * 32
        s = await store_factory(session_id)
        await s.save()
        session_path = store.SESSION_DIR / f"{session_id}.json"
        assert session_path.exists()

        # mtime을 TTL 이전으로 설정하고 캐시에서 제거
        old_time = time.time() - store.SESSION_TTL_SECONDS - 100
        os.utime(session_path, (old_time, old_time))
        store._session_cache.pop(session_id, None)

        removed = await store.cleanup_expired_sessions()
        assert removed == 1
        assert not session_path.exists()

    async def test_preserves_recent_file(self, store_factory) -> None:
        """최근 파일은 삭제하지 않음"""
        session_id = "i" * 32
        s = await store_factory(session_id)
        await s.save()
        session_path = store.SESSION_DIR / f"{session_id}.json"

        removed = await store.cleanup_expired_sessions()
        assert removed == 0
        assert session_path.exists()
