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
    g = outcome - p  # gradient

    prec_a_new = 1.0 / sigma_sq_a + w
    prec_b_new = 1.0 / sigma_sq_b + w

    mu_a_new = mu_a + g / prec_a_new
    mu_b_new = mu_b - g / prec_b_new

    sigma_sq_a_new = max(_SIGMA_SQ_FLOOR, 1.0 / prec_a_new)
    sigma_sq_b_new = max(_SIGMA_SQ_FLOOR, 1.0 / prec_b_new)

    return mu_a_new, sigma_sq_a_new, mu_b_new, sigma_sq_b_new


def hierarchical_shrinkage(store: DataStore, item: dict[str, Any]) -> None:
    """계층적 축소: 기준 간 정보를 공유하여 데이터 부족 기준을 보강합니다.

    각 기준 k의 μ를 나머지 기준들의 정밀도 가중 평균(Leave-One-Out cross_mean)
    방향으로 축소합니다 (in-place). LOO 방식으로 자기 자신이 축소 대상에
    포함되는 자기 강화 편향을 제거합니다.
    축소 강도는 기준별 관측 수에 반비례하여 적응 — 데이터 풍부 기준은 덜 축소됩니다.
    """
    base_strength = store.settings["hierarchical_strength"]
    if base_strength <= 0:
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

    weighted_sum = sum(mus[k] * precisions[k] for k in precisions)

    criterion_matches = item.get("criterion_matches", {})
    for c in criteria:
        k = c["key"]
        old_prec = precisions[k]
        # Leave-One-Out: 기준 k를 제외한 나머지의 정밀도 가중 평균
        loo_prec = total_prec - old_prec
        if loo_prec <= 0:
            continue
        loo_mean = (weighted_sum - mus[k] * old_prec) / loo_prec
        # 적응형 강도: 관측 수가 많을수록 축소 감소
        effective_strength = base_strength / (1.0 + criterion_matches.get(k, 0))
        new_prec = old_prec + effective_strength
        item["mu"][k] = (mus[k] * old_prec + loo_mean * effective_strength) / new_prec


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
    beta_param = (1.0 - s["draw_prior_max"]) * s["draw_prior_strength"] + (
        battles - draws
    )
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
    """가중 복합 점수를 계산합니다. 매치메이킹과 랭킹에서 공통 사용.

    criteria가 비어 있거나 weight 합이 0 이하인 비정상 상태에서는
    `display_center` (μ=0의 표시값)을 반환해 fallback이 0점으로 보이지 않게 합니다.
    """
    criteria = store.criteria
    if not criteria:
        return float(store.settings["display_center"])

    total_weight = sum(c["weight"] for c in criteria)
    if total_weight <= 0:
        return float(store.settings["display_center"])

    return (
        sum(
            display_rating(store, item["mu"].get(c["key"], 0.0)) * c["weight"]
            for c in criteria
        )
        / total_weight
    )


# --- Matchmaking ---

_EIG_SAMPLE_THRESHOLD = 500
_TRIPLE_EXHAUSTIVE_THRESHOLD = 80


def _pair_eig(
    a: dict[str, Any],
    b: dict[str, Any],
    criteria_keys: list[str],
    initial_sq: float,
) -> float:
    """두 항목 간 기대 정보 이득(Expected Information Gain)을 합산합니다.

    EIG(i,j) = Σ_k 0.5·log(1 + w·σ²_a) + 0.5·log(1 + w·σ²_b)
    여기서 w = p(1-p), p = sigmoid(μ_a - μ_b).
    """
    total = 0.0
    for k in criteria_keys:
        mu_a = a["mu"].get(k, 0.0)
        sq_a = a["sigma_sq"].get(k, initial_sq)
        mu_b = b["mu"].get(k, 0.0)
        sq_b = b["sigma_sq"].get(k, initial_sq)
        p = sigmoid(mu_a - mu_b)
        w = p * (1.0 - p)
        total += 0.5 * math.log1p(w * sq_a) + 0.5 * math.log1p(w * sq_b)
    return total


def get_match_pair(
    store: DataStore,
    focus_id: int | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """대결 상대를 선정합니다 (Expected Information Gain 최대화).

    모든 가능한 쌍의 EIG를 계산하여 정보 획득이 최대인 쌍을 반환합니다.
    n > _EIG_SAMPLE_THRESHOLD일 때는 랜덤 샘플링으로 후보를 축소합니다.
    """
    items = store.items
    if len(items) < 2:
        return (store.get_item(focus_id) if focus_id else None), None

    criteria_keys = [c["key"] for c in store.criteria]
    initial_sq = store.settings["initial_sigma"] ** 2

    if focus_id:
        item1 = store.get_item(focus_id)
        if not item1:
            return None, None
        candidates = [i for i in items if i["id"] != item1["id"]]
        item2 = max(
            candidates, key=lambda x: _pair_eig(item1, x, criteria_keys, initial_sq)
        )
        return item1, item2

    # n이 클 때는 샘플링으로 후보 축소
    pool = items
    if len(items) > _EIG_SAMPLE_THRESHOLD:
        pool = random.sample(items, _EIG_SAMPLE_THRESHOLD)

    best_eig = -1.0
    best_pair: tuple[dict[str, Any] | None, dict[str, Any] | None] = (None, None)
    for i in range(len(pool)):
        for j in range(i + 1, len(pool)):
            eig = _pair_eig(pool[i], pool[j], criteria_keys, initial_sq)
            if eig > best_eig:
                best_eig = eig
                best_pair = (pool[i], pool[j])

    return best_pair


def _triple_eig(
    a: dict[str, Any],
    b: dict[str, Any],
    c: dict[str, Any],
    criteria_keys: list[str],
    initial_sq: float,
) -> float:
    """삼중항의 총 EIG = 3개 쌍 EIG 합."""
    return (
        _pair_eig(a, b, criteria_keys, initial_sq)
        + _pair_eig(a, c, criteria_keys, initial_sq)
        + _pair_eig(b, c, criteria_keys, initial_sq)
    )


def get_match_triple(
    store: DataStore,
    focus_id: int | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]:
    """3-way 비교를 위한 3개 항목을 선정합니다 (EIG 기반).

    소규모 풀(n ≤ _TRIPLE_EXHAUSTIVE_THRESHOLD): O(n³) 완전 탐색으로 전역 최적 삼중항 선택.
    대규모 풀: top-K 쌍 기반 탐욕적 선택으로 근사.
    """
    items = store.items
    if len(items) < 3:
        return None, None, None

    criteria_keys = [c["key"] for c in store.criteria]
    initial_sq = store.settings["initial_sigma"] ** 2

    # focus 모드: focus 항목 고정 + 나머지에서 최적 쌍 탐색
    if focus_id:
        item1 = store.get_item(focus_id)
        if not item1:
            return None, None, None
        others = [i for i in items if i["id"] != item1["id"]]
        if len(others) < 2:
            return None, None, None
        if len(others) > _EIG_SAMPLE_THRESHOLD:
            others = random.sample(others, _EIG_SAMPLE_THRESHOLD)
        best_eig = -1.0
        best_pair = (others[0], others[1])
        for i in range(len(others)):
            for j in range(i + 1, len(others)):
                eig = _triple_eig(
                    item1, others[i], others[j], criteria_keys, initial_sq
                )
                if eig > best_eig:
                    best_eig = eig
                    best_pair = (others[i], others[j])
        return item1, best_pair[0], best_pair[1]

    # 소규모 풀: O(n³) 완전 탐색
    pool = items
    if len(items) > _EIG_SAMPLE_THRESHOLD:
        pool = random.sample(items, _EIG_SAMPLE_THRESHOLD)

    if len(pool) <= _TRIPLE_EXHAUSTIVE_THRESHOLD:
        best_eig = -1.0
        best_triple: tuple[dict[str, Any], ...] = (pool[0], pool[1], pool[2])
        for i in range(len(pool)):
            for j in range(i + 1, len(pool)):
                for k in range(j + 1, len(pool)):
                    eig = _triple_eig(
                        pool[i], pool[j], pool[k], criteria_keys, initial_sq
                    )
                    if eig > best_eig:
                        best_eig = eig
                        best_triple = (pool[i], pool[j], pool[k])
        return best_triple[0], best_triple[1], best_triple[2]

    # 대규모 풀: top-K 쌍 기반 탐욕적 선택
    _TOP_K = 10
    top_pairs: list[tuple[float, dict[str, Any], dict[str, Any]]] = []
    for i in range(len(pool)):
        for j in range(i + 1, len(pool)):
            eig = _pair_eig(pool[i], pool[j], criteria_keys, initial_sq)
            if len(top_pairs) < _TOP_K:
                top_pairs.append((eig, pool[i], pool[j]))
                top_pairs.sort(key=lambda x: x[0])
            elif eig > top_pairs[0][0]:
                top_pairs[0] = (eig, pool[i], pool[j])
                top_pairs.sort(key=lambda x: x[0])

    best_eig = -1.0
    best_result: tuple[
        dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None
    ] = (None, None, None)
    for _, p1, p2 in top_pairs:
        others = [i for i in pool if i["id"] not in (p1["id"], p2["id"])]
        if not others:
            continue
        p3 = max(
            others, key=lambda x: _triple_eig(p1, p2, x, criteria_keys, initial_sq)
        )
        eig = _triple_eig(p1, p2, p3, criteria_keys, initial_sq)
        if eig > best_eig:
            best_eig = eig
            best_result = (p1, p2, p3)

    return best_result


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
