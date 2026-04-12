# routers/battle.py
# 모든 평가 기준을 한 라운드에 동시 비교하여 Elo 수렴 속도를 대폭 향상시킵니다.
# 세션별 DataStore를 사용하여 멀티유저를 지원합니다.

import logging

from fastapi import APIRouter, Request, BackgroundTasks, Cookie, Depends, HTTPException
from fastapi.responses import HTMLResponse

from deps import get_session_store, require_store
from schemas import BattleVoteRequest, BattleVoteResponse
from store import (
    BattleItemNotFoundError,
    DataStore,
    InvalidBattleVoteError,
    SessionSaveError,
    StaleBattleRoundError,
)
from services import (
    get_match_pair,
    get_item_rank,
    normalize_scores,
    get_match_probabilities,
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
    init = store.settings["initial_rating"]

    # 각 기준별 확률 계산 (실제 무승부 이력 반영)
    criteria_info = []
    for c in criteria:
        r1 = item1["ratings"].get(c["key"], init)
        r2 = item2["ratings"].get(c["key"], init)
        probs = get_match_probabilities(
            store, r1, r2,
            battles=c.get("battles", 0),
            draws=c.get("draws", 0),
        )
        criteria_info.append({
            **c,
            "r1": round(r1),
            "r2": round(r2),
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


@router.get("", response_class=HTMLResponse)
async def get_battle(request: Request, store: DataStore = Depends(require_store)):
    if not store.criteria:
        return HTMLResponse(
            "<div style='text-align:center;padding:50px;'>"
            "<h2>평가 기준이 없습니다.</h2>"
            "<a href='/manage'>관리 페이지에서 기준을 추가하세요.</a></div>"
        )

    item1, item2 = get_match_pair(store)
    if not item1 or not item2:
        return HTMLResponse(
            "<div style='text-align:center;padding:50px;'>"
            "<h2>데이터가 부족합니다.</h2>"
            "<a href='/manage'>관리 페이지에서 항목을 추가하세요.</a></div>"
        )

    round_token = await store.issue_battle_round(item1["id"], item2["id"])
    ctx = _build_battle_context(store, item1, item2, round_token)
    return templates.TemplateResponse(request, "battle.html", ctx)


@router.get("/focus/{item_id}", response_class=HTMLResponse)
async def focus_battle(item_id: int, request: Request, store: DataStore = Depends(require_store)):
    if not store.criteria:
        return HTMLResponse("평가 기준이 없습니다.", status_code=400)

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
    background_tasks: BackgroundTasks,
    session_id: str | None = Cookie(default=None),
):
    """모든 criteria에 대한 투표를 한번에 수신하여 일괄 업데이트합니다."""
    store = await get_session_store(request, session_id)
    if not store:
        raise HTTPException(status_code=401, detail="No active session")

    try:
        response_data, should_normalize = await store.apply_battle_vote(payload)
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

    if should_normalize:
        background_tasks.add_task(_normalize_scores_safe, store)
    return response_data


async def _normalize_scores_safe(store: DataStore) -> None:
    """백그라운드 정규화 태스크 — 예외를 로깅하고 삼킵니다."""
    try:
        await normalize_scores(store)
    except Exception:
        logger.exception("normalize_failed — session_id=%s", store._session_id)
