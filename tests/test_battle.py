"""투표 검증, 3개 비교 응답의 해석, 투표 기록의 취소·보관·재계산."""

import json
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from ranker import store
from ranker.database import fetch_events
from ranker.schemas import BattleVoteRequest
from ranker.services import display_rating
from tests.helpers import approx_items, vote

BALLOTS = pytest.mark.parametrize(
    "ballot",
    [((1, 2), "1"), ((1, 2, 3), {"1": "best", "3": "worst"})],
    ids=["2way", "3way"],
)


class TestBattleVoteValidation:
    async def test_vote_model_rejects_same_item_payload(
        self, store_with_items: store.DataStore
    ) -> None:
        votes = {criterion["key"]: "1" for criterion in store_with_items.criteria}
        with pytest.raises(ValidationError):
            BattleVoteRequest(item1_id=1, item2_id=1, round_token="x" * 24, votes=votes)

    async def test_apply_battle_vote_rejects_replayed_round(
        self, store_with_items: store.DataStore
    ) -> None:
        s = store_with_items
        token = await s.issue_battle_round([1, 2])
        payload = BattleVoteRequest(
            item1_id=1,
            item2_id=2,
            round_token=token,
            votes={c["key"]: "1" for c in s.criteria},
        )
        assert (await s.apply_vote(payload))["a1_id"] == 1
        with pytest.raises(store.StaleBattleRoundError):
            await s.apply_vote(payload)

    async def test_unknown_winner_value_raises(
        self, store_with_items: store.DataStore
    ) -> None:
        """요청 검증을 거치지 않은 알 수 없는 선택은 무승부로 넘기지 않고 거절합니다."""
        s = store_with_items
        token = await s.issue_battle_round([1, 2])
        payload = SimpleNamespace(
            item1_id=1,
            item2_id=2,
            round_token=token,
            votes={c["key"]: "unknown" for c in s.criteria},
        )
        with pytest.raises(store.InvalidBattleVoteError):
            await s.apply_vote(payload)  # type: ignore[arg-type]


class TestThreeWayBallots:
    async def test_best_only_tied_vote(
        self, store_with_three_items: store.DataStore
    ) -> None:
        s = store_with_three_items
        response = await vote(s, (1, 2, 3), {"1": "best", "2": "tied", "3": "tied"})
        for r in response["results"]:
            assert r["diffs"]["1"] > 0
        for c in s.criteria:
            assert c["draws"] > 0

    async def test_worst_only_vote(
        self, store_with_three_items: store.DataStore
    ) -> None:
        s = store_with_three_items
        response = await vote(s, (1, 2, 3), {"1": "worst", "2": "tied", "3": "tied"})
        for r in response["results"]:
            assert r["diffs"]["1"] < 0
            assert r["best_id"] is None
            assert r["worst_id"] == 1
        for c in s.criteria:
            assert c["draws"] == 1

    async def test_all_tied_vote(self, store_with_three_items: store.DataStore) -> None:
        s = store_with_three_items
        response = await vote(s, (1, 2, 3), {"1": "tied", "2": "tied", "3": "tied"})
        for r in response["results"]:
            # 점수가 같은 항목들의 대칭 동률이라 점수가 거의 움직이지 않습니다.
            assert all(abs(diff) < 0.1 for diff in r["diffs"].values())
            assert r["best_id"] is None
            assert r["worst_id"] is None
        for c in s.criteria:
            assert c["draws"] == 1

    async def test_full_ranking_vote(
        self, store_with_three_items: store.DataStore
    ) -> None:
        """최고와 최하만 고르면 남은 항목을 중간으로 둔 완전 순위 한 건입니다."""
        s = store_with_three_items
        response = await vote(s, (1, 2, 3), {"1": "best", "3": "worst"})
        for r in response["results"]:
            assert r["diffs"]["1"] > 0
            assert r["diffs"]["3"] < 0
            assert r["best_id"] == 1
            assert r["worst_id"] == 3
        for c in s.criteria:
            assert c["draws"] == 0
            assert c["battles"] == 1

    @pytest.mark.parametrize(
        "ballot",
        [
            {"1": "best", "2": "best", "3": "worst"},
            {"abc": "best", "2": "tied", "3": "tied"},
            # int("01") == 1이라 항목 1이 자기 자신과 비교됩니다.
            {"1": "best", "01": "tied", "3": "tied"},
        ],
        ids=["two-best", "non-numeric-id", "same-item-twice"],
    )
    async def test_invalid_ballot_is_rejected(
        self, store_with_three_items: store.DataStore, ballot: dict[str, str]
    ) -> None:
        with pytest.raises(store.InvalidBattleVoteError):
            await vote(store_with_three_items, (1, 2, 3), ballot)


class TestVoteHistory:
    @BALLOTS
    async def test_skip_does_not_change_rating_or_counter(
        self, store_with_three_items: store.DataStore, ballot
    ) -> None:
        session = store_with_three_items
        key = session.criteria[0]["key"]
        before = deepcopy(session.items)
        result = await vote(session, *ballot, skip={key})
        for item, original in zip(session.items, before):
            assert item["mu"][key] == original["mu"][key]
            assert item["sigma_sq"][key] == original["sigma_sq"][key]
            assert item["criterion_matches"][key] == 0
        assert session.criteria[0]["battles"] == 0
        assert result["results"][0]["skipped"] is True

    @BALLOTS
    async def test_undo_and_full_refit(
        self, store_with_three_items: store.DataStore, ballot
    ) -> None:
        session = store_with_three_items
        baseline = deepcopy(session.items)
        await vote(session, *ballot)
        first = deepcopy(session.items)
        await vote(session, *ballot)
        await session.undo_last_vote(session.revision)
        assert session.items == approx_items(first)
        replay = await session.recalculate_ratings()
        assert replay["responses"] == len(session.criteria)
        assert session.items == approx_items(first)
        await session.undo_last_vote(session.revision)
        assert session.items == approx_items(baseline)
        with pytest.raises(store.StaleBattleRoundError, match="취소할 투표가 없습니다"):
            await session.undo_last_vote(session.revision)

    async def test_double_undo_is_rejected(
        self, store_with_items: store.DataStore
    ) -> None:
        session = store_with_items
        await vote(session)
        await vote(session)
        seen = session.revision
        await session.undo_last_vote(seen)
        with pytest.raises(store.StaleBattleRoundError):
            await session.undo_last_vote(seen)
        assert not (await fetch_events(session.session_id))[0]["undone"]

    async def test_structure_edit_resets_undo_baseline(
        self, store_with_items: store.DataStore
    ) -> None:
        session = store_with_items
        await vote(session)
        await session.delete_item(2)
        with pytest.raises(store.StaleBattleRoundError):
            await session.undo_last_vote(session.revision)
        await session.recalculate_ratings()
        assert [item["id"] for item in session.items] == [1]

    async def test_history_export_import_and_current_settings_replay(
        self, store_with_items: store.DataStore
    ) -> None:
        session = store_with_items
        await vote(session)
        expected = deepcopy(session.items)
        raw = await session.export_json()
        preview = session.preview_import(raw)
        assert preview["history"] == 1
        await session.import_json(raw)
        assert len(await fetch_events(session.session_id)) == 1
        result = await session.recalculate_ratings({"display_center": 1500})
        assert result["responses"] == len(session.criteria)
        assert session.settings["display_center"] == 1500
        assert session.items == approx_items(expected)

    @BALLOTS
    async def test_result_matches_final_refitted_rating(
        self, store_with_three_items: store.DataStore, ballot
    ) -> None:
        session = store_with_three_items
        ids, _ = ballot
        session.items[0]["mu"][session.criteria[0]["key"]] = 2.0
        await session._save_to_db()
        response = await vote(session, *ballot)
        for result in response["results"]:
            for iid in ids:
                displayed = (
                    result["ratings"][str(iid)]
                    if len(ids) == 3
                    else result[f"new_r{iid}"]
                )
                assert displayed == round(
                    display_rating(session, session.get_item(iid)["mu"][result["key"]]),
                    1,
                )

    async def test_clear_history_retains_refittable_observations(
        self, store_with_items: store.DataStore
    ) -> None:
        session = store_with_items
        await vote(session)
        expected = deepcopy(session.items)
        await session.clear_history()
        assert (await fetch_events(session.session_id)) == []
        assert session.items == approx_items(expected)
        await session.recalculate_ratings()
        assert session.items == approx_items(expected)

    async def test_archived_votes_survive_structure_change_and_export(
        self, store_with_items: store.DataStore
    ) -> None:
        session = store_with_items
        await vote(session)
        await session.delete_item(2)
        assert len(await fetch_events(session.session_id)) == 1
        assert (await fetch_events(session.session_id))[0]["archived"] is True
        raw = await session.export_json()
        await session.import_json(raw)
        assert (await fetch_events(session.session_id))[0]["payload"]["item2_id"] == 2
        assert (await session.recalculate_ratings())["responses"] == len(
            session.criteria
        )
        assert [item["id"] for item in session.items] == [1]

    async def test_rename_preserves_history_and_current_name_on_undo_replay(
        self, store_with_items: store.DataStore
    ) -> None:
        session = store_with_items
        await vote(session)
        await session.rename_item(1, "Renamed")
        assert len(await fetch_events(session.session_id)) == 1
        assert not (await fetch_events(session.session_id))[0]["archived"]
        assert (await session.recalculate_ratings())["responses"] == len(
            session.criteria
        )
        assert session.get_item(1)["name"] == "Renamed"
        await session.undo_last_vote(session.revision)
        assert session.get_item(1)["name"] == "Renamed"

    async def test_archived_votes_with_removed_criterion_can_be_restored(
        self, store_with_items: store.DataStore
    ) -> None:
        session = store_with_items
        await vote(session)
        await session.set_criteria(session.criteria[:1])
        assert (await fetch_events(session.session_id))[0]["archived"]
        await session.import_json(await session.export_json())
        assert len(session.criteria) == 1
        assert len((await fetch_events(session.session_id))[0]["payload"]["votes"]) == 6

    async def test_import_persists_validated_history_values(
        self, store_with_items: store.DataStore
    ) -> None:
        session = store_with_items
        await vote(session)
        backup = json.loads(await session.export_json())
        event = backup["history"][0]
        event["payload"]["item1_id"] = "1"
        event["payload"]["item2_id"] = "2"
        event["created_at"] = str(int(event["created_at"]))
        await session.import_json(json.dumps(backup))
        session = await store.open_store(session.session_id)
        stored = (await fetch_events(session.session_id))[0]
        assert stored["payload"]["item1_id"] == 1
        assert isinstance(stored["created_at"], float)
        assert stored["names"] == {"1": "Alpha", "2": "Beta"}
        await session.undo_last_vote(session.revision)
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
        session = store_with_items
        await vote(session)
        original = await session.export_json()
        backup = json.loads(original)
        backup["history"][0]["created_at"] = timestamp
        with pytest.raises(ValidationError, match="created_at"):
            await session.import_json(json.dumps(backup))
        reloaded = await store.open_store(session.session_id)
        assert await reloaded.export_json() == original

    async def test_import_timestamp_boundary_can_be_rendered(
        self, store_with_items: store.DataStore
    ) -> None:
        session = store_with_items
        await vote(session)
        backup = json.loads(await session.export_json())
        backup["history"][0]["created_at"] = 253_402_300_799
        await session.import_json(json.dumps(backup))
        timestamp = (await fetch_events(session.session_id))[0]["created_at"]
        assert datetime.fromtimestamp(timestamp, UTC).year == 9999
