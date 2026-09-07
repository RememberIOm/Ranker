"""저장소와 분리된 온라인 BT 대각 근사 및 표시용 예측 계산.

평균·분산 갱신은 기존 모델과 같다. 정확한 결합 사후분포나 보정된
신뢰구간을 보장하지 않으며, 다자 순위는 쌍대비교 합성우도로 처리한다.
"""

import math

ALGORITHM_VERSION = "bt-diagonal-v1"
SIGMA_SQ_FLOOR = 0.01


def sigmoid(x: float) -> float:
    """극단적인 실력 차이에서도 안전한 로지스틱 함수."""
    return 1.0 / (1.0 + math.exp(-max(-500.0, min(500.0, x))))


def update_ratings(
    ratings: dict[int, tuple[float, float]],
    comparisons: list[tuple[int, int, float]],
) -> dict[int, tuple[float, float]]:
    """원본 상태에서 모든 비교를 계산하여 새 상태를 반환한다.

    입력은 수정하지 않는다. 비교에 참여하지 않은 항목의 상태는 유지하며,
    동일 배틀의 비교 나열 순서는 결과에 영향을 주지 않는다.
    """
    gradients: dict[int, list[float]] = {key: [] for key in ratings}
    information: dict[int, list[float]] = {key: [] for key in ratings}
    for mu, variance in ratings.values():
        if not math.isfinite(mu) or not math.isfinite(variance) or variance <= 0:
            raise ValueError("평균은 유한하고 분산은 유한한 양수여야 합니다.")
    for a, b, outcome in comparisons:
        if a == b or a not in ratings or b not in ratings:
            raise ValueError("서로 다른 기존 항목을 비교해야 합니다.")
        if not math.isfinite(outcome) or not 0 <= outcome <= 1:
            raise ValueError("비교 결과는 0과 1 사이여야 합니다.")
        p = sigmoid(ratings[a][0] - ratings[b][0])
        gradient = outcome - p
        fisher = p * (1 - p)
        gradients[a].append(gradient)
        gradients[b].append(-gradient)
        information[a].append(fisher)
        information[b].append(fisher)
    result = dict(ratings)
    for key, (mu, variance) in ratings.items():
        if not gradients[key]:
            continue
        precision = 1 / variance + math.fsum(information[key])
        result[key] = (
            mu + math.fsum(gradients[key]) / precision,
            max(SIGMA_SQ_FLOOR, 1 / precision),
        )
    return result


def _normal_grid() -> tuple[tuple[float, float], ...]:
    """표준정규 적분용 고정 Simpson 격자: ±8σ 바깥의 질량은 무시한다."""
    points = []
    for i in range(161):
        z = -8 + i / 10
        multiplier = 1 if i in (0, 160) else (4 if i % 2 else 2)
        points.append((z, multiplier * math.exp(-z * z / 2)))
    total = math.fsum(weight for _, weight in points)
    return tuple((z, weight / total) for z, weight in points)


_NORMAL_GRID = _normal_grid()


def predict_outcomes(
    mu_a: float,
    variance_a: float,
    mu_b: float,
    variance_b: float,
    draw_rate: float,
    draw_bandwidth: float,
) -> tuple[float, float, float]:
    """독립 정규 근사에서 승·무·패를 적분한 표시용 추정치.

    무승부 성분은 Gaussian 실력차 감쇠 휴리스틱이며 BT 학습 우도와 다르다.
    따라서 보정된 승률이라고 주장하지 않는다. draw_rate는 같은 실력일 때의
    무승부 상한이며 전체 대결의 실측 무승부율과 일치함을 보장하지 않는다.
    """
    if not all(math.isfinite(value) for value in (mu_a, mu_b, variance_a, variance_b)):
        raise ValueError("실력과 분산은 유한해야 합니다.")
    if min(variance_a, variance_b) < 0:
        raise ValueError("분산은 음수일 수 없습니다.")
    if not math.isfinite(draw_bandwidth) or draw_bandwidth <= 0:
        raise ValueError("무승부 감쇠 폭은 유한한 양수여야 합니다.")
    if not math.isfinite(draw_rate) or not 0 <= draw_rate <= 1:
        raise ValueError("무승부 상한은 0과 1 사이여야 합니다.")
    delta = mu_a - mu_b
    spread = math.hypot(math.sqrt(variance_a), math.sqrt(variance_b))
    wins, draws = [], []
    for z, weight in _NORMAL_GRID:
        difference = delta + spread * z
        ratio = difference / draw_bandwidth
        # 유효한 큰 입력에서도 제곱 오버플로우를 피한다.
        draw = draw_rate * math.exp(-(ratio * ratio)) if abs(ratio) < 28 else 0.0
        draws.append(weight * draw)
        wins.append(weight * (1 - draw) * sigmoid(difference))
    win_a, draw = math.fsum(wins), math.fsum(draws)
    return win_a, draw, max(0.0, 1 - win_a - draw)
