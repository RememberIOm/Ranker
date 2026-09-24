# deps.py
# FastAPI 의존성 — 세션 쿠키에서 DataStore를 주입합니다.

import re
import uuid

from fastapi import Cookie, Request, UploadFile

from ranker.schemas import MAX_BACKUP_BYTES
from ranker.store import DataStore, InvalidSessionDataError, open_store

_SESSION_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


class RequiresSessionException(Exception):
    """세션이 없거나 읽을 수 없습니다. main.py의 핸들러가 / 로 보냅니다."""


class UploadTooLargeError(ValueError):
    """백업 파일이 한도를 넘습니다."""


def create_session_id() -> str:
    return uuid.uuid4().hex


async def get_session_store(
    request: Request,
    session_id: str | None = Cookie(default=None),
) -> DataStore | None:
    """쿠키의 랭킹을 엽니다. 없거나 손상되었으면 None입니다.

    연 랭킹은 요청 상태에 기록해 응답에서 세션 쿠키 만료를 연장합니다.
    """
    if not session_id or not _SESSION_ID_RE.fullmatch(session_id):
        return None
    try:
        store = await open_store(session_id)
    except InvalidSessionDataError:
        return None
    if store is not None:
        request.state.session_id = session_id
    return store


async def require_store(
    request: Request,
    session_id: str | None = Cookie(default=None),
) -> DataStore:
    store = await get_session_store(request, session_id)
    if store is None:
        raise RequiresSessionException()
    return store


async def read_backup_upload(file: UploadFile) -> str:
    """UTF-8 백업 파일을 한도 안에서 읽습니다."""
    raw = await file.read(MAX_BACKUP_BYTES + 1)
    if len(raw) > MAX_BACKUP_BYTES:
        raise UploadTooLargeError("백업 파일은 64MB를 넘을 수 없습니다.")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InvalidSessionDataError("UTF-8로 저장된 JSON 파일이 필요합니다.") from exc


def import_error_message(exc: Exception) -> str:
    """검증 실패 원인 중 사용자가 고칠 수 있는 설명만 보여줍니다."""
    if isinstance(exc, InvalidSessionDataError):
        return str(exc)
    return (
        "백업 파일을 읽을 수 없습니다. Ranker에서 내보낸 형식 4 JSON인지 확인해주세요."
    )
