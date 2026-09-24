"""복구 코드로 소유권을 증명하는 개인 랭킹 목록."""

import hashlib
import re
import secrets
import time

from fastapi import Request
from fastapi.responses import Response

from ranker.cookies import COOKIE_SECURE, set_session_cookie
from ranker.database import transaction
from ranker.deps import create_session_id
from ranker.store import DataStore, create_store

COOKIE_NAME = "ranker_library"
CODE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")


def library_id(code: str) -> str:
    """서버에는 복구 코드의 해시만 저장합니다."""
    return hashlib.sha256(code.encode()).hexdigest()


async def library_exists(code: str) -> bool:
    if not CODE_PATTERN.fullmatch(code):
        return False
    async with transaction() as db:
        cursor = await db.execute(
            "SELECT 1 FROM libraries WHERE id = ?", (library_id(code),)
        )
        return await cursor.fetchone() is not None


async def get_library(request: Request) -> str | None:
    """쿠키의 복구 코드가 유효하면 반환합니다. 새 목록은 만들지 않습니다."""
    code = request.cookies.get(COOKIE_NAME, "")
    return code if await library_exists(code) else None


async def ensure_library(request: Request) -> str:
    """쿠키의 목록을 쓰고, 없으면 새 복구 코드로 목록을 만듭니다."""
    code = await get_library(request)
    if code:
        return code
    code = secrets.token_urlsafe(32)
    async with transaction() as db:
        await db.execute(
            "INSERT INTO libraries(id, created_at) VALUES (?, ?)",
            (library_id(code), time.time()),
        )
    return code


def set_library_cookie(response: Response, code: str) -> None:
    response.set_cookie(
        COOKIE_NAME,
        code,
        max_age=365 * 24 * 3600,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="strict",
    )


async def attach_board(code: str, session_id: str, name: str) -> None:
    """이미 다른 목록에 속한 랭킹의 소유권은 바꾸지 않습니다."""
    async with transaction() as db:
        await db.execute(
            "INSERT INTO boards(session_id, library_id, name) VALUES (?, ?, ?) "
            "ON CONFLICT(session_id) DO NOTHING",
            (session_id, library_id(code), name.strip()[:100] or "새 랭킹"),
        )


async def create_board(request: Request, response: Response, name: str) -> DataStore:
    """새 랭킹을 만들어 내 목록에 넣고, 그 랭킹을 현재 랭킹으로 엽니다."""
    code = await ensure_library(request)
    store = await create_store(create_session_id())
    await attach_board(code, store.session_id, name)
    set_session_cookie(response, store.session_id)
    set_library_cookie(response, code)
    return store


async def owns_board(code: str, session_id: str) -> bool:
    async with transaction() as db:
        cursor = await db.execute(
            "SELECT 1 FROM boards WHERE session_id=? AND library_id=?",
            (session_id, library_id(code)),
        )
        return await cursor.fetchone() is not None


async def board_library(session_id: str) -> str | None:
    async with transaction() as db:
        cursor = await db.execute(
            "SELECT library_id FROM boards WHERE session_id=?", (session_id,)
        )
        row = await cursor.fetchone()
    return row["library_id"] if row else None
