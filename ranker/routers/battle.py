# routers/battle.py
# 모든 평가 기준을 한 라운드에서 비교하고 결과와 다음 대결을 반환합니다.
# 세션별 DataStore를 사용하여 멀티유저를 지원합니다.

import asyncio
import logging
import re
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response

from ranker.deps import is_htmx, require_store
from ranker.schemas import BattleVoteRequest, ThreeWayBattleVoteRequest
from ranker.services import (
    display_rating,
    display_uncertainty,
    get_item_ranks,
    get_match_pair,
    get_match_probabilities,
    get_match_triple,
)
from ranker.store import DataStore
from ranker.template_env import templates

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
    criteria_info = []
    for c in store.criteria:
        mu1, sq1 = item1["mu"][c["key"]], item1["sigma_sq"][c["key"]]
        mu2, sq2 = item2["mu"][c["key"]], item2["sigma_sq"][c["key"]]
        probs = (
            get_match_probabilities(store, c["key"], item1["id"], item2["id"])
            if not store.settings["blind_mode"]
            else None
        )
        criteria_info.append(
            {
                **c,
                "r1": round(display_rating(store, mu1), 1),
                "r2": round(display_rating(store, mu2), 1),
                "sigma1": round(display_uncertainty(store, sq1), 1),
                "sigma2": round(display_uncertainty(store, sq2), 1),
                "probs": probs,
            }
        )

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
        "focus_name": store.get_item(focus_id)["name"] if focus_id else "",
        "round_token": round_token,
        "blind_mode": store.settings["blind_mode"],
        "result_auto_skip": store.settings["result_auto_skip"],
        "result_skip_seconds": store.settings["result_skip_seconds"],
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
    criteria_info = []
    for c in store.criteria:
        item_data = []
        for item in (item1, item2, item3):
            mu, sq = item["mu"][c["key"]], item["sigma_sq"][c["key"]]
            item_data.append(
                {
                    "id": item["id"],
                    "r": round(display_rating(store, mu), 1),
                    "sigma": round(display_uncertainty(store, sq), 1),
                }
            )

        criteria_info.append(
            {
                **c,
                "item_ratings": item_data,
            }
        )

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
        "focus_name": store.get_item(focus_id)["name"] if focus_id else "",
        "round_token": round_token,
        "blind_mode": store.settings["blind_mode"],
        "result_auto_skip": store.settings["result_auto_skip"],
        "result_skip_seconds": store.settings["result_skip_seconds"],
    }


_EMPTY_NO_CRITERIA = {
    "icon": "📐",
    "title": "평가 기준이 없습니다",
    "description": "대결하려면 평가 기준이 하나 이상 있어야 합니다.",
    "link_url": "/manage?tab=criteria",
    "link_text": "기준 추가하러 가기",
}

_EMPTY_NOT_ENOUGH = {
    "icon": "📭",
    "title": "항목이 부족합니다",
    "description": "대결하려면 항목이 {min_count}개 이상 있어야 합니다.",
    "link_url": "/manage?tab=items",
    "link_text": "항목 추가하러 가기",
}


def _battle_template(
    request: Request, ctx: dict[str, Any], *, is_3way: bool
) -> HTMLResponse:
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
    """진행 중인 대결이 조건에 맞으면 이어서 보여주고, 아니면 새로 고릅니다.

    3개 비교 모드에서 항목이 3개 미만이면 1대1로 진행합니다.
    Returns (ctx, is_3way). 대결을 만들 수 없으면 (None, False).
    """
    three_way = store.settings["battle_mode"] == "3way" and len(store.items) >= 3
    size = 3 if three_way else 2
    reused = store.reusable_round(size, focus_id)
    if reused:
        token, items = reused
    else:
        select = get_match_triple if three_way else get_match_pair
        items = await asyncio.to_thread(select, store, focus_id=focus_id)
        if not all(items):
            return None, False
        token = await store.issue_battle_round([item["id"] for item in items])
        items = [store.get_item(item["id"]) for item in items]
    build = _build_3way_context if three_way else _build_battle_context
    ctx = await asyncio.to_thread(
        build,
        store,
        *items,
        token,
        focus_mode=focus_id is not None,
        focus_id=focus_id,
    )
    return ctx, three_way


@router.get("", response_class=HTMLResponse)
async def get_battle(
    request: Request, store: DataStore = Depends(require_store)
) -> HTMLResponse:
    if not store.criteria:
        return templates.TemplateResponse(
            request, "battle_empty.html", _EMPTY_NO_CRITERIA
        )

    ctx, is_3way = await _pick_match(store)
    if ctx is None:
        empty_ctx = {
            **_EMPTY_NOT_ENOUGH,
            "description": _EMPTY_NOT_ENOUGH["description"].format(min_count=2),
        }
        return templates.TemplateResponse(request, "battle_empty.html", empty_ctx)
    return _battle_template(request, ctx, is_3way=is_3way)


@router.get("/focus/{item_id}", response_class=HTMLResponse)
async def focus_battle(
    item_id: int, request: Request, store: DataStore = Depends(require_store)
) -> Response:
    if not store.criteria:
        return templates.TemplateResponse(
            request, "battle_empty.html", _EMPTY_NO_CRITERIA
        )
    if not store.get_item(item_id):
        return templates.TemplateResponse(
            request,
            "error.html",
            {"message": "이 항목은 삭제되었거나 다른 랭킹에 있습니다."},
            status_code=404,
        )
    ctx, is_3way = await _pick_match(store, focus_id=item_id)
    if ctx is None:
        empty_ctx = {
            **_EMPTY_NOT_ENOUGH,
            "description": _EMPTY_NOT_ENOUGH["description"].format(min_count=2),
        }
        return templates.TemplateResponse(request, "battle_empty.html", empty_ctx)
    return _battle_template(request, ctx, is_3way=is_3way)


def _parse_focus_id(redirect_to: str | None) -> int | None:
    if not redirect_to:
        return None
    m = _FOCUS_RE.match(redirect_to)
    return int(m.group(1)) if m else None


async def _render_next_battle(store: DataStore, redirect_to: str | None) -> str:
    """다음 대결 카드를 렌더링합니다. 실패하면 빈 문자열을 돌려 새로고침하게 합니다."""
    try:
        ctx, is_3way = await _pick_match(store, focus_id=_parse_focus_id(redirect_to))
    except Exception:
        logger.exception("next_battle_render_failed")
        return ""
    if ctx is None:
        return ""
    partial = (
        "partials/battle_3way_cards.html" if is_3way else "partials/battle_cards.html"
    )
    return templates.env.get_template(partial).render(**ctx)


async def _vote_response(
    store: DataStore, payload: BattleVoteRequest | ThreeWayBattleVoteRequest
) -> HTMLResponse:
    """결과 모달과 다음 대결 카드(OOB)를 함께 돌려줍니다."""
    response_data = await store.apply_vote(payload)
    template = (
        "partials/battle_3way_result.html"
        if isinstance(payload, ThreeWayBattleVoteRequest)
        else "partials/battle_result.html"
    )
    html = templates.env.get_template(template).render(
        **response_data,
        result_auto_skip=store.settings["result_auto_skip"],
        result_skip_seconds=store.settings["result_skip_seconds"],
    )
    next_html = await _render_next_battle(store, payload.redirect_to)
    if next_html:
        html += f'\n<div id="battle-arena" hx-swap-oob="innerHTML">{next_html}</div>'
    return HTMLResponse(html)


@router.post("/vote")
async def vote(
    payload: BattleVoteRequest, store: DataStore = Depends(require_store)
) -> Response:
    return await _vote_response(store, payload)


@router.post("/vote/3way")
async def vote_3way(
    payload: ThreeWayBattleVoteRequest, store: DataStore = Depends(require_store)
) -> Response:
    return await _vote_response(store, payload)
