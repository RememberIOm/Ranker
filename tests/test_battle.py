from typing import Any

import pytest
from pydantic import ValidationError

from ranker import store
from ranker.schemas import BattleVoteRequest, ThreeWayBattleVoteRequest


class TestBattleVoteValidation:
    async def test_vote_model_rejects_same_item_payload(
        self, store_with_items: store.DataStore
    ) -> None:
        item = store_with_items.items[0]
        votes = {criterion["key"]: "1" for criterion in store_with_items.criteria}

        with pytest.raises(ValidationError):
            BattleVoteRequest(
                item1_id=item["id"],
                item2_id=item["id"],
                round_token="x" * 24,
                votes=votes,
                redirect_to="/battle",
            )

    async def test_apply_battle_vote_rejects_replayed_round(
        self, store_with_items: store.DataStore
    ) -> None:
        item1 = store_with_items.items[0]
        item2 = store_with_items.items[1]
        votes = {criterion["key"]: "1" for criterion in store_with_items.criteria}
        round_token = await store_with_items.issue_battle_round(
            [item1["id"], item2["id"]]
        )
        payload = BattleVoteRequest(
            item1_id=item1["id"],
            item2_id=item2["id"],
            round_token=round_token,
            votes=votes,
            redirect_to="/battle",
        )

        result = await store_with_items.apply_vote(payload)
        assert result["a1_id"] == item1["id"]

        with pytest.raises(store.StaleBattleRoundError):
            await store_with_items.apply_vote(payload)

    async def test_unknown_winner_value_raises(
        self, store_with_items: store.DataStore
    ) -> None:
        """schema가 아닌 경로로 미지의 winner 값이 들어오면 silent draw가 아닌 명시 실패.

        BattleVoteRequest의 Literal 검증을 우회하는 dict 페이로드를 직접 주입해
        store 계층의 fail-fast 분기를 검증합니다 (회귀 보호).
        """
        from types import SimpleNamespace

        s = store_with_items
        item1 = s.items[0]
        item2 = s.items[1]
        token = await s.issue_battle_round([item1["id"], item2["id"]])
        votes = {c["key"]: "unknown" for c in s.criteria}  # 알 수 없는 vote 값
        # Literal 검증을 우회하기 위해 SimpleNamespace로 페이로드 모사
        payload = SimpleNamespace(
            item1_id=item1["id"],
            item2_id=item2["id"],
            round_token=token,
            votes=votes,
            redirect_to="/battle",
        )

        with pytest.raises(store.InvalidBattleVoteError):
            await s.apply_vote(payload)  # type: ignore[arg-type]

    async def test_vote_result_contains_sigma(
        self, store_with_items: store.DataStore
    ) -> None:
        """투표 결과에 sigma1/sigma2 필드가 포함됨"""
        item1 = store_with_items.items[0]
        item2 = store_with_items.items[1]
        votes = {criterion["key"]: "1" for criterion in store_with_items.criteria}
        token = await store_with_items.issue_battle_round([item1["id"], item2["id"]])
        payload = BattleVoteRequest(
            item1_id=item1["id"],
            item2_id=item2["id"],
            round_token=token,
            votes=votes,
            redirect_to="/battle",
        )
        result = await store_with_items.apply_vote(payload)
        for r in result["results"]:
            assert "sigma1" in r
            assert "sigma2" in r
            assert r["sigma1"] > 0
            assert r["sigma2"] > 0


# --- 3-way Tied Vote ---


class TestThreeWayTiedVote:
    async def test_best_only_tied_vote(
        self, store_with_three_items: store.DataStore
    ) -> None:
        """3-way 'best only' 투표: best > tied_a, best > tied_b, tied_a ≈ tied_b"""
        s = store_with_three_items
        items = s.items
        token = await s.issue_battle_round(
            [items[0]["id"], items[1]["id"], items[2]["id"]]
        )

        votes = {}
        for c in s.criteria:
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
        resp_data = await s.apply_vote(payload)

        # best(item1)는 레이팅 상승
        for r in resp_data["results"]:
            best_diff = r["diffs"][str(items[0]["id"])]
            assert best_diff > 0

        # draws 통계가 증가 (tied 쌍 = 무승부)
        for c in s.criteria:
            assert c["draws"] > 0

    async def test_worst_only_vote(
        self, store_with_three_items: store.DataStore
    ) -> None:
        """3-way 'worst only' 투표: tied_a > worst, tied_b > worst, tied_a ≈ tied_b"""
        s = store_with_three_items
        items = s.items
        token = await s.issue_battle_round(
            [items[0]["id"], items[1]["id"], items[2]["id"]]
        )

        votes = {}
        for c in s.criteria:
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
        resp_data = await s.apply_vote(payload)

        # worst(item1)는 레이팅 하락
        for r in resp_data["results"]:
            worst_diff = r["diffs"][str(items[0]["id"])]
            assert worst_diff < 0
            assert r["best_id"] is None
            assert r["worst_id"] == items[0]["id"]
            assert r["middle_id"] is None

        # draws 통계 증가 (tied 쌍 1개)
        for c in s.criteria:
            assert c.get("draws", 0) == 1

    async def test_all_tied_vote(self, store_with_three_items: store.DataStore) -> None:
        """3-way 모두 무승부: 3개 항목 모두 tied"""
        s = store_with_three_items
        items = s.items
        token = await s.issue_battle_round(
            [items[0]["id"], items[1]["id"], items[2]["id"]]
        )

        votes = {}
        for c in s.criteria:
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
        resp_data = await s.apply_vote(payload)

        # 모든 레이팅 변화가 0에 가까움 (동일 레이팅 항목들의 대칭 무승부)
        for r in resp_data["results"]:
            for item in items:
                diff = abs(r["diffs"][str(item["id"])])
                assert diff < 0.1
            assert r["best_id"] is None
            assert r["worst_id"] is None
            assert r["middle_id"] is None

        # draws 통계: 기준당 동률 응답 한 건
        for c in s.criteria:
            assert c.get("draws", 0) == 1

    async def test_invalid_role_combination(
        self, store_with_three_items: store.DataStore
    ) -> None:
        """잘못된 역할 조합 (best 2개) → InvalidBattleVoteError"""
        s = store_with_three_items
        items = s.items
        token = await s.issue_battle_round(
            [items[0]["id"], items[1]["id"], items[2]["id"]]
        )

        votes = {}
        for c in s.criteria:
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
            await s.apply_vote(payload)

    async def test_non_numeric_id_key_rejected(
        self, store_with_three_items: store.DataStore
    ) -> None:
        """3-way vote의 inner key가 정수 변환 불가 → InvalidBattleVoteError (500 아님)"""
        s = store_with_three_items
        items = s.items
        token = await s.issue_battle_round(
            [items[0]["id"], items[1]["id"], items[2]["id"]]
        )

        votes = {}
        for c in s.criteria:
            votes[c["key"]] = {
                "abc": "best",
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
        with pytest.raises(store.InvalidBattleVoteError):
            await s.apply_vote(payload)

    async def test_duplicate_item_id_in_vote_rejected(
        self, store_with_three_items: store.DataStore
    ) -> None:
        """3-way vote에서 같은 정수 ID가 두 번 등장 ('1' + '01') → InvalidBattleVoteError

        클라이언트 조작이나 버그로 best와 tied에 같은 항목이 들어가면 자기 자신과 비교되어
        σ²만 줄고 μ는 안 변하는 비정상 동작을 방지.
        """
        s = store_with_three_items
        items = s.items
        # item1과 동일한 정수로 변환되는 두 키 ("1"과 "01")를 동시에 등장시킴
        if items[0]["id"] != 1:
            pytest.skip("이 회귀 테스트는 첫 항목 id가 1일 때만 의미가 있습니다.")
        token = await s.issue_battle_round(
            [items[0]["id"], items[1]["id"], items[2]["id"]]
        )

        votes = {}
        for c in s.criteria:
            votes[c["key"]] = {
                "1": "best",
                "01": "tied",  # int("01") == 1 → 같은 항목
                str(items[2]["id"]): "tied",
            }
        payload = ThreeWayBattleVoteRequest(
            item1_id=items[0]["id"],
            item2_id=items[1]["id"],
            item3_id=items[2]["id"],
            round_token=token,
            votes=votes,
        )
        with pytest.raises(store.InvalidBattleVoteError):
            await s.apply_vote(payload)


# --- 3-way Mode B (Full Ranking) ---


class TestThreeWayModeBVote:
    async def test_full_ranking_vote(
        self, store_with_three_items: store.DataStore
    ) -> None:
        """Mode B: best + worst만 지정 → best > middle > worst, 무승부 없음"""
        s = store_with_three_items
        items = s.items
        token = await s.issue_battle_round(
            [items[0]["id"], items[1]["id"], items[2]["id"]]
        )

        # best=item0, worst=item2, middle=item1 (미지정 → 자동 추론)
        votes = {}
        for c in s.criteria:
            votes[c["key"]] = {
                str(items[0]["id"]): "best",
                str(items[2]["id"]): "worst",
            }

        payload = ThreeWayBattleVoteRequest(
            item1_id=items[0]["id"],
            item2_id=items[1]["id"],
            item3_id=items[2]["id"],
            round_token=token,
            votes=votes,
        )
        resp_data = await s.apply_vote(payload)

        for r in resp_data["results"]:
            assert r["diffs"][str(items[0]["id"])] > 0  # best 상승
            assert r["diffs"][str(items[2]["id"])] < 0  # worst 하락
            assert r["best_id"] == items[0]["id"]
            assert r["worst_id"] == items[2]["id"]
            assert r["middle_id"] == items[1]["id"]

        # Mode B: 동률 없는 완전 순위 응답 한 건
        for c in s.criteria:
            assert c.get("draws", 0) == 0
            assert c["battles"] == 1


class TestVoteHistory:
    async def _vote(
        self,
        session: store.DataStore,
        *,
        three_way: bool = False,
        skip_key: str | None = None,
    ) -> dict[str, Any]:
        token = await session.issue_battle_round([1, 2, 3] if three_way else [1, 2])
        votes = {
            c["key"]: ({"1": "best", "3": "worst"} if three_way else "1")
            for c in session.criteria
        }
        if skip_key is not None:
            votes[skip_key] = "skip"
        if three_way:
            return await session.apply_vote(
                ThreeWayBattleVoteRequest(
                    item1_id=1, item2_id=2, item3_id=3, round_token=token, votes=votes
                )
            )
        return await session.apply_vote(
            BattleVoteRequest(item1_id=1, item2_id=2, round_token=token, votes=votes)
        )

    @pytest.mark.parametrize("three_way", [False, True])
    async def test_skip_does_not_change_rating_or_counter(
        self, store_with_three_items: store.DataStore, three_way: bool
    ) -> None:
        from copy import deepcopy

        session = store_with_three_items
        key = session.criteria[0]["key"]
        before = deepcopy(session.items)
        result = await self._vote(session, three_way=three_way, skip_key=key)
        for item, original in zip(session.items, before):
            assert item["mu"][key] == original["mu"][key]
            assert item["sigma_sq"][key] == original["sigma_sq"][key]
            assert item["criterion_matches"][key] == 0
        assert session.criteria[0]["battles"] == 0
        assert result["results"][0]["skipped"] is True

    @pytest.mark.parametrize("three_way", [False, True])
    async def test_undo_and_full_refit(
        self, store_with_three_items: store.DataStore, three_way: bool
    ) -> None:
        from copy import deepcopy

        session = store_with_three_items
        baseline = deepcopy(session.items)
        await self._vote(session, three_way=three_way)
        first = deepcopy(session.items)
        await self._vote(session, three_way=three_way)
        await session.undo_last_vote(expected_event_id=2)
        assert session.items == first
        replay = await session.recalculate_ratings()
        assert replay["responses"] == len(session.criteria)
        assert session.items == first
        await session.undo_last_vote(expected_event_id=1)
        assert session.items == baseline
        with pytest.raises(store.StaleBattleRoundError):
            await session.undo_last_vote()

    async def test_double_undo_is_rejected(
        self, store_with_items: store.DataStore
    ) -> None:
        session = store_with_items
        await self._vote(session)
        await self._vote(session)
        await session.undo_last_vote(expected_event_id=2)
        with pytest.raises(store.StaleBattleRoundError):
            await session.undo_last_vote(expected_event_id=2)
        assert not (await session.history_events())[0]["undone"]

    async def test_structure_edit_resets_undo_baseline(
        self, store_with_items: store.DataStore
    ) -> None:
        session = store_with_items
        await self._vote(session)
        await session.delete_item(2)
        with pytest.raises(store.StaleBattleRoundError):
            await session.undo_last_vote()
        await session.recalculate_ratings()
        assert [item["id"] for item in session.items] == [1]

    async def test_history_export_import_and_current_settings_replay(
        self, store_with_items: store.DataStore
    ) -> None:
        from copy import deepcopy

        session = store_with_items
        await self._vote(session)
        expected = deepcopy(session.items)
        raw = await session.export_json()
        preview = session.preview_import(raw)
        assert preview["history"] == 1
        await session.import_json(raw)
        assert len(await session.history_events()) == 1
        result = await session.recalculate_ratings({"display_center": 1500})
        assert result["responses"] == len(session.criteria)
        assert session.settings["display_center"] == 1500
        assert session.items == expected

    @pytest.mark.parametrize("three_way", [False, True])
    async def test_result_matches_final_refitted_rating(
        self, store_with_three_items: store.DataStore, three_way: bool
    ) -> None:
        from ranker.services import display_rating

        session = store_with_three_items
        session.items[0]["mu"][session.criteria[0]["key"]] = 2.0
        await session._save_to_db()
        response = await self._vote(session, three_way=three_way)
        for result in response["results"]:
            for item in session.items[: 3 if three_way else 2]:
                displayed = (
                    result["ratings"][str(item["id"])]
                    if three_way
                    else result[f"new_r{item['id']}"]
                )
                assert displayed == round(
                    display_rating(session, item["mu"][result["key"]]), 1
                )

    async def test_clear_history_retains_refittable_observations(
        self, store_with_items: store.DataStore
    ) -> None:
        from copy import deepcopy

        session = store_with_items
        await self._vote(session)
        expected = deepcopy(session.items)
        await session.clear_history()
        assert (await session.history_events()) == []
        assert session.items == expected
        await session.recalculate_ratings()
        assert session.items == expected

    async def test_archived_votes_survive_structure_change_and_export(
        self, store_with_items: store.DataStore
    ) -> None:
        session = store_with_items
        await self._vote(session)
        await session.delete_item(2)
        assert len(await session.history_events()) == 1
        assert (await session.history_events())[0]["archived"] is True
        raw = await session.export_json()
        await session.import_json(raw)
        assert (await session.history_events())[0]["payload"]["item2_id"] == 2
        assert (await session.recalculate_ratings())["responses"] == len(
            session.criteria
        )
        assert [item["id"] for item in session.items] == [1]

    async def test_rename_preserves_history_and_current_name_on_undo_replay(
        self, store_with_items: store.DataStore
    ) -> None:
        session = store_with_items
        await self._vote(session)
        await session.rename_item(1, "Renamed")
        assert len(await session.history_events()) == 1
        assert not (await session.history_events())[0]["archived"]
        assert (await session.recalculate_ratings())["responses"] == len(
            session.criteria
        )
        assert session.get_item(1)["name"] == "Renamed"
        await session.undo_last_vote()
        assert session.get_item(1)["name"] == "Renamed"

    async def test_archived_votes_with_removed_criterion_can_be_restored(
        self, store_with_items: store.DataStore
    ) -> None:
        session = store_with_items
        await self._vote(session)
        await session.set_criteria(session.criteria[:1])
        assert (await session.history_events())[0]["archived"]
        await session.import_json(await session.export_json())
        assert len(session.criteria) == 1
        assert len((await session.history_events())[0]["payload"]["votes"]) == 6

    async def test_import_persists_validated_history_values(
        self, store_with_items: store.DataStore
    ) -> None:
        import json

        session = store_with_items
        await self._vote(session)
        backup = json.loads(await session.export_json())
        event = backup["history"][0]
        event["payload"]["item1_id"] = "1"
        event["payload"]["item2_id"] = "2"
        event["created_at"] = str(int(event["created_at"]))
        await session.import_json(json.dumps(backup))
        session = await store.open_store(session.session_id)
        stored = (await session.history_events())[0]
        assert stored["payload"]["item1_id"] == 1
        assert isinstance(stored["created_at"], float)
        assert stored["names"] == {"1": "Alpha", "2": "Beta"}
        await session.undo_last_vote()
        assert all(item["matches_played"] == 0 for item in session.items)
        assert all(
            item["mu"][criterion["key"]] == 0
            for item in session.items
            for criterion in session.criteria
        )

    @pytest.mark.parametrize("timestamp", [253_402_300_800, 1e12, 10**400])
    async def test_import_rejects_unrenderable_event_timestamp(
        self, store_with_items: store.DataStore, timestamp: float
    ) -> None:
        import json

        session = store_with_items
        await self._vote(session)
        original = await session.export_json()
        backup = json.loads(original)
        backup["history"][0]["created_at"] = timestamp
        with pytest.raises(ValidationError, match="created_at"):
            await session.import_json(json.dumps(backup))
        reloaded = await store.open_store(session._session_id)
        assert await reloaded.export_json() == original

    async def test_import_timestamp_boundary_can_be_rendered(
        self, store_with_items: store.DataStore
    ) -> None:
        import json
        from datetime import UTC, datetime

        session = store_with_items
        await self._vote(session)
        backup = json.loads(await session.export_json())
        backup["history"][0]["created_at"] = 253_402_300_799
        await session.import_json(json.dumps(backup))
        timestamp = (await session.history_events())[0]["created_at"]
        assert datetime.fromtimestamp(timestamp, UTC).year == 9999
