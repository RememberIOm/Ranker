# routers/battle.py
# 모든 평가 기준을 한 라운드에 동시 비교하여 Elo 수렴 속도를 대폭 향상시킵니다.
# 세션별 DataStore를 사용하여 멀티유저를 지원합니다.

import logging

from fastapi import APIRouter, Request, Cookie, Depends, HTTPException
from fastapi.responses import HTMLResponse

from deps import get_session_store, require_store
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
    get_item_rank,
    get_match_probabilities,
    display_rating,
    display_uncertainty,
)
from template_env import templates

logger = logging.getLogger("ranker.battle")

router = APIRouter(prefix="/battle", tags=["battle"])


def _build_battle_context(
    store,
    item1: dict,
    item2: dict,
    round_token: str,
    *,
    focus_mode: bool = False,
    focus_id: int | None = None,
) -> dict:
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

    # 순위 계산
    rank1, total = get_item_rank(store, item1["id"])
    rank2, _ = get_item_rank(store, item2["id"])

    return {
        "item1": item1,
        "item2": item2,
        "rank1": rank1,
        "rank2": rank2,
        "total_items": total,
        "criteria_info": criteria_info,
        "focus_mode": focus_mode,
        "focus_id": focus_id,
        "round_token": round_token,
        "result_auto_skip": store.settings.get("result_auto_skip", False),
        "result_skip_seconds": store.settings.get("result_skip_seconds", 3.0),
    }


def _build_3way_context(
    store,
    item1: dict,
    item2: dict,
    item3: dict,
    round_token: str,
    *,
    focus_mode: bool = False,
    focus_id: int | None = None,
) -> dict:
    """3-way 배틀 페이지 템플릿 컨텍스트를 구성합니다."""
    criteria = store.criteria
    initial_sq = store.settings["initial_sigma"] ** 2
    items_3 = [item1, item2, item3]

    criteria_info = []
    for c in criteria:
        item_data = []
        for item in items_3:
            mu = item["mu"].get(c["key"], 0.0)
            sq = item["sigma_sq"].get(c["key"], initial_sq)
            item_data.append({
                "id": item["id"],
                "r": round(display_rating(store, mu), 1),
                "sigma": round(display_uncertainty(store, sq), 1),
            })
        criteria_info.append({
            **c,
            "item_ratings": item_data,
        })

    ranks = []
    for item in items_3:
        rank, total = get_item_rank(store, item["id"])
        ranks.append(rank)
    total_items = total if items_3 else 0

    return {
        "item1": item1,
        "item2": item2,
        "item3": item3,
        "rank1": ranks[0],
        "rank2": ranks[1],
        "rank3": ranks[2],
        "total_items": total_items,
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


@router.get("", response_class=HTMLResponse)
async def get_battle(request: Request, store: DataStore = Depends(require_store)):
    if not store.criteria:
        return templates.TemplateResponse(request, "battle_empty.html", _EMPTY_NO_CRITERIA)

    battle_mode = store.settings.get("battle_mode", "2way")

    if battle_mode == "3way":
        item1, item2, item3 = get_match_triple(store)
        if not item1 or not item2 or not item3:
            # 3개 미만이면 2-way로 fallback 시도
            item1, item2 = get_match_pair(store)
            if not item1 or not item2:
                ctx = {**_EMPTY_NOT_ENOUGH, "description": _EMPTY_NOT_ENOUGH["description"].format(min_count=2)}
                return templates.TemplateResponse(request, "battle_empty.html", ctx)
            round_token = await store.issue_battle_round(item1["id"], item2["id"])
            ctx = _build_battle_context(store, item1, item2, round_token)
            return templates.TemplateResponse(request, "battle.html", ctx)

        round_token = await store.issue_battle_round(item1["id"], item2["id"], item3["id"])
        ctx = _build_3way_context(store, item1, item2, item3, round_token)
        return templates.TemplateResponse(request, "battle_3way.html", ctx)

    # 2-way (기본)
    item1, item2 = get_match_pair(store)
    if not item1 or not item2:
        ctx = {**_EMPTY_NOT_ENOUGH, "description": _EMPTY_NOT_ENOUGH["description"].format(min_count=2)}
        return templates.TemplateResponse(request, "battle_empty.html", ctx)

    round_token = await store.issue_battle_round(item1["id"], item2["id"])
    ctx = _build_battle_context(store, item1, item2, round_token)
    return templates.TemplateResponse(request, "battle.html", ctx)


@router.get("/focus/{item_id}", response_class=HTMLResponse)
async def focus_battle(item_id: int, request: Request, store: DataStore = Depends(require_store)):
    if not store.criteria:
        return HTMLResponse("평가 기준이 없습니다.", status_code=400)

    battle_mode = store.settings.get("battle_mode", "2way")

    if battle_mode == "3way":
        item1, item2, item3 = get_match_triple(store, focus_id=item_id)
        if not item1:
            return HTMLResponse("존재하지 않는 항목입니다.", status_code=404)
        if not item2 or not item3:
            # 3-way 불가 시 2-way fallback
            item1, item2 = get_match_pair(store, focus_id=item_id)
            if not item1:
                return HTMLResponse("존재하지 않는 항목입니다.", status_code=404)
            if not item2:
                return HTMLResponse("상대할 항목 데이터가 부족합니다.", status_code=200)
            round_token = await store.issue_battle_round(item1["id"], item2["id"])
            ctx = _build_battle_context(store, item1, item2, round_token, focus_mode=True, focus_id=item_id)
            return templates.TemplateResponse(request, "battle.html", ctx)

        round_token = await store.issue_battle_round(item1["id"], item2["id"], item3["id"])
        ctx = _build_3way_context(store, item1, item2, item3, round_token, focus_mode=True, focus_id=item_id)
        return templates.TemplateResponse(request, "battle_3way.html", ctx)

    # 2-way
    item1, item2 = get_match_pair(store, focus_id=item_id)
    if not item1:
        return HTMLResponse("존재하지 않는 항목입니다.", status_code=404)
    if not item2:
        return HTMLResponse("상대할 항목 데이터가 부족합니다.", status_code=200)

    round_token = await store.issue_battle_round(item1["id"], item2["id"])
    ctx = _build_battle_context(
        store, item1, item2, round_token, focus_mode=True, focus_id=item_id
    )
    return templates.TemplateResponse(request, "battle.html", ctx)


@router.post("/vote", response_model=BattleVoteResponse)
async def vote(
    payload: BattleVoteRequest,
    request: Request,
    session_id: str | None = Cookie(default=None),
):
    """모든 criteria에 대한 투표를 한번에 수신하여 일괄 업데이트합니다."""
    store = await get_session_store(request, session_id)
    if not store:
        raise HTTPException(status_code=401, detail="No active session")

    try:
        response_data, _ = await store.apply_battle_vote(payload)
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

    return response_data


@router.post("/vote/3way", response_model=ThreeWayBattleVoteResponse)
async def vote_3way(
    payload: ThreeWayBattleVoteRequest,
    request: Request,
    session_id: str | None = Cookie(default=None),
):
    """3-way 배틀: 기준별 best/worst 투표를 수신하여 일괄 업데이트합니다."""
    store = await get_session_store(request, session_id)
    if not store:
        raise HTTPException(status_code=401, detail="No active session")

    try:
        response_data = await store.apply_three_way_vote(payload)
    except BattleItemNotFoundError as exc:
        logger.warning("3way_item_not_found — session_id=%s", session_id)
        raise HTTPException(status_code=404, detail="대결 항목을 찾을 수 없습니다.") from exc
    except StaleBattleRoundError as exc:
        logger.warning("3way_stale_round — session_id=%s", session_id)
        raise HTTPException(status_code=409, detail="대결이 만료되었습니다. 새로고침 후 다시 시도해주세요.") from exc
    except InvalidBattleVoteError as exc:
        logger.warning("3way_invalid_vote — session_id=%s: %s", session_id, exc)
        raise HTTPException(status_code=422, detail="투표 데이터가 올바르지 않습니다.") from exc
    except SessionSaveError as exc:
        raise HTTPException(status_code=500, detail="세션 저장에 실패했습니다. 잠시 후 다시 시도해주세요.") from exc

    return response_data
