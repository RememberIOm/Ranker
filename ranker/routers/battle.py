"""대결 화면과 투표 저장. 한 라운드에서 모든 기준을 비교하고 결과와 다음 대결을 돌려줍니다."""

import asyncio
import logging
import re
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from ranker.deps import StoreDep, is_htmx
from ranker.schemas import BattleVoteRequest, ThreeWayBattleVoteRequest
from ranker.services import (
    display_rating,
    display_uncertainty,
    get_item_ranks,
    get_match_probabilities,
    select_match,
)
from ranker.store import DataStore
from ranker.template_env import templates

logger = logging.getLogger("ranker.battle")

router = APIRouter(prefix="/battle", tags=["battle"])

_FOCUS_RE = re.compile(r"/battle/focus/(\d+)")

_CARDS = frozendict(
    {2: "partials/battle_cards.html", 3: "partials/battle_3way_cards.html"}
)


def _battle_context(
    store: DataStore,
    items: list[dict[str, Any]],
    round_token: str,
    focus_id: int | None,
) -> dict[str, Any]:
    """대결 카드 템플릿 컨텍스트. 1대1은 블라인드가 아니면 예상 확률도 넣습니다."""
    blind = store.settings["blind_mode"]
    criteria_info = []
    for c in store.criteria:
        key = c["key"]
        info = {
            **c,
            "item_ratings": [
                {
                    "id": item["id"],
                    "r": round(display_rating(store, item["mu"][key]), 1),
                    "sigma": round(
                        display_uncertainty(store, item["sigma_sq"][key]), 1
                    ),
                }
                for item in items
            ],
        }
        if len(items) == 2 and not blind:
            info["probs"] = get_match_probabilities(
                store, key, items[0]["id"], items[1]["id"]
            )
        criteria_info.append(info)

    ranks, total = get_item_ranks(store)
    ctx = {
        "cards": _CARDS[len(items)],
        "criteria_info": criteria_info,
        "total_items": total,
        "focus_id": focus_id,
        "focus_name": store.get_item(focus_id)["name"] if focus_id else "",
        "round_token": round_token,
        "blind_mode": blind,
    }
    for index, item in enumerate(items, 1):
        ctx[f"item{index}"] = item
        ctx[f"rank{index}"] = ranks[item["id"]]
    return ctx


_EMPTY_NO_CRITERIA = frozendict(
    icon="📐",
    title="평가 기준이 없습니다",
    description="대결하려면 평가 기준이 하나 이상 있어야 합니다.",
    link_url="/manage?tab=criteria",
    link_text="평가 기준 추가",
)

_EMPTY_NOT_ENOUGH = frozendict(
    icon="📭",
    title="항목이 부족합니다",
    description="대결하려면 항목이 2개 이상 있어야 합니다.",
    link_url="/manage?tab=items",
    link_text="항목 추가",
)


async def _pick_match(
    store: DataStore, focus_id: int | None = None
) -> dict[str, Any] | None:
    """진행 중인 대결이 조건에 맞으면 이어서 보여주고, 아니면 새로 고릅니다.

    3개 비교 모드에서 항목이 3개 미만이면 1대1로 진행합니다.
    대결을 만들 수 없으면 None입니다.
    """
    three_way = store.settings["battle_mode"] == "3way" and len(store.items) >= 3
    size = 3 if three_way else 2
    reused = store.reusable_round(size, focus_id)
    if reused:
        token, items = reused
    else:
        selected = await asyncio.to_thread(select_match, store, size, focus_id)
        if selected is None:
            return None
        token = await store.issue_battle_round([item["id"] for item in selected])
        items = [store.get_item(item["id"]) for item in selected]
    return await asyncio.to_thread(_battle_context, store, items, token, focus_id)


async def _battle_page(
    request: Request, store: DataStore, focus_id: int | None = None
) -> HTMLResponse:
    """HTMX 요청에는 대결 카드만, 그 밖에는 전체 페이지를 돌려줍니다."""
    if not store.criteria:
        return templates.TemplateResponse(
            request, "battle_empty.html", dict(_EMPTY_NO_CRITERIA)
        )
    ctx = await _pick_match(store, focus_id)
    if ctx is None:
        return templates.TemplateResponse(
            request, "battle_empty.html", dict(_EMPTY_NOT_ENOUGH)
        )
    template = ctx["cards"] if is_htmx(request) else "battle.html"
    return templates.TemplateResponse(request, template, ctx)


@router.get("", response_class=HTMLResponse)
async def get_battle(request: Request, store: StoreDep) -> HTMLResponse:
    return await _battle_page(request, store)


@router.get("/focus/{item_id}", response_class=HTMLResponse)
async def focus_battle(item_id: int, request: Request, store: StoreDep) -> Response:
    if store.criteria and not store.get_item(item_id):
        return templates.TemplateResponse(
            request,
            "error.html",
            {"message": "삭제되었거나 현재 랭킹에 없는 항목입니다."},
            status_code=404,
        )
    return await _battle_page(request, store, item_id)


def _parse_focus_id(redirect_to: str | None) -> int | None:
    if not redirect_to:
        return None
    m = _FOCUS_RE.fullmatch(redirect_to)
    return int(m.group(1)) if m else None


async def _render_next_battle(store: DataStore, redirect_to: str | None) -> str:
    """다음 대결 카드를 렌더링합니다. 실패하면 빈 문자열을 돌려 새로고침하게 합니다."""
    try:
        ctx = await _pick_match(store, focus_id=_parse_focus_id(redirect_to))
    except Exception:
        logger.exception("next_battle_render_failed")
        return ""
    if ctx is None:
        return ""
    return templates.env.get_template(ctx["cards"]).render(**ctx)


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
async def vote(payload: BattleVoteRequest, store: StoreDep) -> Response:
    return await _vote_response(store, payload)


@router.post("/vote/3way")
async def vote_3way(payload: ThreeWayBattleVoteRequest, store: StoreDep) -> Response:
    return await _vote_response(store, payload)
