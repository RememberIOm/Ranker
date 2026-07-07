# main.py
# 세션 기반 멀티유저 Ranker 웹앱 엔트리포인트.
# 각 사용자는 JSON 파일을 업로드하거나 새 세션을 시작하여 독립적으로 사용합니다.

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from database import init_db, close_db, migrate_json_sessions
from deps import (
    RequiresSessionException,
    create_session_id,
    get_session_store,
    import_json_upload,
)
from store import (
    SESSION_TTL_SECONDS,
    SessionSaveError,
    cleanup_expired_sessions,
    get_store,
    session_exists,
)
from routers import battle, ranking, manage
from template_env import templates


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


COOKIE_SECURE = _env_flag("COOKIE_SECURE", False)


def _set_session_cookie(response: Response, session_id: str) -> None:
    response.set_cookie(
        key="session_id", value=session_id,
        max_age=SESSION_TTL_SECONDS,
        httponly=True, samesite="strict",
        secure=COOKIE_SECURE,
    )

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger("ranker")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """앱 시작 시 DB 초기화, JSON 마이그레이션, 만료 세션 주기적 정리 태스크를 수행합니다."""
    _log = logging.getLogger("ranker.lifespan")

    await init_db()

    # 기존 JSON 세션 파일 자동 마이그레이션
    session_dir = Path(os.getenv("SESSION_DIR", "./data/sessions"))
    if session_dir.exists():
        migrated = await migrate_json_sessions(session_dir)
        if migrated:
            _log.info("json_migration_done — migrated %d sessions", migrated)

    async def _periodic_cleanup():
        while True:
            try:
                await asyncio.sleep(3600)  # 1시간마다
                removed = await cleanup_expired_sessions()
                _log.info("cleanup_done — removed %d expired sessions", removed)
            except asyncio.CancelledError:
                _log.info("cleanup_cancelled")
                raise
            except Exception:
                _log.exception("cleanup_failed")

    task = asyncio.create_task(_periodic_cleanup())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await close_db()


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.exception_handler(RequiresSessionException)
async def session_exception_handler(request: Request, exc: RequiresSessionException):
    return RedirectResponse(url="/", status_code=303)


@app.exception_handler(SessionSaveError)
async def session_save_error_handler(request: Request, exc: SessionSaveError):
    logger.error("session_save_failed — path=%s: %s", request.url.path, exc)
    return HTMLResponse("세션 저장에 실패했습니다. 잠시 후 다시 시도해주세요.", status_code=500)


class SessionCookieRefreshMiddleware(BaseHTTPMiddleware):
    """유효한 세션 쿠키를 모든 응답에서 갱신하여 활성 사용자의 세션이 만료되지 않도록 합니다.

    /static/* 경로는 세션과 무관하므로 제외 — 불필요한 디스크 stat과 Set-Cookie 헤더를 피함.
    """

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        # static 자원 요청은 세션과 무관 — 쿠키 갱신 스킵 (디스크 stat 절감)
        if request.url.path.startswith("/static/"):
            return response
        session_id = request.cookies.get("session_id")
        if session_id and await session_exists(session_id):
            _set_session_cookie(response, session_id)
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "img-src 'self' data:; "
            "font-src 'self' data: https://fonts.gstatic.com; "
            "connect-src 'self';"
        )
        return response


app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(SessionCookieRefreshMiddleware)

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
    has_session = bool(session_id and await get_session_store(request, session_id))
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
    _set_session_cookie(response, sid)
    return response


@app.post("/upload")
async def upload_session(file: UploadFile = File(...)):
    """JSON 파일을 업로드하여 새 세션을 생성합니다."""
    sid = create_session_id()
    store = await get_store(sid)

    error = await import_json_upload(file, store)
    if error:
        return error

    response = RedirectResponse(url="/battle", status_code=303)
    _set_session_cookie(response, sid)
    return response


@app.post("/end-session")
async def end_session(request: Request):
    """현재 세션을 종료하고 쿠키를 삭제합니다."""
    session_id = request.cookies.get("session_id")
    if session_id:
        store = await get_session_store(request, session_id)
        if store:
            await store.delete_session()

    response = RedirectResponse(url="/", status_code=303)
    response.delete_cookie("session_id", httponly=True, samesite="strict", secure=COOKIE_SECURE)
    return response
