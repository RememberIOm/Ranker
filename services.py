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

    Bayesian Beta prior로 실측 무승부 비율에 자연 수렴합니다.
    사전분포 강도 10(≈20배틀에서 실측과 동등)으로 부드러운 전환을 구현합니다.
    """
    s = store.settings
    # Bayesian Beta prior: elo_draw_max를 사전분포 중심으로 사용
    prior_strength = 10
    alpha = s["elo_draw_max"] * prior_strength + draws
    beta_param = (1.0 - s["elo_draw_max"]) * prior_strength + (battles - draws)
    draw_max = max(0.05, min(0.5, alpha / (alpha + beta_param)))

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
    """Elo Rating 업데이트 — K-Avg로 영합(zero-sum) 보장"""
    expected_a = calculate_expected_score(rating_a, rating_b)
    expected_b = calculate_expected_score(rating_b, rating_a)

    k_a = get_dynamic_k_factor(store, matches_a)
    k_b = get_dynamic_k_factor(store, matches_b)
    k_avg = (k_a + k_b) / 2.0

    new_a = rating_a + k_avg * (actual_score - expected_a)
    new_b = rating_b + k_avg * ((1.0 - actual_score) - expected_b)
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

    item1: sqrt(N) 적응형 샘플(최대 10) 중 matches_played가 적은 쪽 (탐험 촉진)
    item2: item1 제외 동일 샘플 크기 중 가중 복합 점수 차이가 작은 쪽 (공정 매칭)
    """
    items = store.items
    if len(items) < 2:
        return (store.get_item(focus_id) if focus_id else None), None

    sample_size = min(max(2, int(math.sqrt(len(items)))), 10)

    # item1: Two Choices — matches_played 적은 쪽 선택
    if focus_id:
        item1 = store.get_item(focus_id)
        if not item1:
            return None, None
    else:
        sample = random.sample(items, min(sample_size, len(items)))
        item1 = min(sample, key=lambda x: x["matches_played"])

    others = [i for i in items if i["id"] != item1["id"]]
    if not others:
        return item1, None

    # item2: Two Choices — 가중 복합 점수 차이 작은 쪽 선택
    sample2 = random.sample(others, min(sample_size, len(others)))

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
