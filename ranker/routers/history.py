"""투표 기록 조회, 취소와 현재 설정으로 다시 계산."""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ranker.deps import StoreDep
from ranker.template_env import templates

router = APIRouter(prefix="/history", tags=["history"])

_ROLES = frozendict(best="최고", worst="최하", tied="동률")


def _summaries(event: dict) -> list[str]:
    """투표 당시의 항목·기준 이름으로 선택을 설명합니다."""
    payload, names, labels = event["payload"], event["names"], event["labels"]
    summaries = []
    for key, vote in payload["votes"].items():
        if vote == "skip":
            choice = "건너뜀"
        elif isinstance(vote, str):
            choice = {
                "1": f"{names[str(payload['item1_id'])]} 승",
                "2": f"{names[str(payload['item2_id'])]} 승",
                "draw": "무승부",
            }[vote]
        else:
            choice = ", ".join(
                f"{names[item_id]} {_ROLES[role]}" for item_id, role in vote.items()
            )
        summaries.append(f"{labels[key]}: {choice}")
    return summaries


@router.get("", response_class=HTMLResponse)
async def history_page(
    request: Request, store: StoreDep, page: int = 1
) -> HTMLResponse:
    result = await store.history_page(page)
    events = [
        {
            **event,
            "item_names": list(event["names"].values()),
            "vote_summary": _summaries(event),
            "created_at": datetime.fromtimestamp(event["created_at"], UTC).strftime(
                "%Y-%m-%d %H:%M UTC"
            ),
        }
        for event in result["events"]
    ]
    return templates.TemplateResponse(
        request, "history.html", {**result, "events": events}
    )


@router.post("/undo")
async def undo_vote(event_id: Annotated[int, Form()], store: StoreDep) -> Response:
    await store.undo_last_vote(expected_event_id=event_id)
    return RedirectResponse("/history", status_code=303)


@router.post("/recalculate")
async def recalculate(store: StoreDep) -> Response:
    await store.recalculate_ratings()
    return RedirectResponse("/ranking", status_code=303)


@router.post("/clear")
async def clear_history(store: StoreDep) -> Response:
    await store.clear_history()
    return RedirectResponse("/history", status_code=303)
