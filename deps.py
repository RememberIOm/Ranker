# deps.py
# FastAPI 의존성 — 세션 쿠키에서 DataStore를 주입합니다.
# 세션이 없으면 인덱스(업로드) 페이지로 리다이렉트합니다.

import uuid

from fastapi import Cookie, Request

from store import DataStore, get_store, session_exists


def create_session_id() -> str:
    """새 세션 ID를 생성합니다."""
    return uuid.uuid4().hex


async def get_session_store(
    request: Request,
    session_id: str | None = Cookie(default=None),
) -> DataStore | None:
    """
    쿠키의 session_id로 DataStore를 반환합니다.
    세션이 없거나 유효하지 않으면 None을 반환합니다.
    """
    if not session_id or not session_exists(session_id):
        return None
    return await get_store(session_id)
