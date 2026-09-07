"""투표 원본 조회, 취소와 현재 설정 재계산."""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from deps import require_store
from store import DataStore, SessionSaveError
from template_env import templates

router = APIRouter(prefix="/history", tags=["history"])


@router.get("", response_class=HTMLResponse)
async def history_page(
    request: Request, store: DataStore = Depends(require_store)
) -> HTMLResponse:
    events = []
    for event in reversed(store.history):
        payload = event.get("payload", {})
        before = event.get("before_state", {})
        name_by_id = {str(item["id"]): item["name"] for item in before.get("items", [])}
        names = list(name_by_id.values())
        labels = {c["key"]: c["label"] for c in before.get("criteria", [])}
        summaries = []
        for key, vote in payload.get("votes", {}).items():
            if isinstance(vote, str):
                choice = {
                    "1": name_by_id.get(str(payload.get("item1_id")), "A"),
                    "2": name_by_id.get(str(payload.get("item2_id")), "B"),
                    "draw": "무승부",
                    "skip": "건너뛰기",
                }.get(vote, vote)
            else:
                roles = {
                    "best": "최고",
                    "worst": "최하",
                    "tied": "동률",
                    "skip": "건너뛰기",
                }
                choice = ", ".join(
                    f"{name_by_id.get(item_id, '')} {roles.get(role, role)}".strip()
                    for item_id, role in vote.items()
                )
            summaries.append(f"{labels.get(key, key)}: {choice}")
        events.append(
            {
                **event,
                "item_names": names,
                "vote_summary": summaries,
                "votes": payload.get("votes", {}),
                "created_at": datetime.fromtimestamp(
                    event["created_at"], timezone.utc
                ).strftime("%Y-%m-%d %H:%M UTC"),
            }
        )
    active = [
        event
        for event in events
        if not event.get("undone") and not event.get("archived")
    ]
    return templates.TemplateResponse(
        request,
        "history.html",
        {
            "events": events,
            "can_undo": bool(active),
            "last_event_id": active[0]["id"] if active else None,
        },
    )


@router.post("/undo")
async def undo_vote(
    event_id: int = Form(...), store: DataStore = Depends(require_store)
) -> Response:
    try:
        await store.undo_last_vote(expected_event_id=event_id)
    except SessionSaveError:
        raise
    except (ValueError, RuntimeError):
        return HTMLResponse(
            "투표 기록이 변경되었거나 처리할 기록이 없습니다. 새로고침해주세요.",
            status_code=409,
        )
    return RedirectResponse("/history", status_code=303)


@router.post("/recalculate")
async def recalculate(
    store: DataStore = Depends(require_store),
) -> Response:
    try:
        await store.replay_history(settings_patch={})
    except SessionSaveError:
        raise
    except (ValueError, RuntimeError):
        return HTMLResponse(
            "투표 기록이 변경되었거나 처리할 기록이 없습니다. 새로고침해주세요.",
            status_code=409,
        )
    return RedirectResponse("/ranking", status_code=303)


@router.post("/archive")
async def archive_history(
    store: DataStore = Depends(require_store),
) -> RedirectResponse:
    await store.clear_history()
    return RedirectResponse("/history", status_code=303)
