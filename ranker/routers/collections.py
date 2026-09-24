"""이름 있는 랭킹의 생성, 전환, 복구."""

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ranker.boards import (
    attach_board,
    board_library,
    create_board,
    ensure_library,
    get_library,
    library_exists,
    library_id,
    owns_board,
    set_library_cookie,
)
from ranker.cookies import delete_session_cookie, set_session_cookie
from ranker.database import transaction
from ranker.deps import get_session_store
from ranker.store import delete_session
from ranker.template_env import templates

router = APIRouter(prefix="/collections", tags=["collections"])


@router.get("", response_class=HTMLResponse)
async def list_collections(request: Request) -> HTMLResponse:
    code = await get_library(request)
    boards = []
    if code:
        async with transaction() as db:
            cursor = await db.execute(
                "SELECT b.session_id,b.name,COUNT(i.id) AS item_count FROM boards b "
                "LEFT JOIN items i ON i.session_id=b.session_id WHERE b.library_id=? "
                "GROUP BY b.session_id ORDER BY b.name,b.session_id",
                (library_id(code),),
            )
            boards = [dict(row) for row in await cursor.fetchall()]
    store = await get_session_store(request, request.cookies.get("session_id"))
    unlisted = store is not None and await board_library(store.session_id) is None
    return templates.TemplateResponse(
        request,
        "collections.html",
        {
            "boards": boards,
            "active_session_id": store.session_id if store else None,
            "unlisted": unlisted,
            "recovery_code": code,
        },
    )


@router.post("/create")
async def create_collection(
    request: Request, name: str = Form("새 랭킹", max_length=100)
) -> RedirectResponse:
    response = RedirectResponse("/manage", status_code=303)
    await create_board(request, response, name)
    return response


@router.post("/keep-current")
async def keep_current(
    request: Request, name: str = Form("내 랭킹", max_length=100)
) -> RedirectResponse:
    """목록에 없는 현재 랭킹을 내 목록에 넣습니다."""
    store = await get_session_store(request, request.cookies.get("session_id"))
    if store is None:
        raise HTTPException(404, "열려 있는 랭킹이 없습니다.")
    code = await ensure_library(request)
    await attach_board(code, store.session_id, name)
    response = RedirectResponse("/collections", status_code=303)
    set_library_cookie(response, code)
    return response


@router.post("/switch")
async def switch_collection(
    request: Request, session_id: str = Form(...)
) -> RedirectResponse:
    code = await get_library(request)
    if not code or not await owns_board(code, session_id):
        raise HTTPException(404, "이 목록에서 랭킹을 찾을 수 없습니다.")
    response = RedirectResponse("/ranking", status_code=303)
    set_session_cookie(response, session_id)
    return response


@router.post("/rename")
async def rename_collection(
    request: Request,
    session_id: str = Form(...),
    name: str = Form(..., min_length=1, max_length=100),
) -> RedirectResponse:
    code = await get_library(request)
    if not code or not name.strip():
        raise HTTPException(400, "랭킹 이름을 입력해주세요.")
    async with transaction() as db:
        cursor = await db.execute(
            "UPDATE boards SET name=? WHERE session_id=? AND library_id=?",
            (name.strip(), session_id, library_id(code)),
        )
        if cursor.rowcount != 1:
            raise HTTPException(404, "이 목록에서 랭킹을 찾을 수 없습니다.")
    return RedirectResponse("/collections", status_code=303)


@router.post("/recover")
async def recover_collections(code: str = Form(...)) -> RedirectResponse:
    code = code.strip()
    if not await library_exists(code):
        raise HTTPException(400, "복구 코드가 맞지 않습니다. 다시 확인해주세요.")
    response = RedirectResponse("/collections", status_code=303)
    set_library_cookie(response, code)
    delete_session_cookie(response)
    return response


@router.post("/delete")
async def delete_collection(
    request: Request, session_id: str = Form(...)
) -> RedirectResponse:
    code = await get_library(request)
    if not code or not await owns_board(code, session_id):
        raise HTTPException(404, "이 목록에서 랭킹을 찾을 수 없습니다.")
    await delete_session(session_id)
    response = RedirectResponse("/collections", status_code=303)
    if request.cookies.get("session_id") == session_id:
        request.state.session_id = None
        delete_session_cookie(response)
    return response
