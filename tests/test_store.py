from typing import Any
import json
import time

import pytest

from ranker.schemas import BattleVoteRequest
from ranker import database
from ranker import store


class TestStoreValidation:
    async def test_delete_session_clears_runtime_state(self, store_factory) -> None:
        session_id = "b" * 32
        session = await store_factory(session_id)
        await session.save()
        store._get_lock(session_id)

        await session.delete_session()

        assert not await store.session_exists(session_id)
        assert session_id not in store._locks

    async def test_add_item_initializes_mu_sigma(
        self, temp_store: store.DataStore
    ) -> None:
        """새 항목은 mu=0, sigma_sq=initial_sigma² 로 초기화됨"""
        await temp_store.add_item("NewItem")
        item = temp_store.items[0]
        initial_sq = temp_store.settings["initial_sigma"] ** 2
        for c in temp_store.criteria:
            assert item["mu"][c["key"]] == pytest.approx(0.0)
            assert item["sigma_sq"][c["key"]] == pytest.approx(initial_sq)

    async def test_set_criteria_syncs_mu_sigma(
        self, temp_store: store.DataStore
    ) -> None:
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
    async def test_criterion_matches_initialized_with_all_keys(
        self, temp_store: store.DataStore
    ) -> None:
        await temp_store.add_item("Alpha")
        cm = temp_store.items[0].get("criterion_matches", {})
        expected_keys = {c["key"] for c in temp_store.criteria}
        assert set(cm.keys()) == expected_keys
        assert all(v == 0 for v in cm.values())

    async def test_criterion_matches_incremented_after_vote(
        self, store_with_items: store.DataStore
    ) -> None:
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
        """3-way active_round의 item3_id가 DB 재로드 후 보존됨"""
        session_id = "f" * 32
        s = await store_factory(session_id)
        await s.add_item("Alpha")
        await s.add_item("Beta")
        await s.add_item("Gamma")
        item1, item2, item3 = s.items[0], s.items[1], s.items[2]
        token = await s.issue_battle_round(item1["id"], item2["id"], item3["id"])

        # DB에서 다시 로드
        store._locks.clear()
        s2 = await store_factory(session_id)

        ar = s2._data["active_round"]
        assert ar is not None
        assert ar["token"] == token
        assert ar["item1_id"] == item1["id"]
        assert ar["item2_id"] == item2["id"]
        assert ar["item3_id"] == item3["id"]

    async def test_2way_round_no_item3(self, store_factory) -> None:
        """2-way active_round는 item3_id가 없음"""
        session_id = "g" * 32
        s = await store_factory(session_id)
        await s.add_item("Alpha")
        await s.add_item("Beta")
        await s.issue_battle_round(s.items[0]["id"], s.items[1]["id"])

        store._locks.clear()
        s2 = await store_factory(session_id)

        ar = s2._data["active_round"]
        assert ar is not None
        assert ar.get("item3_id") is None


# --- Export / Import ---


class TestExportImportRoundtrip:
    async def test_roundtrip_preserves_data(
        self, store_with_items: store.DataStore
    ) -> None:
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
    async def test_removes_expired_session(self, store_factory) -> None:
        """만료된 세션 삭제"""
        session_id = "h" * 32
        s = await store_factory(session_id)
        await s.save()
        assert await store.session_exists(session_id)

        # last_accessed를 TTL 이전으로 설정
        old_time = time.time() - store.SESSION_TTL_SECONDS - 100
        db = database.get_db()
        await db.execute(
            "UPDATE sessions SET last_accessed = ? WHERE id = ?",
            (old_time, session_id),
        )
        await db.commit()

        removed = await store.cleanup_expired_sessions()
        assert removed == 1
        assert not await store.session_exists(session_id)

    async def test_preserves_recent_session(self, store_factory) -> None:
        """최근 세션은 삭제하지 않음"""
        session_id = "i" * 32
        s = await store_factory(session_id)
        await s.save()

        removed = await store.cleanup_expired_sessions()
        assert removed == 0
        assert await store.session_exists(session_id)


# --- CASCADE Delete ---


class TestCascadeDelete:
    async def test_delete_session_removes_all_related_rows(self, store_factory) -> None:
        """세션 삭제 시 관련 행 모두 CASCADE 삭제"""
        session_id = "d" * 32
        s = await store_factory(session_id)
        await s.add_item("Alpha")
        await s.add_item("Beta")
        await s.save()

        assert await store.session_exists(session_id)

        await store.delete_session(session_id)

        assert not await store.session_exists(session_id)

        # items, criteria, item_ratings 행도 삭제 확인
        db = database.get_db()
        async with db.execute(
            "SELECT COUNT(*) FROM items WHERE session_id = ?", (session_id,)
        ) as c:
            assert (await c.fetchone())[0] == 0
        async with db.execute(
            "SELECT COUNT(*) FROM criteria WHERE session_id = ?", (session_id,)
        ) as c:
            assert (await c.fetchone())[0] == 0
        async with db.execute(
            "SELECT COUNT(*) FROM item_ratings WHERE session_id = ?", (session_id,)
        ) as c:
            assert (await c.fetchone())[0] == 0


# --- Session Isolation ---


class TestSessionIsolation:
    async def test_concurrent_sessions_isolated(self, store_factory) -> None:
        """두 세션이 서로 간섭하지 않음"""
        s1 = await store_factory("1" * 32)
        s2 = await store_factory("2" * 32)
        await s1.add_item("S1-Item")
        await s2.add_item("S2-Item")

        # 재로드 후 확인
        s1r = await store_factory("1" * 32)
        s2r = await store_factory("2" * 32)

        assert len(s1r.items) == 1
        assert s1r.items[0]["name"] == "S1-Item"
        assert len(s2r.items) == 1
        assert s2r.items[0]["name"] == "S2-Item"


class TestStorageRecovery:
    async def test_two_snapshots_preserve_both_additions(
        self, store_with_items: store.DataStore
    ) -> None:
        """서로 다른 요청에서 로드한 스냅샷도 순서대로 병합됩니다."""
        import asyncio

        sid = store_with_items._session_id
        a, b = await asyncio.gather(
            store.DataStore.create(sid), store.DataStore.create(sid)
        )
        await asyncio.gather(a.add_item("First"), b.add_item("Second"))
        loaded = await store.DataStore.create(sid)
        assert {item["name"] for item in loaded.items} == {
            "Alpha",
            "Beta",
            "First",
            "Second",
        }
        assert len({item["id"] for item in loaded.items}) == 4

    async def test_deleted_snapshot_cannot_resurrect_session(
        self, store_with_items: store.DataStore
    ) -> None:
        sid = store_with_items._session_id
        stale = await store.DataStore.create(sid)
        await store.delete_session(sid)
        with pytest.raises(database.StaleSessionError):
            await stale.update_settings({"result_auto_skip": True})
        assert not await store.session_exists(sid)

    async def test_save_rejects_stale_external_mutation(
        self, store_with_items: store.DataStore
    ) -> None:
        stale = await store.DataStore.create(store_with_items._session_id)
        await store_with_items.add_item("Preserved")
        with pytest.raises(database.StaleSessionError):
            await stale.save()
        assert len((await store.DataStore.create(stale._session_id)).items) == 3

    async def test_cancelled_write_rolls_back_before_next_request(
        self, store_with_items: store.DataStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import asyncio

        db = database.get_db()
        original = db.executemany

        async def cancel(*args: Any, **kwargs: Any) -> None:
            raise asyncio.CancelledError()

        monkeypatch.setattr(db, "executemany", cancel)
        with pytest.raises(asyncio.CancelledError):
            await store_with_items.add_item("Must not survive")
        monkeypatch.setattr(db, "executemany", original)
        assert not db.in_transaction
        loaded = await store.get_store(store_with_items._session_id)
        assert [item["name"] for item in loaded.items] == ["Alpha", "Beta"]

    async def test_reads_wait_for_complete_transaction(
        self, store_with_items: store.DataStore
    ) -> None:
        import asyncio

        db = database.get_db()
        task = None
        async with database.transaction():
            await db.execute(
                "UPDATE items SET name = 'Complete' WHERE session_id = ?",
                (store_with_items._session_id,),
            )
            task = asyncio.create_task(
                store.DataStore.create(store_with_items._session_id)
            )
            await asyncio.sleep(0)
            assert not task.done()
        loaded = await task
        assert {item["name"] for item in loaded.items} == {"Complete"}

    async def test_round_issue_does_not_rewrite_ratings(
        self, store_with_items: store.DataStore
    ) -> None:
        db = database.get_db()
        await db.executescript(
            "CREATE TEMP TABLE rating_writes(n); CREATE TEMP TRIGGER trace_rating_update AFTER UPDATE ON item_ratings BEGIN INSERT INTO rating_writes VALUES (1); END;"
        )
        await store_with_items.issue_battle_round(1, 2)
        async with db.execute("SELECT COUNT(*) FROM rating_writes") as cursor:
            assert (await cursor.fetchone())[0] == 0

    async def test_registered_board_survives_ttl(
        self, store_with_items: store.DataStore
    ) -> None:
        async with database.transaction() as db:
            await db.execute("INSERT INTO libraries VALUES ('library', 0)")
            await db.execute(
                "INSERT INTO boards VALUES (?, 'library', 'Saved')",
                (store_with_items._session_id,),
            )
            await db.execute("UPDATE sessions SET last_accessed = 0")
        assert await store.cleanup_expired_sessions() == 0

    async def test_empty_import_rejected_without_reset(
        self, store_with_items: store.DataStore
    ) -> None:
        with pytest.raises(store.InvalidSessionDataError):
            await store_with_items.import_json("{}")
        assert len(store_with_items.items) == 2

    async def test_import_confirmation_rejects_changed_snapshot(
        self, store_with_items: store.DataStore
    ) -> None:
        import hashlib

        raw = store_with_items.export_json()
        digest = hashlib.sha256(raw.encode()).hexdigest()
        await store_with_items.add_item("New")
        with pytest.raises(store.InvalidSessionDataError):
            await store_with_items.import_json(raw, expected_export_digest=digest)
        assert len(store_with_items.items) == 3

    async def test_database_error_is_reported_as_save_error(
        self, store_with_items: store.DataStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import sqlite3

        async def fail(*args: Any, **kwargs: Any) -> None:
            raise sqlite3.OperationalError("disk full")

        monkeypatch.setattr(database.get_db(), "executemany", fail)
        with pytest.raises(store.SessionSaveError):
            await store_with_items.add_item("Failure")
        assert not database.get_db().in_transaction

    async def test_capacity_rejects_write_and_preserves_database(
        self, store_with_items: store.DataStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(store, "MAX_BACKUP_BYTES", 1)
        with pytest.raises(store.SessionSaveError, match="백업 한도"):
            await store_with_items.add_item("Over capacity")
        loaded = await store.DataStore.create(store_with_items._session_id)
        assert [item["name"] for item in loaded.items] == ["Alpha", "Beta"]

    async def test_long_name_add_is_rejected_before_database_write(
        self, store_with_items: store.DataStore
    ) -> None:
        from pydantic import ValidationError

        session = store_with_items
        with pytest.raises(ValidationError):
            await session.add_item("x" * 501)
        reloaded = await store.DataStore.create(session._session_id)
        assert [item["name"] for item in reloaded.items] == ["Alpha", "Beta"]

    async def test_bulk_over_item_limit_is_rejected_before_database_write(
        self, store_with_items: store.DataStore
    ) -> None:
        from pydantic import ValidationError

        session = store_with_items
        with pytest.raises(ValidationError):
            await session.add_items_bulk([f"Item {index}" for index in range(10_001)])
        reloaded = await store.DataStore.create(session._session_id)
        assert len(reloaded.items) == 2

    async def test_too_many_criteria_are_rejected_before_database_write(
        self, store_with_items: store.DataStore
    ) -> None:
        from pydantic import ValidationError

        session = store_with_items
        with pytest.raises(ValidationError):
            await session.set_criteria(
                [
                    {
                        "key": f"criterion_{index}",
                        "label": "기준",
                        "color": "blue",
                        "weight": 1.0,
                    }
                    for index in range(65)
                ]
            )
        reloaded = await store.DataStore.create(session._session_id)
        assert len(reloaded.criteria) == 6

    async def test_invalid_direct_save_preserves_existing_database(
        self, store_with_items: store.DataStore
    ) -> None:
        from pydantic import ValidationError

        session = store_with_items
        original = session.export_json()
        session.items[0]["mu"][session.criteria[0]["key"]] = float("inf")
        with pytest.raises(ValidationError):
            await session.save()
        reloaded = await store.DataStore.create(session._session_id)
        assert reloaded.export_json() == original

    async def test_http_long_name_returns_validation_error_and_session_survives(
        self, store_with_items: store.DataStore
    ) -> None:
        import httpx
        from ranker.main import app

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            cookies={"session_id": store_with_items._session_id},
        ) as client:
            assert (
                await client.post("/manage/add", data={"name": "x" * 501})
            ).status_code == 422
            assert (await client.get("/manage")).status_code == 200
