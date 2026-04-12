# services.py
# Bayesian Bradley-Terry 레이팅 계산 및 매칭 로직 — 순수 함수 기반
# Online Laplace Approximation으로 항목별·기준별 (μ, σ²) 사후분포를 유지합니다.

import math
import random
from typing import Any

from store import DataStore


# --- Bayesian BT Core ---

_SIGMOID_CLAMP = 500.0
_SIGMA_SQ_FLOOR = 0.01


def sigmoid(x: float) -> float:
    """수치 안정 sigmoid: 1 / (1 + exp(-x))"""
    x = max(-_SIGMOID_CLAMP, min(_SIGMOID_CLAMP, x))
    return 1.0 / (1.0 + math.exp(-x))


def bt_update(
    mu_a: float,
    sigma_sq_a: float,
    mu_b: float,
    sigma_sq_b: float,
    outcome: float,
) -> tuple[float, float, float, float]:
    """Online Bayesian Bradley-Terry 업데이트 (Laplace Approximation).

    Args:
        outcome: 1.0=a승, 0.0=b승, 0.5=무승부

    Returns:
        (mu_a', sigma_sq_a', mu_b', sigma_sq_b')
    """
    p = sigmoid(mu_a - mu_b)
    w = p * (1.0 - p)  # Fisher information
    g = outcome - p     # gradient

    prec_a_new = 1.0 / sigma_sq_a + w
    prec_b_new = 1.0 / sigma_sq_b + w

    mu_a_new = (mu_a / sigma_sq_a + g) / prec_a_new
    mu_b_new = (mu_b / sigma_sq_b - g) / prec_b_new

    sigma_sq_a_new = max(_SIGMA_SQ_FLOOR, 1.0 / prec_a_new)
    sigma_sq_b_new = max(_SIGMA_SQ_FLOOR, 1.0 / prec_b_new)

    return mu_a_new, sigma_sq_a_new, mu_b_new, sigma_sq_b_new


def hierarchical_shrinkage(store: DataStore, item: dict[str, Any]) -> None:
    """계층적 축소: 기준 간 정보를 공유하여 데이터 부족 기준을 보강합니다.

    모든 기준의 정밀도 가중 평균(cross_mean)을 계산하고,
    각 기준의 μ를 cross_mean 방향으로 축소합니다 (in-place).
    """
    strength = store.settings["hierarchical_strength"]
    if strength <= 0:
        return

    criteria = store.criteria
    if len(criteria) < 2:
        return

    precisions: dict[str, float] = {}
    mus: dict[str, float] = {}
    for c in criteria:
        k = c["key"]
        sq = item["sigma_sq"].get(k, store.settings["initial_sigma"] ** 2)
        precisions[k] = 1.0 / sq
        mus[k] = item["mu"].get(k, 0.0)

    total_prec = sum(precisions.values())
    if total_prec <= 0:
        return

    cross_mean = sum(mus[k] * precisions[k] for k in precisions) / total_prec

    for c in criteria:
        k = c["key"]
        old_prec = precisions[k]
        new_prec = old_prec + strength
        item["mu"][k] = (mus[k] * old_prec + cross_mean * strength) / new_prec
        item["sigma_sq"][k] = max(_SIGMA_SQ_FLOOR, 1.0 / new_prec)


# --- Display Conversion ---


def display_rating(store: DataStore, mu: float) -> float:
    """logit 스케일 μ를 친숙한 표시 점수로 변환합니다."""
    s = store.settings
    return mu * s["display_scale"] + s["display_center"]


def display_uncertainty(store: DataStore, sigma_sq: float) -> float:
    """logit 스케일 σ²를 표시 스케일 불확실성으로 변환합니다."""
    return math.sqrt(sigma_sq) * store.settings["display_scale"]


# --- Match Probabilities ---


def get_match_probabilities(
    store: DataStore,
    mu_a: float,
    sigma_sq_a: float,
    mu_b: float,
    sigma_sq_b: float,
    battles: int = 0,
    draws: int = 0,
) -> dict[str, float]:
    """UI 표시용 승/무/패 확률 계산.

    Bayesian Beta prior로 실측 무승부 비율에 자연 수렴합니다.
    """
    s = store.settings

    # Bayesian Beta prior
    alpha = s["draw_prior_max"] * s["draw_prior_strength"] + draws
    beta_param = (1.0 - s["draw_prior_max"]) * s["draw_prior_strength"] + (battles - draws)
    draw_max = max(0.05, min(0.5, alpha / (alpha + beta_param)))

    # BT 승률 (logit 스케일 직접 사용)
    p_a = sigmoid(mu_a - mu_b)
    delta = abs(mu_a - mu_b)

    # 무승부 확률 — logit 스케일 차이 기반 가우시안 감쇠
    p_draw = draw_max * math.exp(-((delta / s["draw_bandwidth"]) ** 2))

    p_win_a = max(0.0, p_a - 0.5 * p_draw)
    p_win_b = max(0.0, (1.0 - p_a) - 0.5 * p_draw)

    total = p_win_a + p_draw + p_win_b
    if total == 0:
        return {"win_a": 0.0, "draw": 100.0, "win_b": 0.0}

    return {
        "win_a": round((p_win_a / total) * 100, 1),
        "draw": round((p_draw / total) * 100, 1),
        "win_b": round((p_win_b / total) * 100, 1),
    }


# --- Composite Rating ---


def composite_rating(store: DataStore, item: dict[str, Any]) -> float:
    """가중 복합 점수를 계산합니다. 매치메이킹과 랭킹에서 공통 사용."""
    criteria = store.criteria
    total_weight = sum(c["weight"] for c in criteria) or 1.0
    return sum(
        display_rating(store, item["mu"].get(c["key"], 0.0)) * c["weight"]
        for c in criteria
    ) / total_weight


# --- Matchmaking ---


def get_match_pair(
    store: DataStore,
    focus_id: int | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """대결 상대를 선정합니다 (불확실성 기반 Power of Two Choices).

    item1: sqrt(N) 적응형 샘플(최대 10) 중 평균 σ² 최대 (정보 획득 극대화)
    item2: item1 제외 동일 샘플 크기 중 가중 복합 점수 차이가 작은 쪽 (공정 매칭)
    """
    items = store.items
    if len(items) < 2:
        return (store.get_item(focus_id) if focus_id else None), None

    sample_size = min(max(2, int(math.sqrt(len(items)))), 10)
    criteria_keys = [c["key"] for c in store.criteria]
    initial_sq = store.settings["initial_sigma"] ** 2

    def _avg_sigma_sq(item: dict[str, Any]) -> float:
        if not criteria_keys:
            return 0.0
        return sum(item["sigma_sq"].get(k, initial_sq) for k in criteria_keys) / len(criteria_keys)

    # item1: Two Choices — 평균 σ² 최대 선택 (가장 불확실한 항목 우선)
    if focus_id:
        item1 = store.get_item(focus_id)
        if not item1:
            return None, None
    else:
        sample = random.sample(items, min(sample_size, len(items)))
        item1 = max(sample, key=_avg_sigma_sq)

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
