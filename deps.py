# deps.py
# FastAPI 의존성 — 세션 쿠키에서 DataStore를 주입합니다.
# 세션이 없으면 인덱스(업로드) 페이지로 리다이렉트합니다.

import re
import uuid

from fastapi import Cookie, Request

from store import DataStore, InvalidSessionDataError, delete_session, get_store, session_exists

_SESSION_ID_RE = re.compile(r'^[0-9a-f]{32}$')


class RequiresSessionException(Exception):
    """세션이 없거나 유효하지 않을 때 발생합니다. main.py의 핸들러가 / 로 리다이렉트합니다."""


def create_session_id() -> str:
    """새 세션 ID를 생성합니다."""
    return uuid.uuid4().hex


def _is_valid_session_id(session_id: str) -> bool:
    return bool(_SESSION_ID_RE.fullmatch(session_id))


async def get_session_store(
    request: Request,
    session_id: str | None = Cookie(default=None),
) -> DataStore | None:
    """
    쿠키의 session_id로 DataStore를 반환합니다.
    세션이 없거나 유효하지 않으면 None을 반환합니다.
    JSON 응답이 필요한 엔드포인트(예: /battle/vote)에서 사용합니다.
    """
    if not session_id or not _is_valid_session_id(session_id) or not session_exists(session_id):
        return None
    try:
        return await get_store(session_id)
    except InvalidSessionDataError:
        delete_session(session_id)
        return None


async def require_store(
    request: Request,
    session_id: str | None = Cookie(default=None),
) -> DataStore:
    """
    세션이 없으면 RequiresSessionException을 발생시킵니다.
    HTML을 반환하는 라우터 엔드포인트에서 Depends(require_store)로 사용합니다.
    """
    if not session_id or not _is_valid_session_id(session_id) or not session_exists(session_id):
        raise RequiresSessionException()
    try:
        return await get_store(session_id)
    except InvalidSessionDataError:
        delete_session(session_id)
        raise RequiresSessionException() from None
