# main.py
# 세션 기반 멀티유저 Ranker 웹앱 엔트리포인트.
# 각 사용자는 JSON 파일을 업로드하거나 새 세션을 시작하여 독립적으로 사용합니다.

import json

from fastapi import FastAPI, Request, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from deps import create_session_id, get_session_store
from store import get_store, session_exists
from routers import battle, ranking, manage

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

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
        httponly=True, samesite="lax",
    )
    return response


@app.post("/upload")
async def upload_session(file: UploadFile = File(...)):
    """JSON 파일을 업로드하여 새 세션을 생성합니다."""
    sid = create_session_id()
    store = await get_store(sid)

    raw = await file.read()
    try:
        await store.import_json(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return HTMLResponse("유효하지 않은 JSON 파일입니다.", status_code=400)

    response = RedirectResponse(url="/battle", status_code=303)
    response.set_cookie(
        key="session_id", value=sid,
        max_age=7 * 24 * 60 * 60,
        httponly=True, samesite="lax",
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
    response.delete_cookie("session_id")
    return response
