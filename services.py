# services.py
# Bayesian BT 대각 근사 엔진의 저장소 어댑터와 매칭·표시 계산.
# σ²와 정보량은 근사치이며 실제 포함률·효율은 별도 평가가 필요합니다.

import heapq
import math
import random
from itertools import combinations
from collections.abc import Iterable
from typing import Any

from store import DataStore


# --- Bayesian BT Core ---

from rating_engine import (
    predict_outcomes,
    sigmoid as sigmoid,
    update_ratings,
)


def bt_update(
    mu_a: float,
    sigma_sq_a: float,
    mu_b: float,
    sigma_sq_b: float,
    outcome: float,
) -> tuple[float, float, float, float]:
    """기존 호출부를 위한 순수 BT 대각 근사 엔진 어댑터."""
    updated = update_ratings(
        {0: (mu_a, sigma_sq_a), 1: (mu_b, sigma_sq_b)}, [(0, 1, outcome)]
    )
    return *updated[0], *updated[1]


def hierarchical_shrinkage(
    store: DataStore, item: dict[str, Any], keys: set[str] | None = None
) -> None:
    """계층적 축소: 기준 간 정보를 공유하여 데이터 부족 기준을 보강합니다.

    각 기준 k의 μ를 나머지 기준들의 정밀도 가중 평균(Leave-One-Out cross_mean)
    방향으로 축소합니다 (in-place). LOO 방식으로 자기 자신이 축소 대상에
    포함되는 자기 강화 편향을 제거합니다.
    축소 강도는 기준별 관측 수에 반비례하여 적응 — 데이터 풍부 기준은 덜 축소됩니다.
    """
    base_strength = store.settings["hierarchical_strength"]
    if base_strength <= 0:
        return

    criteria = [c for c in store.criteria if keys is None or c["key"] in keys]
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
    """독립 정규·무승부 감쇠 근사의 승/무/패 추정치를 표시한다.

    경험적 무승부율의 Beta 추정치는 감쇠 전 상한으로 쓰며,
    전체 실측 비율로의 수렴이나 보정된 예측확률을 보장하지 않는다.
    """
    s = store.settings
    draw_rate = (s["draw_prior_max"] * s["draw_prior_strength"] + draws) / (
        s["draw_prior_strength"] + battles
    )
    win_a, draw, _ = predict_outcomes(
        mu_a,
        sigma_sq_a,
        mu_b,
        sigma_sq_b,
        max(0.0, min(1.0, draw_rate)),
        s["draw_bandwidth"],
    )
    shown_a, shown_draw = round(win_a * 100, 1), round(draw * 100, 1)
    return {
        "win_a": shown_a,
        "draw": shown_draw,
        "win_b": round(100 - shown_a - shown_draw, 1),
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
    """두 항목 간 대각 근사의 정보량 점수를 합산합니다.

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
    """정보량 근사로 대결을 고르고 동점 후보와 표시 위치를 무작위화한다."""
    selected = _select_match(store, size=2, focus_id=focus_id)
    if selected is None:
        return (store.get_item(focus_id) if focus_id else None), None
    return selected[0], selected[1]


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
    """3-way 후보를 고른다. 쌍 점수를 한 번 계산해 삼중 탐색에 재사용한다."""
    selected = _select_match(store, size=3, focus_id=focus_id)
    if selected is None:
        return None, None, None
    return selected[0], selected[1], selected[2]


def _recent_matches(store: DataStore) -> set[frozenset[int]]:
    """취소하지 않은 최근 다섯 투표와 진행 중 대결의 항목 조합을 읽는다."""
    events = sorted(
        (event for event in getattr(store, "history", []) if not event.get("undone")),
        key=lambda event: event.get("created_at", 0),
        reverse=True,
    )[:5]
    payloads = [event["payload"] for event in events]
    active = getattr(store, "active_round", None)
    if active:
        payloads.append(active)
    return {
        frozenset(
            payload[key]
            for key in ("item1_id", "item2_id", "item3_id")
            if payload.get(key) is not None
        )
        for payload in payloads
    }


def _select_match(
    store: DataStore, size: int, focus_id: int | None
) -> tuple[dict[str, Any], ...] | None:
    """최고 정보량 후보를 선택하되 최근 조합을 가능한 경우 제외한다.

    후보가 모두 최근 조합이면 반복을 허용한다. 집중 항목도 표시 위치는
    무작위이며, 500개 초과 시 집중 항목을 보존한 채 후보를 샘플링한다.
    """
    if len(store.items) < size:
        return None
    focus = store.get_item(focus_id) if focus_id is not None else None
    if focus_id is not None and focus is None:
        return None
    pool = [item for item in store.items if item is not focus]
    limit = _EIG_SAMPLE_THRESHOLD - (1 if focus else 0)
    if len(pool) > limit:
        pool = random.sample(pool, limit)
    else:
        random.shuffle(pool)
    if focus:
        pool.insert(0, focus)
    count = len(pool)
    keys = [c["key"] for c in store.criteria]
    initial_sq = store.settings["initial_sigma"] ** 2
    edge_pairs = (
        ((0, i) for i in range(1, count))
        if focus and size == 2
        else combinations(range(count), 2)
    )
    edges = {
        (i, j): _pair_eig(pool[i], pool[j], keys, initial_sq) for i, j in edge_pairs
    }
    candidates: Iterable[tuple[int, ...]]
    if focus:
        candidates = (
            (0, *others) for others in combinations(range(1, count), size - 1)
        )
    elif size == 2 or count <= _TRIPLE_EXHAUSTIVE_THRESHOLD:
        candidates = combinations(range(count), size)
    else:
        # 큰 풀에서는 상위 쌍 열 개에 세 번째 항목을 더하는 근사를 사용한다.
        top_pairs = heapq.nlargest(10, edges, key=edges.__getitem__)
        candidates = sorted(
            {
                tuple(sorted((i, j, k)))
                for i, j in top_pairs
                for k in range(count)
                if k not in (i, j)
            }
        )
    recent = _recent_matches(store)
    best_any: tuple[int, ...] | None = None
    best_new: tuple[int, ...] | None = None
    score_any = score_new = -math.inf
    ties_any = ties_new = 0
    for candidate in candidates:
        score = math.fsum(edges[pair] for pair in combinations(candidate, 2))
        if score > score_any:
            best_any, score_any, ties_any = candidate, score, 1
        elif score == score_any:
            ties_any += 1
            if random.randrange(ties_any) == 0:
                best_any = candidate
        if frozenset(pool[i]["id"] for i in candidate) in recent:
            continue
        if score > score_new:
            best_new, score_new, ties_new = candidate, score, 1
        elif score == score_new:
            ties_new += 1
            if random.randrange(ties_new) == 0:
                best_new = candidate
    chosen = best_new if best_new is not None else best_any
    if chosen is None:
        return None
    selected = [pool[i] for i in chosen]
    random.shuffle(selected)
    return tuple(selected)


# --- Ranking ---


def get_item_ranks(store: DataStore) -> tuple[dict[int, int], int]:
    """전체 항목의 {item_id: rank} 맵과 총 항목 수를 반환합니다 (rank=1이 최고).

    한 번의 정렬로 여러 항목의 순위를 조회할 때 사용합니다.
    """
    scores = sorted(
        ((composite_rating(store, item), item["id"]) for item in store.items),
        key=lambda x: x[0],
        reverse=True,
    )
    ranks: dict[int, int] = {}
    previous_score: float | None = None
    rank = 0
    for position, (score, iid) in enumerate(scores, start=1):
        if previous_score is None or score != previous_score:
            rank = position
        ranks[iid] = rank
        previous_score = score
    return ranks, len(scores)


def get_item_rank(store: DataStore, item_id: int) -> tuple[int, int]:
    """가중 합산 점수 기준으로 item_id의 순위를 반환합니다.

    Returns:
        (rank, total): rank=1이 최고, total은 전체 항목 수.
        item_id가 목록에 없으면 (total, total)을 반환합니다 (최하위 취급).
    """
    ranks, total = get_item_ranks(store)
    return ranks.get(item_id, total), total
