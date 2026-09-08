import pytest
from ranker import store
from ranker.services import (
    display_rating,
    display_uncertainty,
    composite_rating,
    get_item_rank,
    get_item_ranks,
)


class TestDisplayConversion:
    async def test_mu_zero_gives_center(self, temp_store: store.DataStore) -> None:
        assert display_rating(temp_store, 0.0) == pytest.approx(
            temp_store.settings["display_center"]
        )

    async def test_uncertainty_positive(self, temp_store: store.DataStore) -> None:
        u = display_uncertainty(temp_store, 4.0)
        assert u > 0.0
        assert u == pytest.approx(2.0 * temp_store.settings["display_scale"])


class TestCompositeRating:
    async def test_uniform_weights_equals_mean(
        self, temp_store: store.DataStore
    ) -> None:
        """모든 weight=1.0일 때 display_rating 평균과 일치"""
        # 모든 기준 weight를 1.0으로 통일
        for c in temp_store.criteria:
            c["weight"] = 1.0
        await temp_store.add_item("Alpha")
        item = temp_store.items[0]
        # 기준별 다른 mu 설정
        keys = [c["key"] for c in temp_store.criteria]
        for i, k in enumerate(keys):
            item["mu"][k] = float(i) * 0.5

        expected = sum(display_rating(temp_store, item["mu"][k]) for k in keys) / len(
            keys
        )
        assert composite_rating(temp_store, item) == pytest.approx(expected)

    async def test_custom_weights(self, temp_store: store.DataStore) -> None:
        """비균일 weight에서 가중 평균 정확성 검증"""
        # 기준 2개만 사용, 나머지 weight=0 대신 아주 작은 값
        await temp_store.set_criteria(
            [
                {"key": "a", "label": "A", "color": "blue", "weight": 2.0},
                {"key": "b", "label": "B", "color": "red", "weight": 1.0},
            ]
        )
        await temp_store.add_item("Alpha")
        item = temp_store.items[0]
        item["mu"]["a"] = 1.0
        item["mu"]["b"] = 0.0

        # 가중 평균: (display(1.0)*2 + display(0.0)*1) / 3
        expected = (
            display_rating(temp_store, 1.0) * 2 + display_rating(temp_store, 0.0) * 1
        ) / 3
        assert composite_rating(temp_store, item) == pytest.approx(expected)

    async def test_zero_mu_gives_center(self, temp_store: store.DataStore) -> None:
        """모든 mu=0.0 → display_center 반환"""
        await temp_store.add_item("Alpha")
        item = temp_store.items[0]
        assert composite_rating(temp_store, item) == pytest.approx(
            temp_store.settings["display_center"]
        )

    async def test_empty_criteria_returns_display_center(
        self, temp_store: store.DataStore
    ) -> None:
        """criteria가 비어 있어도 0이 아닌 display_center를 반환 (회귀 보호)

        예전 `or 1.0` fallback은 빈 criteria에서 0을 반환해 매치메이킹·랭킹·확률
        계산이 모두 0점으로 표시되는 사용자 영향이 있었음.
        """
        await temp_store.add_item("Alpha")
        item = temp_store.items[0]
        # criteria를 강제로 비움 (정상 경로에선 발생하지 않지만 일시 상태 방어)
        temp_store._data["criteria"] = []
        assert composite_rating(temp_store, item) == pytest.approx(
            temp_store.settings["display_center"]
        )


class TestGetItemRank:
    async def test_single_item_rank_one(self, temp_store: store.DataStore) -> None:
        await temp_store.add_item("Alpha")
        rank, total = get_item_rank(temp_store, temp_store.items[0]["id"])
        assert rank == 1
        assert total == 1

    async def test_rank_ordering(self, temp_store: store.DataStore) -> None:
        """mu가 높은 항목이 더 높은 순위"""
        await temp_store.add_item("Low")
        await temp_store.add_item("High")
        # High에 높은 mu 설정
        for c in temp_store.criteria:
            temp_store.items[1]["mu"][c["key"]] = 2.0
        rank_high, _ = get_item_rank(temp_store, temp_store.items[1]["id"])
        rank_low, _ = get_item_rank(temp_store, temp_store.items[0]["id"])
        assert rank_high < rank_low  # 낮은 rank = 높은 순위

    async def test_missing_item_returns_last(self, temp_store: store.DataStore) -> None:
        """존재하지 않는 ID → (total, total)"""
        await temp_store.add_item("Alpha")
        await temp_store.add_item("Beta")
        rank, total = get_item_rank(temp_store, 9999)
        assert rank == total == 2

    async def test_exact_ties_share_competition_rank(
        self, temp_store: store.DataStore
    ) -> None:
        for name in ("A", "B", "C"):
            await temp_store.add_item(name)
        for key in temp_store.items[2]["mu"]:
            temp_store.items[2]["mu"][key] = -1.0
        ranks, total = get_item_ranks(temp_store)
        assert list(ranks.values()) == [1, 1, 3]
        assert total == 3
