# deps.py
# FastAPI 의존성 — 세션 쿠키에서 DataStore를 주입합니다.
# 세션이 없으면 인덱스(업로드) 페이지로 리다이렉트합니다.

import re
import uuid

from fastapi import Cookie, Request, UploadFile
from fastapi.responses import HTMLResponse
from pydantic import ValidationError

from store import DataStore, InvalidSessionDataError, get_store, session_exists

_SESSION_ID_RE = re.compile(r'^[0-9a-f]{32}$')


def is_htmx(request: Request) -> bool:
    """HTMX 요청 여부를 판단합니다."""
    return request.headers.get("HX-Request") == "true"


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
    파일 로드 오류는 절대 파일을 삭제하지 않습니다 — 사용자 데이터 보호.
    """
    if not session_id or not _is_valid_session_id(session_id) or not await session_exists(session_id):
        return None
    try:
        return await get_store(session_id)
    except InvalidSessionDataError:
        return None


async def require_store(
    request: Request,
    session_id: str | None = Cookie(default=None),
) -> DataStore:
    """
    세션이 없으면 RequiresSessionException을 발생시킵니다.
    HTML을 반환하는 라우터 엔드포인트에서 Depends(require_store)로 사용합니다.
    """
    store = await get_session_store(request, session_id)
    if store is None:
        raise RequiresSessionException()
    return store


_MAX_UPLOAD_BYTES = 1_000_000  # 1 MB


async def import_json_upload(file: UploadFile, store: DataStore) -> HTMLResponse | None:
    """업로드된 JSON 파일을 store로 import합니다. 실패 시 에러 응답, 성공 시 None."""
    raw = await file.read(_MAX_UPLOAD_BYTES + 1)
    if len(raw) > _MAX_UPLOAD_BYTES:
        return HTMLResponse("파일 크기는 1MB를 초과할 수 없습니다.", status_code=413)
    try:
        await store.import_json(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValidationError, ValueError):
        return HTMLResponse("유효하지 않은 JSON 파일입니다.", status_code=400)
    return None
