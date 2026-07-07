# routers/battle.py
# 모든 평가 기준을 한 라운드에 동시 비교하여 Elo 수렴 속도를 대폭 향상시킵니다.
# 세션별 DataStore를 사용하여 멀티유저를 지원합니다.

import logging
import re
from typing import Any

from fastapi import APIRouter, Request, Cookie, Depends, HTTPException
from fastapi.responses import HTMLResponse, Response

from deps import get_session_store, is_htmx, require_store
from schemas import (
    BattleVoteRequest,
    BattleVoteResponse,
    ThreeWayBattleVoteRequest,
    ThreeWayBattleVoteResponse,
)
from store import (
    BattleItemNotFoundError,
    DataStore,
    InvalidBattleVoteError,
    SessionSaveError,
    StaleBattleRoundError,
)
from services import (
    get_match_pair,
    get_match_triple,
    get_item_ranks,
    get_match_probabilities,
    display_rating,
    display_uncertainty,
)
from template_env import templates

logger = logging.getLogger("ranker.battle")

router = APIRouter(prefix="/battle", tags=["battle"])

_FOCUS_RE = re.compile(r"^/battle/focus/(\d+)$")


def _build_battle_context(
    store: DataStore,
    item1: dict[str, Any],
    item2: dict[str, Any],
    round_token: str,
    *,
    focus_mode: bool = False,
    focus_id: int | None = None,
) -> dict[str, Any]:
    """배틀 페이지 템플릿 컨텍스트를 구성합니다."""
    criteria = store.criteria
    initial_sq = store.settings["initial_sigma"] ** 2

    # 각 기준별 확률 계산 (실제 무승부 이력 반영)
    criteria_info = []
    for c in criteria:
        mu1 = item1["mu"].get(c["key"], 0.0)
        sq1 = item1["sigma_sq"].get(c["key"], initial_sq)
        mu2 = item2["mu"].get(c["key"], 0.0)
        sq2 = item2["sigma_sq"].get(c["key"], initial_sq)
        probs = get_match_probabilities(
            store, mu1, sq1, mu2, sq2,
            battles=c.get("battles", 0),
            draws=c.get("draws", 0),
        )
        criteria_info.append({
            **c,
            "r1": round(display_rating(store, mu1), 1),
            "r2": round(display_rating(store, mu2), 1),
            "sigma1": round(display_uncertainty(store, sq1), 1),
            "sigma2": round(display_uncertainty(store, sq2), 1),
            "probs": probs,
        })

    # 순위 계산 (한 번의 정렬로 두 항목 조회)
    ranks, total = get_item_ranks(store)

    return {
        "item1": item1,
        "item2": item2,
        "rank1": ranks.get(item1["id"], total),
        "rank2": ranks.get(item2["id"], total),
        "total_items": total,
        "criteria_info": criteria_info,
        "focus_mode": focus_mode,
        "focus_id": focus_id,
        "round_token": round_token,
        "result_auto_skip": store.settings.get("result_auto_skip", False),
        "result_skip_seconds": store.settings.get("result_skip_seconds", 3.0),
    }


def _build_3way_context(
    store: DataStore,
    item1: dict[str, Any],
    item2: dict[str, Any],
    item3: dict[str, Any],
    round_token: str,
    *,
    focus_mode: bool = False,
    focus_id: int | None = None,
) -> dict[str, Any]:
    """3-way 배틀 페이지 템플릿 컨텍스트를 구성합니다."""
    criteria = store.criteria
    initial_sq = store.settings["initial_sigma"] ** 2
    items_3 = [item1, item2, item3]

    criteria_info = []
    for c in criteria:
        mus: list[float] = []
        sqs: list[float] = []
        item_data = []
        for item in items_3:
            mu = item["mu"].get(c["key"], 0.0)
            sq = item["sigma_sq"].get(c["key"], initial_sq)
            mus.append(mu)
            sqs.append(sq)
            item_data.append({
                "id": item["id"],
                "r": round(display_rating(store, mu), 1),
                "sigma": round(display_uncertainty(store, sq), 1),
            })

        # 쌍대 승률: A-B, A-C, B-C
        cb, cd = c.get("battles", 0), c.get("draws", 0)
        p_ab = get_match_probabilities(store, mus[0], sqs[0], mus[1], sqs[1], cb, cd)
        p_ac = get_match_probabilities(store, mus[0], sqs[0], mus[2], sqs[2], cb, cd)
        p_bc = get_match_probabilities(store, mus[1], sqs[1], mus[2], sqs[2], cb, cd)

        # 항목별 평균 승률 → 정규화
        raw_a = (p_ab["win_a"] + p_ac["win_a"]) / 2
        raw_b = (p_ab["win_b"] + p_bc["win_a"]) / 2
        raw_c = (p_ac["win_b"] + p_bc["win_b"]) / 2
        total_raw = raw_a + raw_b + raw_c

        if total_raw > 0:
            s_a = round(raw_a / total_raw * 100, 1)
            s_b = round(raw_b / total_raw * 100, 1)
            s_c = round(100 - s_a - s_b, 1)
        else:
            s_a = s_b = s_c = 33.3

        item_data[0]["strength"] = s_a
        item_data[1]["strength"] = s_b
        item_data[2]["strength"] = s_c

        criteria_info.append({
            **c,
            "item_ratings": item_data,
            "strengths": [s_a, s_b, s_c],
        })

    ranks, total = get_item_ranks(store)

    return {
        "item1": item1,
        "item2": item2,
        "item3": item3,
        "rank1": ranks.get(item1["id"], total),
        "rank2": ranks.get(item2["id"], total),
        "rank3": ranks.get(item3["id"], total),
        "total_items": total,
        "criteria_info": criteria_info,
        "focus_mode": focus_mode,
        "focus_id": focus_id,
        "round_token": round_token,
        "result_auto_skip": store.settings.get("result_auto_skip", False),
        "result_skip_seconds": store.settings.get("result_skip_seconds", 3.0),
    }


_EMPTY_NO_CRITERIA = {
    "icon": "📐",
    "title": "평가 기준이 없습니다",
    "description": "대결을 시작하려면 먼저 평가 기준을 추가해야 합니다.",
    "link_url": "/manage?tab=criteria",
    "link_text": "기준 추가하러 가기",
}

_EMPTY_NOT_ENOUGH = {
    "icon": "📭",
    "title": "항목이 부족합니다",
    "description": "대결하려면 최소 {min_count}개 이상의 항목이 필요합니다.",
    "link_url": "/manage?tab=items",
    "link_text": "항목 추가하러 가기",
}


def _battle_template(request: Request, ctx: dict[str, Any], *, is_3way: bool) -> HTMLResponse:
    """배틀 모드에 따라 적절한 full-page 또는 partial 템플릿을 반환합니다."""
    if is_3way:
        full, partial = "battle_3way.html", "partials/battle_3way_cards.html"
    else:
        full, partial = "battle.html", "partials/battle_cards.html"

    if is_htmx(request):
        return templates.TemplateResponse(request, partial, ctx)
    return templates.TemplateResponse(request, full, ctx)


async def _pick_match(
    store: DataStore, focus_id: int | None = None
) -> tuple[dict[str, Any] | None, bool]:
    """배틀 모드에 맞는 매치를 선정하고 라운드를 발급합니다.

    3-way 모드에서 삼중항 구성이 불가하면 2-way로 fallback합니다.

    Returns:
        (ctx, is_3way) — 매치 구성 불가 시 (None, False).
    """
    focus_mode = focus_id is not None
    if store.settings.get("battle_mode", "2way") == "3way":
        item1, item2, item3 = get_match_triple(store, focus_id=focus_id)
        if item1 and item2 and item3:
            token = await store.issue_battle_round(item1["id"], item2["id"], item3["id"])
            ctx = _build_3way_context(
                store, item1, item2, item3, token, focus_mode=focus_mode, focus_id=focus_id
            )
            return ctx, True

    item1, item2 = get_match_pair(store, focus_id=focus_id)
    if not item1 or not item2:
        return None, False
    token = await store.issue_battle_round(item1["id"], item2["id"])
    ctx = _build_battle_context(
        store, item1, item2, token, focus_mode=focus_mode, focus_id=focus_id
    )
    return ctx, False


@router.get("", response_class=HTMLResponse)
async def get_battle(request: Request, store: DataStore = Depends(require_store)) -> HTMLResponse:
    if not store.criteria:
        return templates.TemplateResponse(request, "battle_empty.html", _EMPTY_NO_CRITERIA)

    ctx, is_3way = await _pick_match(store)
    if ctx is None:
        empty_ctx = {**_EMPTY_NOT_ENOUGH, "description": _EMPTY_NOT_ENOUGH["description"].format(min_count=2)}
        return templates.TemplateResponse(request, "battle_empty.html", empty_ctx)
    return _battle_template(request, ctx, is_3way=is_3way)


@router.get("/focus/{item_id}", response_class=HTMLResponse)
async def focus_battle(item_id: int, request: Request, store: DataStore = Depends(require_store)) -> Response:
    if not store.criteria:
        return HTMLResponse("평가 기준이 없습니다.", status_code=400)
    if not store.get_item(item_id):
        return HTMLResponse("존재하지 않는 항목입니다.", status_code=404)

    ctx, is_3way = await _pick_match(store, focus_id=item_id)
    if ctx is None:
        return HTMLResponse("상대할 항목 데이터가 부족합니다.", status_code=200)
    return _battle_template(request, ctx, is_3way=is_3way)


def _parse_focus_id(redirect_to: str | None) -> int | None:
    """redirect_to 경로에서 focus item_id를 추출합니다."""
    if not redirect_to:
        return None
    m = _FOCUS_RE.match(redirect_to)
    return int(m.group(1)) if m else None


async def _render_next_battle(store: DataStore, redirect_to: str | None) -> str:
    """다음 배틀 카드 HTML을 렌더링합니다. 실패 시 빈 문자열."""
    try:
        ctx, is_3way = await _pick_match(store, focus_id=_parse_focus_id(redirect_to))
        if ctx is None:
            return ""
        partial = "partials/battle_3way_cards.html" if is_3way else "partials/battle_cards.html"
        return templates.env.get_template(partial).render(**ctx)
    except Exception:
        logger.exception("next_battle_render_failed")
        return ""


async def _apply_vote(coro: Any, session_id: str | None) -> dict[str, Any]:
    """store 계층의 투표 예외를 HTTP 응답 코드로 변환합니다."""
    try:
        return await coro
    except BattleItemNotFoundError as exc:
        logger.warning("battle_item_not_found — session_id=%s", session_id)
        raise HTTPException(status_code=404, detail="대결 항목을 찾을 수 없습니다.") from exc
    except StaleBattleRoundError as exc:
        logger.warning("stale_round — session_id=%s", session_id)
        raise HTTPException(status_code=409, detail="대결이 만료되었습니다. 새로고침 후 다시 시도해주세요.") from exc
    except InvalidBattleVoteError as exc:
        logger.warning("invalid_vote — session_id=%s: %s", session_id, exc)
        raise HTTPException(status_code=422, detail="투표 데이터가 올바르지 않습니다.") from exc
    except SessionSaveError as exc:
        raise HTTPException(status_code=500, detail="세션 저장에 실패했습니다. 잠시 후 다시 시도해주세요.") from exc


async def _htmx_vote_response(
    store: DataStore,
    response_data: dict[str, Any],
    result_template: str,
    redirect_to: str | None,
) -> HTMLResponse:
    """결과 모달 HTML + OOB 다음 배틀 카드를 조립합니다."""
    result_html = templates.env.get_template(result_template).render(
        **response_data,
        result_auto_skip=store.settings.get("result_auto_skip", False),
        result_skip_seconds=store.settings.get("result_skip_seconds", 3.0),
    )
    next_html = await _render_next_battle(store, redirect_to)
    if next_html:
        result_html += f'\n<div id="battle-arena" hx-swap-oob="innerHTML">{next_html}</div>'
    return HTMLResponse(content=result_html)


@router.post("/vote")
async def vote(
    payload: BattleVoteRequest,
    request: Request,
    session_id: str | None = Cookie(default=None),
) -> Response:
    """모든 criteria에 대한 투표를 한번에 수신하여 일괄 업데이트합니다."""
    store = await get_session_store(request, session_id)
    if not store:
        raise HTTPException(status_code=401, detail="No active session")

    response_data = await _apply_vote(store.apply_battle_vote(payload), session_id)
    if not is_htmx(request):
        return BattleVoteResponse(**response_data)
    return await _htmx_vote_response(
        store, response_data, "partials/battle_result.html", payload.redirect_to
    )


@router.post("/vote/3way")
async def vote_3way(
    payload: ThreeWayBattleVoteRequest,
    request: Request,
    session_id: str | None = Cookie(default=None),
) -> Response:
    """3-way 배틀: 기준별 best/worst 투표를 수신하여 일괄 업데이트합니다."""
    store = await get_session_store(request, session_id)
    if not store:
        raise HTTPException(status_code=401, detail="No active session")

    response_data = await _apply_vote(store.apply_three_way_vote(payload), session_id)
    if not is_htmx(request):
        return ThreeWayBattleVoteResponse(**response_data)
    return await _htmx_vote_response(
        store, response_data, "partials/battle_3way_result.html", payload.redirect_to
    )
