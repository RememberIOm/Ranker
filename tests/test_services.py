"""표시 점수의 가중 평균과 공동 순위."""

import pytest

from ranker import store
from ranker.services import (
    composite_rating,
    display_rating,
    get_item_ranks,
)


class TestCompositeRating:
    async def test_custom_weights(self, temp_store: store.DataStore) -> None:
        """종합 점수는 기준별 표시 점수의 가중 평균입니다."""
        await temp_store.set_criteria(
            [
                {"key": "a", "label": "A", "color": "blue", "weight": 2.0},
                {"key": "b", "label": "B", "color": "red", "weight": 1.0},
            ]
        )
        await temp_store.add_items(["Alpha"])
        item = temp_store.items[0]
        item["mu"]["a"] = 1.0
        item["mu"]["b"] = 0.0

        # 가중 평균: (display(1.0)*2 + display(0.0)*1) / 3
        expected = (
            display_rating(temp_store, 1.0) * 2 + display_rating(temp_store, 0.0) * 1
        ) / 3
        assert composite_rating(temp_store, item) == pytest.approx(expected)

    async def test_empty_criteria_returns_display_center(
        self, temp_store: store.DataStore
    ) -> None:
        """기준을 모두 지운 랭킹도 0점이 아니라 중심점을 보여줍니다."""
        await temp_store.add_items(["Alpha"])
        item = temp_store.items[0]
        temp_store._data["criteria"] = []
        assert composite_rating(temp_store, item) == pytest.approx(
            temp_store.settings["display_center"]
        )


class TestGetItemRank:
    async def test_exact_ties_share_competition_rank(
        self, temp_store: store.DataStore
    ) -> None:
        for name in ("A", "B", "C"):
            await temp_store.add_items([name])
        for key in temp_store.items[2]["mu"]:
            temp_store.items[2]["mu"][key] = -1.0
        ranks, total = get_item_ranks(temp_store)
        assert list(ranks.values()) == [1, 1, 3]
        assert total == 3
