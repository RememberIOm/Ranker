"""이름 있는 랭킹의 생성, 전환, 복구."""

import re

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from boards import attach_board, get_library, library_id, owns_board, set_library_cookie
from database import transaction
from deps import create_session_id, get_session_store
from store import get_store
from template_env import templates

router = APIRouter(prefix="/collections", tags=["collections"])


@router.get("", response_class=HTMLResponse)
async def list_collections(request: Request) -> HTMLResponse:
    from main import COOKIE_SECURE

    code = await get_library(request, create=True)
    sid = request.cookies.get("session_id")
    if sid and await get_session_store(request, sid):
        await attach_board(code, sid, "기존 랭킹")
    async with transaction() as db:
        cursor = await db.execute(
            "SELECT b.session_id,b.name,COUNT(i.id) AS item_count FROM boards b "
            "LEFT JOIN items i ON i.session_id=b.session_id WHERE b.library_id=? "
            "GROUP BY b.session_id ORDER BY b.name,b.session_id",
            (library_id(code),),
        )
        boards = [dict(row) for row in await cursor.fetchall()]
    response = templates.TemplateResponse(
        request,
        "collections.html",
        {
            "boards": boards,
            "active_session_id": sid,
            "recovery_code": code,
        },
    )
    set_library_cookie(response, code, COOKIE_SECURE)
    return response


@router.post("/create")
async def create_collection(
    request: Request, name: str = Form("새 랭킹", max_length=100)
) -> RedirectResponse:
    from main import COOKIE_SECURE, _set_session_cookie

    code = await get_library(request, create=True)
    sid = create_session_id()
    store = await get_store(sid)
    await store.save()
    await attach_board(code, sid, name)
    response = RedirectResponse("/manage", status_code=303)
    _set_session_cookie(response, sid)
    set_library_cookie(response, code, COOKIE_SECURE)
    return response


@router.post("/switch")
async def switch_collection(
    request: Request, session_id: str = Form(...)
) -> RedirectResponse:
    from main import _set_session_cookie

    code = await get_library(request)
    if not code or not await owns_board(code, session_id):
        raise HTTPException(404, "랭킹을 찾을 수 없습니다.")
    response = RedirectResponse("/ranking", status_code=303)
    _set_session_cookie(response, session_id)
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
            raise HTTPException(404, "랭킹을 찾을 수 없습니다.")
    return RedirectResponse("/collections", status_code=303)


@router.post("/recover")
async def recover_collections(code: str = Form(...)) -> RedirectResponse:
    from main import COOKIE_SECURE

    code = code.strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", code):
        raise HTTPException(400, "복구 코드를 확인해주세요.")
    async with transaction() as db:
        cursor = await db.execute(
            "SELECT id FROM libraries WHERE id=?", (library_id(code),)
        )
        if not await cursor.fetchone():
            raise HTTPException(400, "복구 코드를 확인해주세요.")
    response = RedirectResponse("/collections", status_code=303)
    set_library_cookie(response, code, COOKIE_SECURE)
    response.delete_cookie(
        "session_id", httponly=True, samesite="strict", secure=COOKIE_SECURE
    )
    return response


@router.post("/delete")
async def delete_collection(
    request: Request, session_id: str = Form(...)
) -> RedirectResponse:
    from main import COOKIE_SECURE
    from store import delete_session

    code = await get_library(request)
    if not code or not await owns_board(code, session_id):
        raise HTTPException(404, "랭킹을 찾을 수 없습니다.")
    await delete_session(session_id)
    response = RedirectResponse("/collections", status_code=303)
    if request.cookies.get("session_id") == session_id:
        response.delete_cookie(
            "session_id", httponly=True, samesite="strict", secure=COOKIE_SECURE
        )
    return response
