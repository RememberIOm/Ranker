"""Persistent inference, undo, and matchmaking across model/data changes."""

import json
import random
from copy import deepcopy

import numpy as np
import pytest
from pydantic import ValidationError

from ranker.rating_engine import FitConvergenceError, fit_rankings
from ranker.schemas import BattleVoteRequest, ThreeWayBattleVoteRequest
from ranker.services import (
    candidate_utilities,
    get_match_pair,
    get_match_probabilities,
    get_match_triple,
)
from ranker.store import InvalidSessionDataError, open_store


async def vote(session, a=1, b=2, winner="1", c=None):
    token = await session.issue_battle_round([a, b, c])
    votes = {key["key"]: winner for key in session.criteria}
    if c is not None:
        return await session.apply_vote(
            ThreeWayBattleVoteRequest(
                item1_id=a, item2_id=b, item3_id=c, round_token=token, votes=votes
            )
        )
    return await session.apply_vote(
        BattleVoteRequest(item1_id=a, item2_id=b, round_token=token, votes=votes)
    )


async def test_corpus_survives_compaction_addition_and_prior_change(store_with_items):
    s = store_with_items
    await vote(s)
    await vote(s, winner="2")
    before = deepcopy(s.items)
    await s.clear_history()
    assert (await s.history_events()) == []
    await s.add_items(["New"])
    await s.recalculate_ratings()
    assert s.items[:2] == before
    assert s.items[2]["mu"]["story"] == 0
    await s.update_settings({"initial_sigma": 3})
    reference = fit_rankings([((1,), (2,)), ((2,), (1,))], initial_sigma=3)
    np.testing.assert_allclose(
        s.posterior("story").covariance([1, 2]), reference.covariance([1, 2])
    )
    assert s.items[2]["sigma_sq"]["story"] == 9
    await s.import_json(await s.export_json())
    assert s.criteria[0]["battles"] == 2


async def test_undo_restores_nonparticipants_and_joint_covariance(
    store_with_three_items,
):
    s = store_with_three_items
    await vote(s)
    await vote(s, a=2, b=3)
    before = deepcopy(s.items)
    covariance = s.posterior("story").covariance([1, 2, 3])
    await vote(s, a=1, b=3, winner="2")
    assert s.get_item(2)["mu"]["story"] != before[1]["mu"]["story"]
    await s.undo_last_vote()
    assert s.items == before
    np.testing.assert_allclose(s.posterior("story").covariance([1, 2, 3]), covariance)
    reloaded = await open_store(s._session_id)
    np.testing.assert_allclose(
        reloaded.posterior("story").covariance([1, 2, 3]), covariance
    )


async def test_deleted_opponents_remain_latent_and_ids_are_not_reused(store_with_items):
    s = store_with_items
    await vote(s)
    score = s.items[0]["mu"]["story"]
    await s.delete_item(2)
    await s.clear_history()
    await s.add_items(["New"])
    assert s.items[-1]["id"] == 3
    await s.recalculate_ratings()
    assert s.items[0]["mu"]["story"] == score
    assert s.posterior("story").item_ids == (1, 2)
    await s.import_json(await s.export_json())
    assert s.items[0]["mu"]["story"] == score


async def test_removed_criterion_cannot_resurrect_observations(store_with_items):
    s = store_with_items
    original = deepcopy(s.criteria)
    await vote(s)
    await s.set_criteria(s.criteria[1:])
    await s.set_criteria(original)
    await s.recalculate_ratings()
    assert s.items[0]["mu"]["story"] == 0
    assert s.items[0]["mu"]["visual"] > 0
    assert s.criteria[0]["battles"] == 0


async def test_one_three_way_ballot_counts_as_one_response(store_with_three_items):
    s = store_with_three_items
    await vote(s, c=3, winner={"1": "tied", "2": "tied", "3": "tied"})
    for criterion in s.criteria:
        assert criterion["battles"] == criterion["draws"] == 1
        for item in s.items:
            assert item["criterion_matches"][criterion["key"]] == 1


async def test_optimizer_failure_preserves_round_ratings_and_observations(
    store_with_items, monkeypatch
):
    from ranker import store

    s = store_with_items
    token = await s.issue_battle_round([1, 2])
    before = await s.export_json()

    def fail(*args, **kwargs):
        raise FitConvergenceError("did not converge")

    monkeypatch.setattr(store, "fit_rankings", fail)
    with pytest.raises(FitConvergenceError):
        await s.apply_vote(
            BattleVoteRequest(
                item1_id=1,
                item2_id=2,
                round_token=token,
                votes={c["key"]: "1" for c in s.criteria},
            )
        )
    assert await s.export_json() == before
    assert s.active_round["token"] == token
    reloaded = await open_store(s._session_id)
    assert await reloaded.export_json() == before


async def test_import_rejects_missing_undoable_observations(store_with_items):
    s = store_with_items
    await vote(s)
    before = await s.export_json()
    backup = json.loads(before)
    backup["observations"] = {}
    with pytest.raises(InvalidSessionDataError, match="관측"):
        await s.import_json(json.dumps(backup))
    assert await s.export_json() == before


async def test_pair_predictions_reload_and_round_to_a_simplex(store_with_items):
    s = store_with_items
    await vote(s, winner="draw")
    a = get_match_probabilities(s, "story", 1, 2)
    loaded = await open_store(s._session_id)
    b = get_match_probabilities(loaded, "story", 1, 2)
    assert a == b
    assert sum(a.values()) == pytest.approx(100)
    assert min(a.values()) >= 0


async def test_matcher_prefers_bridge_between_disconnected_groups(temp_store):
    s = temp_store
    await s.set_criteria([{"key": "a", "label": "A", "weight": 1.0, "color": "blue"}])
    await s.add_items(["A", "B", "C", "D"])
    s._data["observations"]["a"] = [
        {"groups": ((1, 2),), "count": 100},
        {"groups": ((3, 4),), "count": 100},
    ]
    s._data["exposures"]["a"] = 200
    await s._save_to_db()
    await s.recalculate_ratings()
    random.seed(1)
    a, b = get_match_pair(s)
    assert ({a["id"], b["id"]} & {1, 2}) and ({a["id"], b["id"]} & {3, 4})


async def test_matcher_honors_weights_and_skipped_criterion_response_rate(temp_store):
    s = temp_store
    await s.set_criteria(
        [
            {"key": "a", "label": "A", "weight": 1.0, "color": "blue"},
            {"key": "b", "label": "B", "weight": 1.0, "color": "red"},
        ]
    )
    await s.add_items(["A", "B", "C", "D"])
    s._data["observations"] = {
        "a": [{"groups": ((1, 2),), "count": 100}],
        "b": [{"groups": ((3, 4),), "count": 100}],
    }
    s._data["exposures"] = {"a": 100, "b": 100}
    await s._save_to_db()
    await s.recalculate_ratings()
    choices = [(0, 1), (2, 3)]
    s.criteria[0]["weight"] = 10
    assert (
        candidate_utilities(s, s.items, choices)[1]
        > candidate_utilities(s, s.items, choices)[0]
    )
    s.criteria[0]["weight"] = 1
    s.criteria[1]["weight"] = 10
    assert (
        candidate_utilities(s, s.items, choices)[0]
        > candidate_utilities(s, s.items, choices)[1]
    )
    s.criteria[1]["weight"] = 1
    s._data["exposures"]["a"] = 10000
    assert (
        candidate_utilities(s, s.items, choices)[0]
        > candidate_utilities(s, s.items, choices)[1]
    )


async def test_matcher_preserves_focus_and_randomizes_positions_above_pool_limit(
    temp_store,
):
    s = temp_store
    await s.add_items([f"Item {i}" for i in range(100)])
    positions = set()
    for seed in range(8):
        random.seed(seed)
        triple = get_match_triple(s, focus_id=100)
        ids = [i["id"] for i in triple]
        assert len(set(ids)) == 3 and 100 in ids
        positions.add(ids.index(100))
    assert positions == {0, 1, 2}
    assert get_match_triple(s, focus_id=1000) == (None, None, None)


async def test_three_way_matcher_avoids_recent_partial_pairs(temp_store):
    s = temp_store
    await s.add_items(["A", "B", "C", "D", "E", "F"])
    await s.issue_battle_round([1, 2, 3])
    selected = {i["id"] for i in get_match_triple(s)}
    assert len(selected & {1, 2, 3}) <= 1


async def test_uniform_ties_are_not_determined_by_item_order(store_with_three_items):
    s = store_with_three_items
    pairs = set()
    for seed in range(15):
        random.seed(seed)
        pairs.add(frozenset(i["id"] for i in get_match_pair(s)))
    assert len(pairs) == 3


async def test_import_rejects_event_without_names(
    store_with_items,
):
    s = store_with_items
    await vote(s)
    backup = json.loads(await s.export_json())
    del backup["history"][0]["names"]
    with pytest.raises(ValidationError):
        s.parse_import(json.dumps(backup))


async def test_settings_fit_failure_leaves_the_previous_prior_and_scores(
    store_with_items, monkeypatch
):
    from ranker import store

    s = store_with_items
    await vote(s)
    before = await s.export_json()

    def fail(*args, **kwargs):
        raise FitConvergenceError("did not converge")

    monkeypatch.setattr(store, "fit_rankings", fail)
    with pytest.raises(FitConvergenceError):
        await s.update_settings({"initial_sigma": 3})
    assert await s.export_json() == before


async def test_missing_model_data_is_not_silently_replaced_by_a_prior(store_with_items):
    from ranker import database

    s = store_with_items
    async with database.transaction() as db:
        await db.execute(
            "DELETE FROM ranking_models WHERE session_id = ?", (s._session_id,)
        )
    with pytest.raises(InvalidSessionDataError):
        await open_store(s._session_id)


async def test_item_deleted_before_any_vote_keeps_its_id(store_with_items):
    s = store_with_items
    await s.add_items(["Temporary"])
    await s.delete_item(3)
    await s.add_items(["Next"])
    assert s.items[-1]["id"] == 4
    await s.import_json(await s.export_json())
    await s.add_items(["After import"])
    assert s.items[-1]["id"] == 5
