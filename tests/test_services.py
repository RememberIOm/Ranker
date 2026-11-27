"""표시 점수의 가중 평균과 공동 순위."""

import pytest

from ranker import store
from ranker.services import (
    composite_rating,
    display_rating,
    get_item_ranks,
    rank_uncertainty,
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


class TestRankUncertainty:
    async def test_adjacent_confidence_follows_map_order_not_sample_mean(
        self, temp_store: store.DataStore
    ) -> None:
        """표시 순서와 다른 표본 평균 순서로 인접 확률을 구하면 상향 편향이 생깁니다."""
        import numpy as np

        await temp_store.set_criteria(
            [{"key": "a", "label": "A", "color": "blue", "weight": 1.0}]
        )
        await temp_store.add_items(["Alpha", "Beta"])
        temp_store.items[0]["mu"]["a"] = 0.0
        temp_store.items[1]["mu"]["a"] = 0.001
        # seed 0의 처음 5개 표본에서는 첫 번째 항목(Alpha)의 표본 평균이 더 크지만,
        # 인접 순위 신뢰도는 표시 순서(Beta -> Alpha) 기준으로 계산해야 합니다.
        draws = temp_store.posterior("a").sample([1, 2], 5, np.random.default_rng(0))
        assert draws[:, 0].mean() > draws[:, 1].mean()
        result = rank_uncertainty(temp_store, "total", samples=5)
        assert result is not None
        assert result["adjacent_confidence"] == pytest.approx(
            float((draws[:, 1] > draws[:, 0]).mean())
        )
