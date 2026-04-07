import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from schemas import BattleVoteRequest
import store


class BattleVoteValidationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_session_dir = store.SESSION_DIR
        self.original_normalize_every_n_votes = store.NORMALIZE_EVERY_N_VOTES
        store.SESSION_DIR = Path(self.tempdir.name)
        store.SESSION_DIR.mkdir(parents=True, exist_ok=True)
        store._session_cache.clear()
        store._locks.clear()

    async def asyncTearDown(self) -> None:
        store._session_cache.clear()
        store._locks.clear()
        store.NORMALIZE_EVERY_N_VOTES = self.original_normalize_every_n_votes
        store.SESSION_DIR = self.original_session_dir
        self.tempdir.cleanup()

    async def _seed_session(self, session_id: str) -> store.DataStore:
        session = await store.get_store(session_id)
        await session.add_item("Alpha")
        await session.add_item("Beta")
        return session

    async def test_vote_model_rejects_same_item_payload(self) -> None:
        session = await self._seed_session("c" * 32)
        item = session.items[0]
        votes = {criterion["key"]: "1" for criterion in session.criteria}

        with self.assertRaises(ValidationError):
            BattleVoteRequest(
                item1_id=item["id"],
                item2_id=item["id"],
                round_token="x" * 24,
                votes=votes,
                redirect_to="/battle",
            )

    async def test_apply_battle_vote_rejects_replayed_round(self) -> None:
        session = await self._seed_session("d" * 32)
        item1 = session.items[0]
        item2 = session.items[1]
        votes = {criterion["key"]: "1" for criterion in session.criteria}
        round_token = await session.issue_battle_round(item1["id"], item2["id"])
        payload = BattleVoteRequest(
            item1_id=item1["id"],
            item2_id=item2["id"],
            round_token=round_token,
            votes=votes,
            redirect_to="/battle",
        )

        result, should_normalize = await session.apply_battle_vote(payload)
        self.assertEqual(result["a1_id"], item1["id"])
        self.assertFalse(should_normalize)

        with self.assertRaises(store.StaleBattleRoundError):
            await session.apply_battle_vote(payload)

    async def test_apply_battle_vote_batches_normalization(self) -> None:
        store.NORMALIZE_EVERY_N_VOTES = 2
        session = await self._seed_session("e" * 32)
        item1 = session.items[0]
        item2 = session.items[1]
        votes = {criterion["key"]: "1" for criterion in session.criteria}

        first_token = await session.issue_battle_round(item1["id"], item2["id"])
        first_payload = BattleVoteRequest(
            item1_id=item1["id"],
            item2_id=item2["id"],
            round_token=first_token,
            votes=votes,
            redirect_to="/battle",
        )
        _, first_should_normalize = await session.apply_battle_vote(first_payload)
        self.assertFalse(first_should_normalize)

        second_token = await session.issue_battle_round(item1["id"], item2["id"])
        second_payload = BattleVoteRequest(
            item1_id=item1["id"],
            item2_id=item2["id"],
            round_token=second_token,
            votes=votes,
            redirect_to="/battle",
        )
        _, second_should_normalize = await session.apply_battle_vote(second_payload)
        self.assertTrue(second_should_normalize)
