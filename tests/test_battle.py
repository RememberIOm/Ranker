import pytest
from pydantic import ValidationError

from schemas import BattleVoteRequest, ThreeWayBattleVoteRequest
import store


class TestBattleVoteValidation:
    async def test_vote_model_rejects_same_item_payload(self, store_with_items: store.DataStore) -> None:
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

    async def test_apply_battle_vote_rejects_replayed_round(self, store_with_items: store.DataStore) -> None:
        item1 = store_with_items.items[0]
        item2 = store_with_items.items[1]
        votes = {criterion["key"]: "1" for criterion in store_with_items.criteria}
        round_token = await store_with_items.issue_battle_round(item1["id"], item2["id"])
        payload = BattleVoteRequest(
            item1_id=item1["id"],
            item2_id=item2["id"],
            round_token=round_token,
            votes=votes,
            redirect_to="/battle",
        )

        result, should_normalize = await store_with_items.apply_battle_vote(payload)
        assert result["a1_id"] == item1["id"]
        assert should_normalize is False

        with pytest.raises(store.StaleBattleRoundError):
            await store_with_items.apply_battle_vote(payload)

    async def test_apply_battle_vote_never_normalizes(self, store_with_items: store.DataStore) -> None:
        """Bayesian BT에서는 정규화가 불필요 — should_normalize 항상 False"""
        item1 = store_with_items.items[0]
        item2 = store_with_items.items[1]
        votes = {criterion["key"]: "1" for criterion in store_with_items.criteria}

        for _ in range(5):
            token = await store_with_items.issue_battle_round(item1["id"], item2["id"])
            payload = BattleVoteRequest(
                item1_id=item1["id"],
                item2_id=item2["id"],
                round_token=token,
                votes=votes,
                redirect_to="/battle",
            )
            _, should_normalize = await store_with_items.apply_battle_vote(payload)
            assert should_normalize is False

    async def test_vote_result_contains_sigma(self, store_with_items: store.DataStore) -> None:
        """투표 결과에 sigma1/sigma2 필드가 포함됨"""
        item1 = store_with_items.items[0]
        item2 = store_with_items.items[1]
        votes = {criterion["key"]: "1" for criterion in store_with_items.criteria}
        token = await store_with_items.issue_battle_round(item1["id"], item2["id"])
        payload = BattleVoteRequest(
            item1_id=item1["id"],
            item2_id=item2["id"],
            round_token=token,
            votes=votes,
            redirect_to="/battle",
        )
        result, _ = await store_with_items.apply_battle_vote(payload)
        for r in result["results"]:
            assert "sigma1" in r
            assert "sigma2" in r
            assert r["sigma1"] > 0
            assert r["sigma2"] > 0


# --- 3-way Tied Vote ---


class TestThreeWayTiedVote:
    async def test_best_only_tied_vote(self, store_with_three_items: store.DataStore) -> None:
        """3-way 'best only' 투표: best > tied_a, best > tied_b, tied_a ≈ tied_b"""
        s = store_with_three_items
        items = s.items
        token = await s.issue_battle_round(items[0]["id"], items[1]["id"], items[2]["id"])

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
        resp_data = await s.apply_three_way_vote(payload)

        # best(item1)는 레이팅 상승
        for r in resp_data["results"]:
            best_diff = r["diffs"][str(items[0]["id"])]
            assert best_diff > 0

        # draws 통계가 증가 (tied 쌍 = 무승부)
        for c in s.criteria:
            assert c["draws"] > 0

    async def test_worst_only_vote(self, store_with_three_items: store.DataStore) -> None:
        """3-way 'worst only' 투표: tied_a > worst, tied_b > worst, tied_a ≈ tied_b"""
        s = store_with_three_items
        items = s.items
        token = await s.issue_battle_round(items[0]["id"], items[1]["id"], items[2]["id"])

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
        resp_data = await s.apply_three_way_vote(payload)

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
        token = await s.issue_battle_round(items[0]["id"], items[1]["id"], items[2]["id"])

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
        resp_data = await s.apply_three_way_vote(payload)

        # 모든 레이팅 변화가 0에 가까움 (동일 레이팅 항목들의 대칭 무승부)
        for r in resp_data["results"]:
            for item in items:
                diff = abs(r["diffs"][str(item["id"])])
                assert diff < 0.1
            assert r["best_id"] is None
            assert r["worst_id"] is None
            assert r["middle_id"] is None

        # draws 통계: 기준당 3개 무승부 쌍
        for c in s.criteria:
            assert c.get("draws", 0) == 3

    async def test_invalid_role_combination(self, store_with_three_items: store.DataStore) -> None:
        """잘못된 역할 조합 (best 2개) → InvalidBattleVoteError"""
        s = store_with_three_items
        items = s.items
        token = await s.issue_battle_round(items[0]["id"], items[1]["id"], items[2]["id"])

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
            await s.apply_three_way_vote(payload)


# --- 3-way Mode B (Full Ranking) ---


class TestThreeWayModeBVote:
    async def test_full_ranking_vote(self, store_with_three_items: store.DataStore) -> None:
        """Mode B: best + worst만 지정 → best > middle > worst, 무승부 없음"""
        s = store_with_three_items
        items = s.items
        token = await s.issue_battle_round(items[0]["id"], items[1]["id"], items[2]["id"])

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
        resp_data = await s.apply_three_way_vote(payload)

        for r in resp_data["results"]:
            assert r["diffs"][str(items[0]["id"])] > 0   # best 상승
            assert r["diffs"][str(items[2]["id"])] < 0   # worst 하락
            assert r["best_id"] == items[0]["id"]
            assert r["worst_id"] == items[2]["id"]
            assert r["middle_id"] == items[1]["id"]

        # Mode B: 무승부 쌍 없음 (3개 쌍 모두 outcome=1.0)
        for c in s.criteria:
            assert c.get("draws", 0) == 0
            assert c["battles"] == 3
