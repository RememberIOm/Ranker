# routers/manage.py
# 관리 페이지: 항목 CRUD, 대량 추가, 평가 기준 편집, Elo 설정, JSON Import/Export
# 세션별 DataStore를 사용합니다.

import logging
import re
import hashlib
import unicodedata

from fastapi import APIRouter, Request, Form, UploadFile, File, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import ValidationError

from deps import require_store
from schemas import SettingsModel
from store import DataStore
from template_env import templates

logger = logging.getLogger("ranker.manage")

router = APIRouter(prefix="/manage", tags=["manage"])

_SETTINGS_BOUNDS: dict[str, tuple] = {
    "initial_sigma": (0.1, 10.0),
    "draw_prior_max": (0.0, 1.0),
    "draw_prior_strength": (1, 1000),
    "draw_bandwidth": (0.1, 10.0),
    "hierarchical_strength": (0.0, 100.0),
    "display_center": (0.0, 100000.0),
    "display_scale": (1.0, 10000.0),
    "result_skip_seconds": (0.5, 60.0),
}


def _safe_redirect(url: str, fallback: str) -> str:
    """외부 URL로의 오픈 리다이렉트를 방지합니다. 상대 경로만 허용합니다."""
    if url.startswith("/") and not url.startswith("//"):
        return url
    return fallback


_VALID_TABS = {"items", "criteria", "settings", "data"}


@router.get("", response_class=HTMLResponse)
async def manage_page(request: Request, tab: str = "items", store: DataStore = Depends(require_store)):
    if tab not in _VALID_TABS:
        tab = "items"

    sorted_items = sorted(store.items, key=lambda x: x["name"])
    return templates.TemplateResponse(request, "manage.html", {
        "items": sorted_items,
        "criteria": store.criteria,
        "settings": store.settings,
        "tab": tab,
    })


# --- Items ---


@router.post("/add")
async def add_item(name: str = Form(...), store: DataStore = Depends(require_store)):
    if name.strip():
        await store.add_item(name)
    return RedirectResponse(url="/manage?tab=items", status_code=303)


@router.post("/add-bulk")
async def add_items_bulk(names: str = Form(...), store: DataStore = Depends(require_store)):
    """줄바꿈으로 구분된 이름 목록을 한번에 추가합니다."""
    name_list = [n.strip() for n in names.splitlines() if n.strip()]
    await store.add_items_bulk(name_list)
    return RedirectResponse(url="/manage?tab=items", status_code=303)


@router.post("/delete")
async def delete_item(
    item_id: int = Form(...),
    redirect_url: str = Form("/manage?tab=items"),
    store: DataStore = Depends(require_store),
):
    await store.delete_item(item_id)
    return RedirectResponse(url=_safe_redirect(redirect_url, "/manage?tab=items"), status_code=303)


@router.post("/edit")
async def edit_item(
    item_id: int = Form(...),
    new_name: str = Form(...),
    redirect_url: str = Form("/manage?tab=items"),
    store: DataStore = Depends(require_store),
):
    if new_name.strip():
        await store.update_item(item_id, name=new_name.strip())
    return RedirectResponse(url=_safe_redirect(redirect_url, "/manage?tab=items"), status_code=303)


# --- Criteria ---


@router.post("/criteria")
async def update_criteria(request: Request, store: DataStore = Depends(require_store)):
    """평가 기준을 폼 데이터로 일괄 교체합니다. key가 비어있으면 자동 생성합니다."""

    form = await request.form()

    keys = form.getlist("key")
    labels = form.getlist("label")
    colors = form.getlist("color")
    weights = form.getlist("weight")

    if not (len(keys) == len(labels) == len(colors) == len(weights)):
        return HTMLResponse("폼 데이터가 올바르지 않습니다.", status_code=400)

    used_keys: set[str] = set()
    new_criteria = []

    for raw_key, lbl, clr, w in zip(keys, labels, colors, weights):
        lbl = lbl.strip()
        if not lbl:
            continue

        key = raw_key.strip()
        if not key:
            key = _generate_key(lbl, used_keys)

        try:
            weight_val = float(w) if w else 1.0
        except ValueError:
            return HTMLResponse(f"'{lbl}'의 가중치는 숫자여야 합니다.", status_code=400)

        used_keys.add(key)
        new_criteria.append({
            "key": key,
            "label": lbl,
            "color": clr.strip() or "gray",
            "weight": weight_val,
        })

    try:
        await store.set_criteria(new_criteria)
    except ValidationError as exc:
        return HTMLResponse(f"기준 저장 실패: {exc.errors()[0]['msg']}", status_code=400)
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
async def update_settings(request: Request, store: DataStore = Depends(require_store)):

    form = await request.form()
    patch: dict = {}

    int_fields = {"draw_prior_strength"}
    float_fields = {
        "initial_sigma", "draw_prior_max", "draw_bandwidth",
        "hierarchical_strength", "display_center", "display_scale",
        "result_skip_seconds",
    }
    bool_fields = {"result_auto_skip"}

    for key in int_fields:
        val = form.get(key)
        if val is not None and val != "":
            parsed = int(val)
            if key in _SETTINGS_BOUNDS:
                lo, hi = _SETTINGS_BOUNDS[key]
                parsed = max(lo, min(hi, parsed))
            patch[key] = parsed

    for key in float_fields:
        val = form.get(key)
        if val is not None and val != "":
            parsed = float(val)
            if key in _SETTINGS_BOUNDS:
                lo, hi = _SETTINGS_BOUNDS[key]
                parsed = max(lo, min(hi, parsed))
            patch[key] = parsed

    for key in bool_fields:
        patch[key] = key in form

    # Pydantic 모델로 재검증
    merged = {**store.settings, **patch}
    try:
        validated = SettingsModel(**merged).model_dump(mode="python")
    except ValidationError as exc:
        return HTMLResponse(f"설정 값이 올바르지 않습니다: {exc.errors()[0]['msg']}", status_code=400)

    await store.update_settings(validated)
    return RedirectResponse(url="/manage?tab=settings", status_code=303)


# --- Import / Export ---


@router.get("/export")
async def export_data(store: DataStore = Depends(require_store)):
    """전체 데이터를 JSON 파일로 다운로드합니다."""
    return Response(
        content=store.export_json(),
        media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=ranker_data.json"},
    )


@router.post("/import")
async def import_data(
    file: UploadFile = File(...),
    store: DataStore = Depends(require_store),
):
    """업로드된 JSON 파일로 전체 데이터를 교체합니다."""
    _MAX_UPLOAD_BYTES = 1_000_000
    raw = await file.read(_MAX_UPLOAD_BYTES + 1)
    if len(raw) > _MAX_UPLOAD_BYTES:
        return HTMLResponse("파일 크기는 1MB를 초과할 수 없습니다.", status_code=413)
    try:
        await store.import_json(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValidationError, ValueError):
        return HTMLResponse("유효하지 않은 JSON 파일입니다.", status_code=400)
    return RedirectResponse(url="/manage?tab=data", status_code=303)
