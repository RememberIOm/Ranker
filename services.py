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
    store: DataStore, rating_a: float, rating_b: float
) -> dict[str, float]:
    """UI 표시용 승/무/패 확률 계산"""
    s = store.settings
    expected_a = calculate_expected_score(rating_a, rating_b)
    delta = abs(rating_a - rating_b)

    p_draw = s["elo_draw_max"] * math.exp(-((delta / s["elo_draw_scale"]) ** 2))
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


# --- Matchmaking ---


def get_match_pair(
    store: DataStore,
    focus_id: int | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """대결 상대를 선정합니다 (Smart Match + Random)."""
    items = store.items
    if len(items) < 2:
        return (store.get_item(focus_id) if focus_id else None), None

    if focus_id:
        item1 = store.get_item(focus_id)
        if not item1:
            return None, None
    else:
        item1 = random.choice(items)

    others = [i for i in items if i["id"] != item1["id"]]
    if not others:
        return item1, None

    s = store.settings
    criteria_keys = [c["key"] for c in store.criteria]
    ref_key = criteria_keys[0] if criteria_keys else None

    item2 = None
    if ref_key and random.random() < s["match_smart_rate"]:
        target = item1["ratings"].get(ref_key, s["initial_rating"])
        r = s["match_score_range"]
        candidates = [
            i for i in others
            if abs(i["ratings"].get(ref_key, s["initial_rating"]) - target) <= r
        ]
        if candidates:
            item2 = random.choice(candidates)

    if not item2:
        item2 = random.choice(others)

    return item1, item2


# --- Rank Calculation ---


def get_item_rank_info(
    store: DataStore, category_key: str, score: float
) -> dict[str, Any]:
    """특정 카테고리 점수 기준으로 등수/상위 퍼센트를 계산합니다."""
    items = store.items
    total = len(items) or 1
    higher = sum(
        1 for i in items if i["ratings"].get(category_key, 0) > score
    )
    rank = higher + 1
    return {
        "rank": rank,
        "total": total,
        "top_percent": round((rank / total) * 100, 1),
    }


# --- Score Normalization ---


async def normalize_scores(store: DataStore) -> None:
    """점수 인플레이션 방지 (Mean Reversion) — 백그라운드 태스크"""
    s = store.settings
    items = store.items
    if not items:
        return

    target = s["normalize_target"]
    threshold = s["normalize_threshold"]
    criteria_keys = [c["key"] for c in store.criteria]
    modified = False

    for key in criteria_keys:
        values = [i["ratings"].get(key, target) for i in items]
        avg = sum(values) / len(values)
        diff = avg - target

        if abs(diff) > threshold:
            modified = True
            for item in items:
                item["ratings"][key] = item["ratings"].get(key, target) - diff

    if modified:
        await store.save()
