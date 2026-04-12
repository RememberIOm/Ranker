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
        store.SESSION_DIR = Path(self.tempdir.name)
        store.SESSION_DIR.mkdir(parents=True, exist_ok=True)
        store._session_cache.clear()
        store._locks.clear()

    async def asyncTearDown(self) -> None:
        store._session_cache.clear()
        store._locks.clear()
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

    async def test_apply_battle_vote_never_normalizes(self) -> None:
        """Bayesian BT에서는 정규화가 불필요 — should_normalize 항상 False"""
        session = await self._seed_session("e" * 32)
        item1 = session.items[0]
        item2 = session.items[1]
        votes = {criterion["key"]: "1" for criterion in session.criteria}

        for _ in range(5):
            token = await session.issue_battle_round(item1["id"], item2["id"])
            payload = BattleVoteRequest(
                item1_id=item1["id"],
                item2_id=item2["id"],
                round_token=token,
                votes=votes,
                redirect_to="/battle",
            )
            _, should_normalize = await session.apply_battle_vote(payload)
            self.assertFalse(should_normalize)

    async def test_vote_result_contains_sigma(self) -> None:
        """투표 결과에 sigma1/sigma2 필드가 포함됨"""
        session = await self._seed_session("f" * 32)
        item1 = session.items[0]
        item2 = session.items[1]
        votes = {criterion["key"]: "1" for criterion in session.criteria}
        token = await session.issue_battle_round(item1["id"], item2["id"])
        payload = BattleVoteRequest(
            item1_id=item1["id"],
            item2_id=item2["id"],
            round_token=token,
            votes=votes,
            redirect_to="/battle",
        )
        result, _ = await session.apply_battle_vote(payload)
        for r in result["results"]:
            self.assertIn("sigma1", r)
            self.assertIn("sigma2", r)
            self.assertGreater(r["sigma1"], 0)
            self.assertGreater(r["sigma2"], 0)
