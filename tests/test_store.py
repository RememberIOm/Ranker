"""저장소의 세션 격리, 동시 저장, 롤백, 저장 전 검증."""

import asyncio
import sqlite3
from typing import Any

import pytest
from pydantic import ValidationError

from ranker import database, store
from ranker.schemas import MAX_ITEMS
from tests.helpers import vote


class TestStoreValidation:
    async def test_delete_session_clears_runtime_state(self, store_factory) -> None:
        session_id = "b" * 32
        session = await store_factory(session_id)
        store._get_lock(session_id)

        await store.delete_session(session.session_id)

        assert await store.open_store(session_id) is None
        assert session_id not in store._locks

    async def test_set_criteria_syncs_mu_sigma(
        self, temp_store: store.DataStore
    ) -> None:
        """기준을 바꾸면 항목마다 새 기준의 점수를 넣고 지운 기준의 점수를 뺍니다."""
        await temp_store.add_items(["Alpha"])
        await temp_store.set_criteria(
            [{"key": "new_crit", "label": "새기준", "color": "red", "weight": 1.0}]
        )
        item = temp_store.items[0]
        assert "new_crit" in item["mu"]
        assert "new_crit" in item["sigma_sq"]
        assert "story" not in item["mu"]
        assert "story" not in item["sigma_sq"]


class TestPerCriterionMatches:
    async def test_criterion_matches_incremented_after_vote(
        self, store_with_items: store.DataStore
    ) -> None:
        s = store_with_items
        await vote(s)
        for c in s.criteria:
            assert s.items[0]["criterion_matches"][c["key"]] == 1
            assert s.items[1]["criterion_matches"][c["key"]] == 1


class TestActiveRoundItem3Persistence:
    async def test_item3_id_survives_reload(self, store_factory) -> None:
        session_id = "f" * 32
        s = await store_factory(session_id)
        await s.add_items(["Alpha", "Beta", "Gamma"])
        token = await s.issue_battle_round([1, 2, 3])

        store._locks.clear()
        reloaded = await store.open_store(session_id)

        assert reloaded.active_round == {
            "token": token,
            "item1_id": 1,
            "item2_id": 2,
            "item3_id": 3,
            "issued_at": reloaded.active_round["issued_at"],
        }


class TestAddItemsBulk:
    async def test_skips_blank_names(self, temp_store: store.DataStore) -> None:
        """빈 이름은 건너뛰고 실제 추가된 개수만 반환"""
        count = await temp_store.add_items(["A", "", "  ", "B"])
        assert count == 2
        assert len(temp_store.items) == 2


class TestCascadeDelete:
    async def test_delete_session_removes_all_related_rows(self, store_factory) -> None:
        session_id = "d" * 32
        s = await store_factory(session_id)
        await s.add_items(["Alpha", "Beta"])

        await store.delete_session(session_id)

        assert await store.open_store(session_id) is None
        db = database.get_db()
        for table in ("items", "criteria", "item_ratings"):
            async with db.execute(
                f"SELECT COUNT(*) FROM {table} WHERE session_id = ?", (session_id,)
            ) as c:
                assert (await c.fetchone())[0] == 0, table


class TestSessionIsolation:
    async def test_concurrent_sessions_isolated(self, store_factory) -> None:
        s1 = await store_factory("1" * 32)
        s2 = await store_factory("2" * 32)
        await s1.add_items(["S1-Item"])
        await s2.add_items(["S2-Item"])

        s1r = await store.open_store("1" * 32)
        s2r = await store.open_store("2" * 32)

        assert [item["name"] for item in s1r.items] == ["S1-Item"]
        assert [item["name"] for item in s2r.items] == ["S2-Item"]


class TestStorageRecovery:
    async def test_two_snapshots_preserve_both_additions(
        self, store_with_items: store.DataStore
    ) -> None:
        """서로 다른 요청에서 로드한 스냅샷도 순서대로 병합됩니다."""
        sid = store_with_items.session_id
        a, b = await asyncio.gather(store.open_store(sid), store.open_store(sid))
        await asyncio.gather(a.add_items(["First"]), b.add_items(["Second"]))
        loaded = await store.open_store(sid)
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
        sid = store_with_items.session_id
        stale = await store.open_store(sid)
        await store.delete_session(sid)
        with pytest.raises(database.StaleSessionError):
            await stale.update_settings({"result_auto_skip": True})
        assert await store.open_store(sid) is None

    async def test_save_rejects_stale_external_mutation(
        self, store_with_items: store.DataStore
    ) -> None:
        stale = await store.open_store(store_with_items.session_id)
        await store_with_items.add_items(["Preserved"])
        with pytest.raises(database.StaleSessionError):
            await stale._save_to_db()
        assert len((await store.open_store(stale.session_id)).items) == 3

    async def test_cancelled_write_rolls_back_before_next_request(
        self, store_with_items: store.DataStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db = database.get_db()
        original = db.executemany

        async def cancel(*args: Any, **kwargs: Any) -> None:
            raise asyncio.CancelledError()

        monkeypatch.setattr(db, "executemany", cancel)
        with pytest.raises(asyncio.CancelledError):
            await store_with_items.add_items(["Must not survive"])
        monkeypatch.setattr(db, "executemany", original)
        assert not db.in_transaction
        loaded = await store.open_store(store_with_items.session_id)
        assert [item["name"] for item in loaded.items] == ["Alpha", "Beta"]

    async def test_reads_wait_for_complete_transaction(
        self, store_with_items: store.DataStore
    ) -> None:
        db = database.get_db()
        async with database.transaction():
            await db.execute(
                "UPDATE items SET name = 'Complete' WHERE session_id = ?",
                (store_with_items.session_id,),
            )
            task = asyncio.create_task(store.open_store(store_with_items.session_id))
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
        await store_with_items.issue_battle_round([1, 2])
        async with db.execute("SELECT COUNT(*) FROM rating_writes") as cursor:
            assert (await cursor.fetchone())[0] == 0

    async def test_import_confirmation_rejects_changed_snapshot(
        self, store_with_items: store.DataStore
    ) -> None:
        raw = await store_with_items.export_json()
        await store_with_items.stage_import(raw)
        await store_with_items.add_items(["New"])
        with pytest.raises(store.StaleImportError):
            await store_with_items.apply_staged_import()
        assert len(store_with_items.items) == 3

    async def test_database_error_is_reported_as_save_error(
        self, store_with_items: store.DataStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fail(*args: Any, **kwargs: Any) -> None:
            raise sqlite3.OperationalError("disk full")

        monkeypatch.setattr(database.get_db(), "executemany", fail)
        with pytest.raises(store.SessionSaveError):
            await store_with_items.add_items(["Failure"])
        assert not database.get_db().in_transaction

    async def test_bulk_over_item_limit_is_rejected_before_database_write(
        self, store_with_items: store.DataStore
    ) -> None:
        session = store_with_items
        with pytest.raises(store.ItemLimitError):
            await session.add_items([f"Item {index}" for index in range(MAX_ITEMS - 1)])
        reloaded = await store.open_store(session.session_id)
        assert len(reloaded.items) == 2

    async def test_too_many_criteria_are_rejected_before_database_write(
        self, store_with_items: store.DataStore
    ) -> None:
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
        reloaded = await store.open_store(session.session_id)
        assert len(reloaded.criteria) == 6

    async def test_invalid_direct_save_preserves_existing_database(
        self, store_with_items: store.DataStore
    ) -> None:
        session = store_with_items
        original = await session.export_json()
        session.items[0]["mu"][session.criteria[0]["key"]] = float("inf")
        with pytest.raises(ValidationError):
            await session._save_to_db()
        reloaded = await store.open_store(session.session_id)
        assert await reloaded.export_json() == original
