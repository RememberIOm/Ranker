# services.py
# Elo 레이팅 계산 및 매칭 로직 — 순수 함수 기반
# 모든 함수가 DataStore 인스턴스를 매개변수로 받아 멀티세션을 지원합니다.

import math
import random
from typing import Any

from store import DataStore


# --- Elo Calculation Logic ---


def get_dynamic_k_factor(store: DataStore, matches_played: int) -> float:
    """
    매치 횟수에 따라 K-Factor를 동적으로 계산합니다 (Logistic Decay).
    초반에 높은 변동폭(배치고사)을 주고 점차 안정화합니다.
    """
    s = store.settings
    k_diff = s["elo_k_max"] - s["elo_k_min"]
    decay = math.exp(-matches_played / s["elo_decay_factor"])
    return s["elo_k_min"] + k_diff * decay


def calculate_expected_score(rating_a: float, rating_b: float) -> float:
    """표준 Elo 기대 승률: E_a = 1 / (1 + 10^((Rb - Ra) / 400))"""
    return 1.0 / (1.0 + 10.0 ** ((rating_b - rating_a) / 400.0))


def get_match_probabilities(
    store: DataStore,
    rating_a: float,
    rating_b: float,
    battles: int = 0,
    draws: int = 0,
) -> dict[str, float]:
    """UI 표시용 승/무/패 확률 계산.

    battles >= 20이면 해당 기준의 실제 무승부 비율로 draw_max를 보정합니다.
    데이터가 부족하면 settings의 기본값(elo_draw_max)을 사용합니다.
    """
    s = store.settings
    if battles >= 20:
        # 실측 무승부 비율 기반 보정 (0.05 ~ 0.5 범위로 클램핑)
        draw_max = max(0.05, min(0.5, draws / battles))
    else:
        draw_max = s["elo_draw_max"]

    expected_a = calculate_expected_score(rating_a, rating_b)
    delta = abs(rating_a - rating_b)

    p_draw = draw_max * math.exp(-((delta / s["elo_draw_scale"]) ** 2))
    p_win_a = max(0.0, expected_a - 0.5 * p_draw)
    p_win_b = max(0.0, (1.0 - expected_a) - 0.5 * p_draw)

    total = p_win_a + p_draw + p_win_b
    if total == 0:
        return {"win_a": 0.0, "draw": 100.0, "win_b": 0.0}

    return {
        "win_a": round((p_win_a / total) * 100, 1),
        "draw": round((p_draw / total) * 100, 1),
        "win_b": round((p_win_b / total) * 100, 1),
    }


def calculate_elo_update(
    store: DataStore,
    rating_a: float,
    rating_b: float,
    actual_score: float,  # 1.0=Win, 0.5=Draw, 0.0=Lose
    matches_a: int,
    matches_b: int,
) -> tuple[float, float]:
    """Elo Rating 업데이트 — 개별 K-Factor 적용"""
    expected_a = calculate_expected_score(rating_a, rating_b)
    expected_b = calculate_expected_score(rating_b, rating_a)

    k_a = get_dynamic_k_factor(store, matches_a)
    k_b = get_dynamic_k_factor(store, matches_b)

    new_a = rating_a + k_a * (actual_score - expected_a)
    new_b = rating_b + k_b * ((1.0 - actual_score) - expected_b)
    return new_a, new_b


# --- Composite Rating ---


def composite_rating(store: DataStore, item: dict[str, Any]) -> float:
    """가중 복합 점수를 계산합니다. 매치메이킹과 랭킹에서 공통 사용."""
    criteria = store.criteria
    total_weight = sum(c["weight"] for c in criteria) or 1.0
    initial = store.settings["initial_rating"]
    return sum(item["ratings"].get(c["key"], initial) * c["weight"] for c in criteria) / total_weight


# --- Matchmaking ---


def get_match_pair(
    store: DataStore,
    focus_id: int | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """대결 상대를 선정합니다 (Power of Two Choices).

    item1: 2개 무작위 샘플 중 matches_played가 적은 쪽 (탐험 촉진)
    item2: item1 제외 2개 무작위 샘플 중 가중 복합 점수 차이가 작은 쪽 (공정 매칭)
    """
    items = store.items
    if len(items) < 2:
        return (store.get_item(focus_id) if focus_id else None), None

    # item1: Two Choices — matches_played 적은 쪽 선택
    if focus_id:
        item1 = store.get_item(focus_id)
        if not item1:
            return None, None
    else:
        sample = random.sample(items, min(2, len(items)))
        item1 = min(sample, key=lambda x: x["matches_played"])

    others = [i for i in items if i["id"] != item1["id"]]
    if not others:
        return item1, None

    # item2: Two Choices — 가중 복합 점수 차이 작은 쪽 선택
    sample2 = random.sample(others, min(2, len(others)))

    if store.criteria:
        r1 = composite_rating(store, item1)
        item2 = min(sample2, key=lambda x: abs(composite_rating(store, x) - r1))
    else:
        item2 = sample2[0]

    return item1, item2


# --- Ranking ---


def get_item_rank(store: DataStore, item_id: int) -> tuple[int, int]:
    """가중 합산 점수 기준으로 item_id의 순위를 반환합니다.

    Returns:
        (rank, total): rank=1이 최고, total은 전체 항목 수.
        항목이 없으면 (0, 0) 반환.
    """
    items = store.items
    if not items:
        return 0, 0

    scores: list[tuple[float, int]] = [
        (composite_rating(store, item), item["id"]) for item in items
    ]
    scores.sort(key=lambda x: x[0], reverse=True)
    total = len(scores)
    for i, (_, iid) in enumerate(scores):
        if iid == item_id:
            return i + 1, total
    return total, total


# --- Score Normalization ---


async def normalize_scores(store: DataStore) -> None:
    """점수 인플레이션 방지 (Mean Reversion) — 백그라운드 태스크"""
    await store.normalize_scores()
