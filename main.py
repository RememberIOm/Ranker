# main.py
# 세션 기반 멀티유저 Ranker 웹앱 엔트리포인트.
# 각 사용자는 JSON 파일을 업로드하거나 새 세션을 시작하여 독립적으로 사용합니다.

import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from starlette.middleware.base import BaseHTTPMiddleware

from deps import create_session_id, get_session_store, RequiresSessionException
from store import get_store, session_exists, cleanup_expired_sessions
from routers import battle, ranking, manage
from template_env import templates


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


COOKIE_SECURE = _env_flag("COOKIE_SECURE", False)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """앱 시작 시 만료 세션 주기적 정리 태스크를 스폰합니다."""
    async def _periodic_cleanup():
        while True:
            await asyncio.sleep(3600)  # 1시간마다
            await cleanup_expired_sessions()

    task = asyncio.create_task(_periodic_cleanup())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.exception_handler(RequiresSessionException)
async def session_exception_handler(request: Request, exc: RequiresSessionException):
    return RedirectResponse(url="/", status_code=303)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "img-src 'self' data:; "
            "font-src 'self' data: https://fonts.gstatic.com; "
            "connect-src 'self';"
        )
        return response


app.add_middleware(SecurityHeadersMiddleware)

# 라우터 등록
app.include_router(battle.router)
app.include_router(ranking.router)
app.include_router(manage.router)


@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    """
    인덱스 페이지: 세션이 이미 있으면 메인 화면, 없으면 업로드/시작 화면을 표시합니다.
    """
    session_id = request.cookies.get("session_id")
    has_session = bool(session_id and session_exists(session_id))
    return templates.TemplateResponse(request, "index.html", {
        "has_session": has_session,
    })


@app.post("/start")
async def start_new_session():
    """새 세션(빈 데이터)을 생성하고 쿠키를 설정합니다."""
    sid = create_session_id()
    store = await get_store(sid)  # 기본 데이터로 초기화
    await store.save()

    response = RedirectResponse(url="/manage", status_code=303)
    response.set_cookie(
        key="session_id", value=sid,
        max_age=7 * 24 * 60 * 60,  # 7일
        httponly=True, samesite="strict",
        secure=COOKIE_SECURE,
    )
    return response


_MAX_UPLOAD_BYTES = 1_000_000  # 1 MB


@app.post("/upload")
async def upload_session(file: UploadFile = File(...)):
    """JSON 파일을 업로드하여 새 세션을 생성합니다."""
    sid = create_session_id()
    store = await get_store(sid)

    raw = await file.read(_MAX_UPLOAD_BYTES + 1)
    if len(raw) > _MAX_UPLOAD_BYTES:
        return HTMLResponse("파일 크기는 1MB를 초과할 수 없습니다.", status_code=413)
    try:
        await store.import_json(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValidationError, ValueError):
        return HTMLResponse("유효하지 않은 JSON 파일입니다.", status_code=400)

    response = RedirectResponse(url="/battle", status_code=303)
    response.set_cookie(
        key="session_id", value=sid,
        max_age=7 * 24 * 60 * 60,
        httponly=True, samesite="strict",
        secure=COOKIE_SECURE,
    )
    return response


@app.post("/end-session")
async def end_session(request: Request):
    """현재 세션을 종료하고 쿠키를 삭제합니다."""
    session_id = request.cookies.get("session_id")
    if session_id:
        store = await get_session_store(request, session_id)
        if store:
            store.delete_session()

    response = RedirectResponse(url="/", status_code=303)
    response.delete_cookie("session_id", httponly=True, samesite="strict", secure=COOKIE_SECURE)
    return response
