"""투표 기록 조회, 취소와 현재 설정으로 다시 계산."""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ranker.deps import require_store
from ranker.store import DataStore
from ranker.template_env import templates

router = APIRouter(prefix="/history", tags=["history"])

_ROLES = {"best": "최고", "worst": "최하", "tied": "동률"}


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
    request: Request, page: int = 1, store: DataStore = Depends(require_store)
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
async def undo_vote(
    event_id: int = Form(...), store: DataStore = Depends(require_store)
) -> Response:
    await store.undo_last_vote(expected_event_id=event_id)
    return RedirectResponse("/history", status_code=303)


@router.post("/recalculate")
async def recalculate(store: DataStore = Depends(require_store)) -> Response:
    await store.recalculate_ratings()
    return RedirectResponse("/ranking", status_code=303)


@router.post("/clear")
async def clear_history(store: DataStore = Depends(require_store)) -> Response:
    await store.clear_history()
    return RedirectResponse("/history", status_code=303)
