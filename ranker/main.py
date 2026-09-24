# main.py
# 앱 구성, 수명주기, 공통 HTTP 처리.

import asyncio
import logging
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import FastAPI, File, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException

from ranker.boards import create_board
from ranker.cookies import delete_session_cookie, session_cookie_header
from ranker.database import close_db, init_db
from ranker.deps import (
    RequiresSessionException,
    UploadTooLargeError,
    get_session_store,
    import_error_message,
    is_htmx,
    read_backup_upload,
)
from ranker.rating_engine import FitConvergenceError
from ranker.routers import battle, collections, history, manage, ranking
from ranker.schemas import MAX_BACKUP_BYTES
from ranker.store import (
    BackupLimitError,
    BattleItemNotFoundError,
    DataStore,
    InvalidBattleVoteError,
    InvalidSessionDataError,
    SessionSaveError,
    StaleBattleRoundError,
    StaleImportError,
    StaleSessionError,
    cleanup_expired_sessions,
    delete_session,
)
from ranker.template_env import PACKAGE_DIR, templates

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger("ranker")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()

    async def periodic_cleanup():
        while True:
            await asyncio.sleep(3600)
            try:
                await cleanup_expired_sessions()
            except Exception:
                logger.exception("cleanup_failed")

    task = asyncio.create_task(periodic_cleanup())
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
app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")


# --- Errors ---


def wants_json(request: Request) -> bool:
    """HTMX와 스크립트 요청은 JSON, 일반 폼 제출과 페이지 이동은 HTML로 답합니다."""
    return is_htmx(request) or request.headers.get("content-type", "").startswith(
        "application/json"
    )


def error_response(request: Request, status: int, message: str) -> Response:
    if wants_json(request):
        return JSONResponse({"detail": message}, status_code=status)
    return templates.TemplateResponse(
        request, "error.html", {"message": message}, status_code=status
    )


_ERROR_STATUS: dict[type[Exception], int] = {
    BattleItemNotFoundError: 404,
    InvalidBattleVoteError: 422,
    InvalidSessionDataError: 409,
    StaleBattleRoundError: 409,
    StaleImportError: 409,
    StaleSessionError: 409,
    BackupLimitError: 409,
    SessionSaveError: 500,
    FitConvergenceError: 503,
}


def _register_error(exc_type: type[Exception], status: int) -> None:
    @app.exception_handler(exc_type)
    async def handler(request: Request, exc: Exception) -> Response:
        if status >= 500:
            logger.error("%s — path=%s: %s", exc_type.__name__, request.url.path, exc)
        return error_response(request, status, str(exc))


for _exc_type, _status in _ERROR_STATUS.items():
    _register_error(_exc_type, _status)


@app.exception_handler(ValidationError)
@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: Exception) -> Response:
    return error_response(request, 422, "입력값의 길이, 개수 또는 범위를 확인해주세요.")


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(request: Request, exc: StarletteHTTPException):
    return error_response(request, exc.status_code, str(exc.detail))


@app.exception_handler(RequiresSessionException)
async def session_required_handler(request: Request, exc: RequiresSessionException):
    if is_htmx(request):
        return Response(status_code=200, headers={"HX-Redirect": "/"})
    if wants_json(request):
        return JSONResponse(
            {"detail": "열려 있는 랭킹이 없습니다. 내 랭킹에서 다시 열어주세요."},
            status_code=401,
        )
    return RedirectResponse(url="/", status_code=303)


# --- ASGI middleware ---


def _plain_response(status: int, message: str) -> Response:
    return JSONResponse({"detail": message}, status_code=status)


class SecurityMiddleware:
    """외부 출처의 쓰기 요청을 거절하고 보안 헤더를 붙입니다."""

    CSP = (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; font-src 'self'; connect-src 'self'; "
        "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    )

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope["headers"])
        is_static = scope["path"].startswith("/static/")

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                h = MutableHeaders(scope=message)
                h["X-Content-Type-Options"] = "nosniff"
                h["X-Frame-Options"] = "DENY"
                h["Referrer-Policy"] = "strict-origin-when-cross-origin"
                h["Content-Security-Policy"] = self.CSP
                if not is_static:
                    h["Cache-Control"] = "no-store"
            await send(message)

        if scope["method"] not in {"GET", "HEAD", "OPTIONS"} and self._foreign(headers):
            response = _plain_response(403, "이 사이트에서 직접 요청해주세요.")
            return await response(scope, receive, send_with_headers)
        await self.app(scope, receive, send_with_headers)

    @staticmethod
    def _foreign(headers: dict[bytes, bytes]) -> bool:
        # TLS 종료 프록시를 고려해 Origin의 호스트를 실제 요청 Host와 비교합니다.
        if headers.get(b"sec-fetch-site") == b"cross-site":
            return True
        origin = headers.get(b"origin", b"").decode("latin-1")
        if not origin:
            return False
        try:
            parsed = urlsplit(origin)
        except ValueError:
            return True
        host = headers.get(b"host", b"").decode("latin-1").lower()
        return parsed.scheme not in {"http", "https"} or parsed.netloc.lower() != host


class BodyLimitMiddleware:
    """백업 업로드 외의 요청 본문은 1MB로 제한합니다."""

    UPLOAD_PATHS = frozenset({"/upload", "/manage/import"})
    SMALL = 1024 * 1024
    # multipart 경계와 헤더 여유분
    LARGE = MAX_BACKUP_BYTES + 1024 * 1024

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        limit = self.LARGE if scope["path"] in self.UPLOAD_PATHS else self.SMALL
        too_large = _plain_response(413, "요청이 너무 큽니다.")
        length = dict(scope["headers"]).get(b"content-length")
        if length is not None and (not length.isdigit() or int(length) > limit):
            return await too_large(scope, receive, send)

        received = 0
        rejected = False
        started = False

        async def limited_receive():
            nonlocal received, rejected
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    rejected = True
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message):
            nonlocal started
            if rejected:
                return
            started = started or message["type"] == "http.response.start"
            await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except Exception:
            if not rejected:
                raise
        if rejected and not started:
            await too_large(scope, receive, send)


class SessionCookieMiddleware:
    """연 랭킹의 세션 쿠키 만료를 응답마다 연장합니다. DB를 다시 읽지 않습니다."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_with_cookie(message):
            if message["type"] == "http.response.start":
                session_id = scope.get("state", {}).get("session_id")
                headers = MutableHeaders(scope=message)
                replaced = any(
                    value.startswith("session_id=")
                    for value in headers.getlist("set-cookie")
                )
                if session_id and not replaced:
                    headers.append("set-cookie", session_cookie_header(session_id))
            await send(message)

        await self.app(scope, receive, send_with_cookie)


app.add_middleware(SessionCookieMiddleware)
app.add_middleware(BodyLimitMiddleware)
app.add_middleware(SecurityMiddleware)

app.include_router(battle.router)
app.include_router(ranking.router)
app.include_router(manage.router)
app.include_router(collections.router)
app.include_router(history.router)


# --- Pages ---


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    has_session = await get_session_store(request, request.cookies.get("session_id"))
    return templates.TemplateResponse(
        request, "index.html", {"has_session": has_session is not None}
    )


@app.post("/start")
async def start_new_session(request: Request):
    response = RedirectResponse(url="/manage", status_code=303)
    await create_board(request, response, "새 랭킹")
    return response


@app.post("/upload")
async def upload_session(request: Request, file: UploadFile = File(...)):
    """백업 파일로 새 랭킹을 만듭니다."""
    try:
        raw = await read_backup_upload(file)
        DataStore.parse_import(raw)  # 잘못된 파일이면 랭킹을 만들지 않습니다.
    except UploadTooLargeError as exc:
        return error_response(request, 413, str(exc))
    except (ValueError, ValidationError) as exc:
        return error_response(request, 400, import_error_message(exc))
    response = RedirectResponse(url="/battle", status_code=303)
    store = await create_board(request, response, "가져온 랭킹")
    await store.import_json(raw)
    return response


@app.post("/end-session")
async def end_session(request: Request):
    """현재 랭킹을 삭제하고 쿠키를 지웁니다."""
    store = await get_session_store(request, request.cookies.get("session_id"))
    if store:
        await delete_session(store.session_id)
        request.state.session_id = None
    response = RedirectResponse(url="/", status_code=303)
    delete_session_cookie(response)
    return response
