"""복구 코드로 소유권을 증명하는 개인 랭킹 목록."""

import hashlib
import re
import secrets
import time

from fastapi import Request
from fastapi.responses import Response

from ranker.database import transaction

COOKIE_NAME = "ranker_library"
_CODE = re.compile(r"^[A-Za-z0-9_-]{43}$")


def library_id(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


async def get_library(request: Request, create: bool = False) -> str | None:
    """저장된 코드를 확인하고 요청한 경우에만 새 목록을 만듭니다."""
    code = request.cookies.get(COOKIE_NAME, "")
    async with transaction() as db:
        if _CODE.fullmatch(code):
            cursor = await db.execute(
                "SELECT id FROM libraries WHERE id = ?", (library_id(code),)
            )
            if await cursor.fetchone():
                return code
        if not create:
            return None
        code = secrets.token_urlsafe(32)
        await db.execute(
            "INSERT INTO libraries(id, created_at) VALUES (?, ?)",
            (library_id(code), time.time()),
        )
    return code


def set_library_cookie(response: Response, code: str, secure: bool) -> None:
    response.set_cookie(
        COOKIE_NAME,
        code,
        max_age=365 * 24 * 3600,
        httponly=True,
        secure=secure,
        samesite="strict",
    )


async def attach_board(code: str, session_id: str, name: str) -> None:
    """이미 다른 목록에 속한 세션의 소유권은 변경하지 않습니다."""
    async with transaction() as db:
        await db.execute(
            "INSERT INTO boards(session_id, library_id, name) VALUES (?, ?, ?) ON CONFLICT(session_id) DO NOTHING",
            (session_id, library_id(code), name.strip()[:100] or "새 랭킹"),
        )


async def owns_board(code: str, session_id: str) -> bool:
    async with transaction() as db:
        cursor = await db.execute(
            "SELECT 1 FROM boards WHERE session_id=? AND library_id=?",
            (session_id, library_id(code)),
        )
        return await cursor.fetchone() is not None
