"""이름 있는 랭킹의 생성, 전환, 복구."""

from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ranker.boards import (
    create_board,
    get_library,
    get_library_code,
    library_exists,
    library_id,
    owns_board,
    set_library_cookie,
)
from ranker.cookies import delete_session_cookie, set_session_cookie
from ranker.database import transaction
from ranker.deps import get_active_session_id
from ranker.store import delete_board_session
from ranker.template_env import templates

router = APIRouter(prefix="/collections", tags=["collections"])


@router.get("", response_class=HTMLResponse)
async def list_collections(request: Request) -> HTMLResponse:
    code = await get_library(request)
    boards = []
    if code:
        async with transaction(write=False) as db:
            cursor = await db.execute(
                "SELECT b.session_id,b.name,COUNT(i.id) AS item_count FROM boards b "
                "LEFT JOIN items i ON i.session_id=b.session_id WHERE b.library_id=? "
                "GROUP BY b.session_id ORDER BY b.name,b.session_id",
                (library_id(code),),
            )
            boards = [dict(row) for row in await cursor.fetchall()]
    active_session_id = await get_active_session_id(request)
    return templates.TemplateResponse(
        request,
        "collections.html",
        {
            "boards": boards,
            "active_session_id": active_session_id,
            "recovery_code": code,
        },
    )


@router.post("/create")
async def create_collection(
    request: Request, name: Annotated[str, Form(max_length=100)] = "새 랭킹"
) -> RedirectResponse:
    response = RedirectResponse("/manage", status_code=303)
    await create_board(request, response, name)
    return response


@router.post("/switch")
async def switch_collection(
    request: Request, session_id: Annotated[str, Form()]
) -> RedirectResponse:
    code = get_library_code(request)
    if not code or not await owns_board(code, session_id):
        raise HTTPException(404, "목록에 없는 랭킹입니다.")
    response = RedirectResponse("/ranking", status_code=303)
    set_session_cookie(response, session_id)
    return response


@router.post("/rename")
async def rename_collection(
    request: Request,
    session_id: Annotated[str, Form()],
    name: Annotated[str, Form(min_length=1, max_length=100)],
) -> RedirectResponse:
    code = get_library_code(request)
    if not code:
        raise HTTPException(404, "목록에 없는 랭킹입니다.")
    clean_name = name.strip()
    if not clean_name:
        raise HTTPException(400, "랭킹 이름을 입력하세요.")
    async with transaction() as db:
        cursor = await db.execute(
            "UPDATE boards SET name=? WHERE session_id=? AND library_id=?",
            (clean_name, session_id, library_id(code)),
        )
        if cursor.rowcount != 1:
            raise HTTPException(404, "목록에 없는 랭킹입니다.")
    return RedirectResponse("/collections", status_code=303)


@router.post("/recover")
async def recover_collections(code: Annotated[str, Form()]) -> RedirectResponse:
    code = code.strip()
    if not await library_exists(code):
        raise HTTPException(400, "복구 코드가 맞지 않습니다. 다시 확인하세요.")
    response = RedirectResponse("/collections", status_code=303)
    set_library_cookie(response, code)
    delete_session_cookie(response)
    return response


@router.post("/delete")
async def delete_collection(
    request: Request, session_id: Annotated[str, Form()]
) -> RedirectResponse:
    code = get_library_code(request)
    if not code or not await delete_board_session(session_id, library_id(code)):
        raise HTTPException(404, "목록에 없는 랭킹입니다.")
    response = RedirectResponse("/collections", status_code=303)
    if request.cookies.get("session_id") == session_id:
        delete_session_cookie(response)
    return response
