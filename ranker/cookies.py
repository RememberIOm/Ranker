"""세션 쿠키 정책과 발급."""

import os

from fastapi.responses import Response


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


COOKIE_SECURE = _env_flag("COOKIE_SECURE", False)
# 지금 연 랭킹을 기억하는 기간입니다. 랭킹 자체는 목록에 남습니다.
SESSION_COOKIE_MAX_AGE = 7 * 24 * 3600


def set_session_cookie(response: Response, session_id: str) -> None:
    response.set_cookie(
        key="session_id",
        value=session_id,
        max_age=SESSION_COOKIE_MAX_AGE,
        httponly=True,
        samesite="strict",
        secure=COOKIE_SECURE,
    )


def delete_session_cookie(response: Response) -> None:
    response.delete_cookie(
        "session_id", httponly=True, samesite="strict", secure=COOKIE_SECURE
    )


def session_cookie_header(session_id: str) -> str:
    response = Response()
    set_session_cookie(response, session_id)
    return response.headers["set-cookie"]
