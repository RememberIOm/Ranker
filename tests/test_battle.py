import tempfile
from pathlib import Path

import pytest
from pydantic import ValidationError

from schemas import BattleVoteRequest
import store


@pytest.fixture()
async def session_with_items():
    tempdir = tempfile.TemporaryDirectory()
    original_session_dir = store.SESSION_DIR
    store.SESSION_DIR = Path(tempdir.name)
    store.SESSION_DIR.mkdir(parents=True, exist_ok=True)
    store._session_cache.clear()
    store._locks.clear()

    session = await store.get_store("c" * 32)
    await session.add_item("Alpha")
    await session.add_item("Beta")
    yield session

    store._session_cache.clear()
    store._locks.clear()
    store.SESSION_DIR = original_session_dir
    tempdir.cleanup()


class TestBattleVoteValidation:
    async def test_vote_model_rejects_same_item_payload(self, session_with_items: store.DataStore) -> None:
        item = session_with_items.items[0]
        votes = {criterion["key"]: "1" for criterion in session_with_items.criteria}

        with pytest.raises(ValidationError):
            BattleVoteRequest(
                item1_id=item["id"],
                item2_id=item["id"],
                round_token="x" * 24,
                votes=votes,
                redirect_to="/battle",
            )

    async def test_apply_battle_vote_rejects_replayed_round(self, session_with_items: store.DataStore) -> None:
        item1 = session_with_items.items[0]
        item2 = session_with_items.items[1]
        votes = {criterion["key"]: "1" for criterion in session_with_items.criteria}
        round_token = await session_with_items.issue_battle_round(item1["id"], item2["id"])
        payload = BattleVoteRequest(
            item1_id=item1["id"],
            item2_id=item2["id"],
            round_token=round_token,
            votes=votes,
            redirect_to="/battle",
        )

        result, should_normalize = await session_with_items.apply_battle_vote(payload)
        assert result["a1_id"] == item1["id"]
        assert should_normalize is False

        with pytest.raises(store.StaleBattleRoundError):
            await session_with_items.apply_battle_vote(payload)

    async def test_apply_battle_vote_never_normalizes(self, session_with_items: store.DataStore) -> None:
        """Bayesian BT에서는 정규화가 불필요 — should_normalize 항상 False"""
        item1 = session_with_items.items[0]
        item2 = session_with_items.items[1]
        votes = {criterion["key"]: "1" for criterion in session_with_items.criteria}

        for _ in range(5):
            token = await session_with_items.issue_battle_round(item1["id"], item2["id"])
            payload = BattleVoteRequest(
                item1_id=item1["id"],
                item2_id=item2["id"],
                round_token=token,
                votes=votes,
                redirect_to="/battle",
            )
            _, should_normalize = await session_with_items.apply_battle_vote(payload)
            assert should_normalize is False

    async def test_vote_result_contains_sigma(self, session_with_items: store.DataStore) -> None:
        """투표 결과에 sigma1/sigma2 필드가 포함됨"""
        item1 = session_with_items.items[0]
        item2 = session_with_items.items[1]
        votes = {criterion["key"]: "1" for criterion in session_with_items.criteria}
        token = await session_with_items.issue_battle_round(item1["id"], item2["id"])
        payload = BattleVoteRequest(
            item1_id=item1["id"],
            item2_id=item2["id"],
            round_token=token,
            votes=votes,
            redirect_to="/battle",
        )
        result, _ = await session_with_items.apply_battle_vote(payload)
        for r in result["results"]:
            assert "sigma1" in r
            assert "sigma2" in r
            assert r["sigma1"] > 0
            assert r["sigma2"] > 0
