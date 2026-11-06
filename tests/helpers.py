"""Shared test helpers: submit a ballot through the store and compare refits."""

from collections.abc import Collection, Sequence
from typing import Any

import pytest

from ranker.schemas import BattleVoteRequest, ThreeWayBattleVoteRequest
from ranker.store import DataStore


async def vote(
    store: DataStore,
    ids: Sequence[int] = (1, 2),
    choice: Any = "1",
    *,
    skip: Collection[str] = (),
) -> dict[str, Any]:
    """Issue a round for ids and answer every criterion with choice, except skip."""
    token = await store.issue_battle_round(list(ids))
    votes = {c["key"]: "skip" if c["key"] in skip else choice for c in store.criteria}
    fields = dict(zip(("item1_id", "item2_id", "item3_id"), ids, strict=False))
    model = ThreeWayBattleVoteRequest if len(ids) == 3 else BattleVoteRequest
    return await store.apply_vote(model(**fields, round_token=token, votes=votes))


def approx_items(items: list[dict]) -> list[dict]:
    """Refits warm-started from different fits agree to rounding, not bit for bit."""
    return [
        {
            **item,
            "mu": pytest.approx(item["mu"], rel=1e-9, abs=1e-12),
            "sigma_sq": pytest.approx(item["sigma_sq"], rel=1e-9),
        }
        for item in items
    ]
