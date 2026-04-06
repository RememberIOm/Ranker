# routers/battle.py
# 모든 평가 기준을 한 라운드에 동시 비교하여 Elo 수렴 속도를 대폭 향상시킵니다.
# 세션별 DataStore를 사용하여 멀티유저를 지원합니다.

from fastapi import APIRouter, Request, BackgroundTasks, Cookie, Depends
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from deps import get_session_store, require_store
from store import DataStore
from services import (
    get_match_pair,
    calculate_elo_update,
    normalize_scores,
    get_match_probabilities,
)

router = APIRouter(prefix="/battle", tags=["battle"])
templates = Jinja2Templates(directory="templates")


def _build_battle_context(
    request: Request,
    store,
    item1: dict,
    item2: dict,
    *,
    focus_mode: bool = False,
    focus_id: int | None = None,
) -> dict:
    """배틀 페이지 템플릿 컨텍스트를 구성합니다."""
    criteria = store.criteria
    init = store.settings["initial_rating"]

    # 각 기준별 확률 계산
    criteria_info = []
    for c in criteria:
        r1 = item1["ratings"].get(c["key"], init)
        r2 = item2["ratings"].get(c["key"], init)
        probs = get_match_probabilities(store, r1, r2)
        criteria_info.append({
            **c,
            "r1": round(r1),
            "r2": round(r2),
            "probs": probs,
        })

    return {
        "item1": item1,
        "item2": item2,
        "criteria_info": criteria_info,
        "focus_mode": focus_mode,
        "focus_id": focus_id,
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

    ctx = _build_battle_context(request, store, item1, item2)
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

    ctx = _build_battle_context(
        request, store, item1, item2, focus_mode=True, focus_id=item_id
    )
    return templates.TemplateResponse(request, "battle.html", ctx)


@router.post("/vote")
async def vote(
    request: Request,
    background_tasks: BackgroundTasks,
    session_id: str | None = Cookie(default=None),
):
    """
    모든 criteria에 대한 투표를 한번에 수신하여 일괄 업데이트합니다.
    Body JSON: { "item1_id": int, "item2_id": int, "votes": {"key": "1"|"2"|"draw", ...}, "redirect_to": str|null }
    """
    store = await get_session_store(request, session_id)
    if not store:
        return JSONResponse({"error": "No active session"}, status_code=401)

    body = await request.json()
    item1_id: int = body["item1_id"]
    item2_id: int = body["item2_id"]
    votes: dict[str, str] = body.get("votes", {})
    _raw_redirect: str = body.get("redirect_to") or ""
    redirect_to = _raw_redirect if (_raw_redirect.startswith("/") and not _raw_redirect.startswith("//")) else ""

    a1 = store.get_item(item1_id)
    a2 = store.get_item(item2_id)
    if not a1 or not a2:
        return JSONResponse({"error": "Item not found"}, status_code=404)

    criteria = store.criteria
    results: list[dict] = []

    for c in criteria:
        key = c["key"]
        winner = votes.get(key, "draw")

        old_r1 = a1["ratings"].get(key, 1200.0)
        old_r2 = a2["ratings"].get(key, 1200.0)

        match winner:
            case "1": actual_score = 1.0
            case "2": actual_score = 0.0
            case _:   actual_score = 0.5

        new_r1, new_r2 = calculate_elo_update(
            store, old_r1, old_r2, actual_score,
            a1["matches_played"], a2["matches_played"],
        )

        a1["ratings"][key] = new_r1
        a2["ratings"][key] = new_r2

        results.append({
            "key": key,
            "label": c["label"],
            "color": c["color"],
            "winner": winner,
            "old_r1": round(old_r1),
            "new_r1": round(new_r1),
            "diff_r1": round(new_r1 - old_r1),
            "old_r2": round(old_r2),
            "new_r2": round(new_r2),
            "diff_r2": round(new_r2 - old_r2),
        })

    # matches_played는 라운드당 1회만 증가
    a1["matches_played"] += 1
    a2["matches_played"] += 1
    await store.save()

    background_tasks.add_task(normalize_scores, store)

    response_data = {
        "a1_id": a1["id"],
        "a2_id": a2["id"],
        "a1_name": a1["name"],
        "a2_name": a2["name"],
        "results": results,
        "total_items": len(store.items),
        "next_url": redirect_to if redirect_to else "/battle",
    }

    return JSONResponse(response_data)
