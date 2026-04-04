# routers/manage.py
# 관리 페이지: 항목 CRUD, 대량 추가, 평가 기준 편집, Elo 설정, JSON Import/Export
# 세션별 DataStore를 사용합니다.

import json
import re
import hashlib
import unicodedata

from fastapi import APIRouter, Request, Form, UploadFile, File, Cookie
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from deps import get_session_store

router = APIRouter(prefix="/manage", tags=["manage"])
templates = Jinja2Templates(directory="templates")


@router.get("", response_class=HTMLResponse)
async def manage_page(request: Request, tab: str = "items", session_id: str | None = Cookie(default=None)):
    store = await get_session_store(request, session_id)
    if not store:
        return RedirectResponse(url="/", status_code=303)

    sorted_items = sorted(store.items, key=lambda x: x["name"])
    return templates.TemplateResponse(request, "manage.html", {
        "items": sorted_items,
        "criteria": store.criteria,
        "settings": store.settings,
        "tab": tab,
    })


# --- Items ---


@router.post("/add")
async def add_item(name: str = Form(...), session_id: str | None = Cookie(default=None), request: Request = None):
    store = await get_session_store(request, session_id)
    if not store:
        return RedirectResponse(url="/", status_code=303)
    if name.strip():
        await store.add_item(name)
    return RedirectResponse(url="/manage?tab=items", status_code=303)


@router.post("/add-bulk")
async def add_items_bulk(names: str = Form(...), session_id: str | None = Cookie(default=None), request: Request = None):
    """줄바꿈으로 구분된 이름 목록을 한번에 추가합니다."""
    store = await get_session_store(request, session_id)
    if not store:
        return RedirectResponse(url="/", status_code=303)
    name_list = [n.strip() for n in names.splitlines() if n.strip()]
    await store.add_items_bulk(name_list)
    return RedirectResponse(url="/manage?tab=items", status_code=303)


@router.post("/delete")
async def delete_item(
    item_id: int = Form(...),
    redirect_url: str = Form("/manage?tab=items"),
    session_id: str | None = Cookie(default=None),
    request: Request = None,
):
    store = await get_session_store(request, session_id)
    if not store:
        return RedirectResponse(url="/", status_code=303)
    await store.delete_item(item_id)
    return RedirectResponse(url=redirect_url, status_code=303)


@router.post("/edit")
async def edit_item(
    item_id: int = Form(...),
    new_name: str = Form(...),
    redirect_url: str = Form("/manage?tab=items"),
    session_id: str | None = Cookie(default=None),
    request: Request = None,
):
    store = await get_session_store(request, session_id)
    if not store:
        return RedirectResponse(url="/", status_code=303)
    if new_name.strip():
        await store.update_item(item_id, name=new_name.strip())
    return RedirectResponse(url=redirect_url, status_code=303)


# --- Criteria ---


@router.post("/criteria")
async def update_criteria(request: Request, session_id: str | None = Cookie(default=None)):
    """평가 기준을 폼 데이터로 일괄 교체합니다. key가 비어있으면 자동 생성합니다."""
    store = await get_session_store(request, session_id)
    if not store:
        return RedirectResponse(url="/", status_code=303)

    form = await request.form()

    keys = form.getlist("key")
    labels = form.getlist("label")
    colors = form.getlist("color")
    weights = form.getlist("weight")

    used_keys: set[str] = set()
    new_criteria = []

    for raw_key, lbl, clr, w in zip(keys, labels, colors, weights):
        lbl = lbl.strip()
        if not lbl:
            continue

        key = raw_key.strip()
        if not key:
            key = _generate_key(lbl, used_keys)

        used_keys.add(key)
        new_criteria.append({
            "key": key,
            "label": lbl,
            "color": clr.strip() or "gray",
            "weight": float(w) if w else 1.0,
        })

    await store.set_criteria(new_criteria)
    return RedirectResponse(url="/manage?tab=criteria", status_code=303)


def _generate_key(label: str, existing: set[str]) -> str:
    """label로부터 안전한 key를 생성합니다. 충돌 시 숫자 접미사를 추가합니다."""
    ascii_label = unicodedata.normalize("NFKD", label).encode("ascii", "ignore").decode()
    base = re.sub(r"[^a-z0-9]+", "_", ascii_label.lower()).strip("_")

    if not base:
        base = "c_" + hashlib.md5(label.encode()).hexdigest()[:6]

    key = base
    counter = 2
    while key in existing:
        key = f"{base}_{counter}"
        counter += 1
    return key


# --- Settings ---


@router.post("/settings")
async def update_settings(request: Request, session_id: str | None = Cookie(default=None)):
    store = await get_session_store(request, session_id)
    if not store:
        return RedirectResponse(url="/", status_code=303)

    form = await request.form()
    patch: dict = {}

    int_fields = {"elo_k_max", "elo_k_min", "elo_decay_factor"}
    float_fields = {
        "elo_draw_max", "elo_draw_scale",
        "initial_rating", "normalize_target", "normalize_threshold",
        "result_skip_seconds",
    }
    bool_fields = {"result_auto_skip"}

    for key in int_fields:
        val = form.get(key)
        if val is not None and val != "":
            patch[key] = int(val)

    for key in float_fields:
        val = form.get(key)
        if val is not None and val != "":
            patch[key] = float(val)

    for key in bool_fields:
        patch[key] = key in form

    await store.update_settings(patch)
    return RedirectResponse(url="/manage?tab=settings", status_code=303)


# --- Import / Export ---


@router.get("/export")
async def export_data(session_id: str | None = Cookie(default=None), request: Request = None):
    """전체 데이터를 JSON 파일로 다운로드합니다."""
    store = await get_session_store(request, session_id)
    if not store:
        return RedirectResponse(url="/", status_code=303)
    return Response(
        content=store.export_json(),
        media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=ranker_data.json"},
    )


@router.post("/import")
async def import_data(
    file: UploadFile = File(...),
    session_id: str | None = Cookie(default=None),
    request: Request = None,
):
    """업로드된 JSON 파일로 전체 데이터를 교체합니다."""
    store = await get_session_store(request, session_id)
    if not store:
        return RedirectResponse(url="/", status_code=303)
    raw = await file.read()
    try:
        await store.import_json(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return HTMLResponse("유효하지 않은 JSON 파일입니다.", status_code=400)
    return RedirectResponse(url="/manage?tab=data", status_code=303)
