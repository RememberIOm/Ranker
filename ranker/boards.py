"""복구 코드로 소유권을 증명하는 개인 랭킹 목록."""

import hashlib
import re
import secrets

from fastapi import Request
from fastapi.responses import Response

from ranker.cookies import COOKIE_SECURE, set_session_cookie
from ranker.database import transaction
from ranker.deps import create_session_id
from ranker.store import DataStore, create_store

COOKIE_NAME = "ranker_library"
CODE_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}")


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


async def library_code(request: Request) -> str:
    """쿠키의 목록 코드를 쓰고, 없으면 새 복구 코드를 만듭니다.

    새 목록은 첫 랭킹과 같은 트랜잭션에서 저장합니다.
    """
    return await get_library(request) or secrets.token_urlsafe(32)


def set_library_cookie(response: Response, code: str) -> None:
    response.set_cookie(
        COOKIE_NAME,
        code,
        max_age=365 * 24 * 3600,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="strict",
    )


async def create_board(request: Request, response: Response, name: str) -> DataStore:
    """새 랭킹을 만들어 내 목록에 넣고, 그 랭킹을 현재 랭킹으로 엽니다."""
    code = await library_code(request)
    store = await create_store(
        create_session_id(), library_id(code), name.strip()[:100] or "새 랭킹"
    )
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
