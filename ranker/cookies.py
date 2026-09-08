"""세션 쿠키 정책과 발급."""

import os

from fastapi.responses import Response

from ranker.store import SESSION_TTL_SECONDS


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


COOKIE_SECURE = _env_flag("COOKIE_SECURE", False)


def set_session_cookie(response: Response, session_id: str) -> None:
    response.set_cookie(
        key="session_id",
        value=session_id,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="strict",
        secure=COOKIE_SECURE,
    )
