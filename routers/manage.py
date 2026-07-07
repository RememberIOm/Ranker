# routers/manage.py
# 관리 페이지: 항목 CRUD, 대량 추가, 평가 기준 편집, Elo 설정, JSON Import/Export
# 세션별 DataStore를 사용합니다.

import json
import re
import hashlib
import unicodedata

from fastapi import APIRouter, Request, Form, UploadFile, File, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import ValidationError

from deps import import_json_upload, is_htmx, require_store
from schemas import CriterionModel, SettingsModel
from store import DataStore
from template_env import templates

router = APIRouter(prefix="/manage", tags=["manage"])


def _safe_redirect(url: str, fallback: str) -> str:
    """외부 URL로의 오픈 리다이렉트를 방지합니다. 상대 경로만 허용합니다."""
    if url.startswith("/") and not url.startswith("//"):
        return url
    return fallback


_VALID_TABS = {"items", "criteria", "settings", "data"}


def _sorted_items(store: DataStore) -> list:
    return sorted(store.items, key=lambda x: x["name"])


def _htmx_toast(message: str, toast_type: str = "success") -> HTMLResponse:
    """HX-Trigger 헤더로 토스트를 트리거하는 빈 응답을 반환합니다."""
    resp = HTMLResponse("")
    resp.headers["HX-Trigger"] = json.dumps({"showToast": {"message": message, "type": toast_type}})
    return resp


def _form_error(request: Request, message: str) -> HTMLResponse:
    """폼 검증 실패 응답 — HTMX면 에러 토스트, 아니면 400."""
    if is_htmx(request):
        return _htmx_toast(message, "error")
    return HTMLResponse(message, status_code=400)


@router.get("", response_class=HTMLResponse)
async def manage_page(request: Request, tab: str = "items", store: DataStore = Depends(require_store)) -> HTMLResponse:
    if tab not in _VALID_TABS:
        tab = "items"

    ctx = {
        "items": _sorted_items(store),
        "criteria": store.criteria,
        "settings": store.settings,
        "tab": tab,
    }

    return templates.TemplateResponse(request, "manage.html", ctx)


# --- Items ---


@router.post("/add")
async def add_item(request: Request, name: str = Form(...), store: DataStore = Depends(require_store)) -> Response:
    if name.strip():
        await store.add_item(name)

    if is_htmx(request):
        return templates.TemplateResponse(request, "partials/manage_item_list.html", {"items": _sorted_items(store)})
    return RedirectResponse(url="/manage?tab=items", status_code=303)


@router.post("/add-bulk")
async def add_items_bulk(request: Request, names: str = Form(...), store: DataStore = Depends(require_store)) -> Response:
    """줄바꿈으로 구분된 이름 목록을 한번에 추가합니다."""
    name_list = [n.strip() for n in names.splitlines() if n.strip()]
    await store.add_items_bulk(name_list)

    if is_htmx(request):
        return templates.TemplateResponse(request, "partials/manage_item_list.html", {"items": _sorted_items(store)})
    return RedirectResponse(url="/manage?tab=items", status_code=303)


@router.post("/delete")
async def delete_item(
    request: Request,
    item_id: int = Form(...),
    redirect_url: str = Form("/manage?tab=items"),
    store: DataStore = Depends(require_store),
) -> Response:
    await store.delete_item(item_id)

    if is_htmx(request):
        return HTMLResponse("")
    return RedirectResponse(url=_safe_redirect(redirect_url, "/manage?tab=items"), status_code=303)


@router.post("/edit")
async def edit_item(
    request: Request,
    item_id: int = Form(...),
    new_name: str = Form(...),
    redirect_url: str = Form("/manage?tab=items"),
    store: DataStore = Depends(require_store),
) -> Response:
    if new_name.strip():
        await store.update_item(item_id, name=new_name.strip())

    if is_htmx(request):
        item = store.get_item(item_id)
        if item:
            return templates.TemplateResponse(request, "partials/manage_item_row.html", {"item": item})
        return HTMLResponse("")
    return RedirectResponse(url=_safe_redirect(redirect_url, "/manage?tab=items"), status_code=303)


# --- Criteria ---


@router.post("/criteria")
async def update_criteria(request: Request, store: DataStore = Depends(require_store)) -> Response:
    """평가 기준을 폼 데이터로 일괄 교체합니다. key가 비어있으면 자동 생성합니다."""

    form = await request.form()

    keys = form.getlist("key")
    labels = form.getlist("label")
    colors = form.getlist("color")
    weights = form.getlist("weight")

    if not (len(keys) == len(labels) == len(colors) == len(weights)):
        return _form_error(request, "폼 데이터가 올바르지 않습니다.")

    used_keys: set[str] = set()
    new_criteria = []

    for raw_key, lbl, clr, w in zip(keys, labels, colors, weights):
        lbl = lbl.strip()
        if not lbl:
            continue

        key = raw_key.strip()
        if not key:
            key = _generate_key(lbl, used_keys)
        if key in used_keys:
            return _form_error(request, f"중복된 key가 있습니다: '{key}'")

        try:
            weight_val = float(w) if w else 1.0
        except ValueError:
            return _form_error(request, f"'{lbl}'의 가중치는 숫자여야 합니다.")

        used_keys.add(key)
        new_criteria.append({
            "key": key,
            "label": lbl,
            "color": clr.strip() or "gray",
            "weight": weight_val,
        })

    try:
        for criterion in new_criteria:
            CriterionModel(**criterion)  # key 형식·weight 범위 검증
    except ValidationError as exc:
        return _form_error(request, f"기준 저장 실패: {exc.errors()[0]['msg']}")

    await store.set_criteria(new_criteria)

    if is_htmx(request):
        return _htmx_toast("평가 기준이 저장되었습니다.")
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


_NUMERIC_SETTINGS_FIELDS = (
    "initial_sigma", "draw_prior_max", "draw_prior_strength", "draw_bandwidth",
    "hierarchical_strength", "display_center", "display_scale",
    "result_skip_seconds",
)


@router.post("/settings")
async def update_settings(request: Request, store: DataStore = Depends(require_store)) -> Response:
    form = await request.form()

    # 형변환·범위 검증은 SettingsModel에 위임 — 잘못된 값은 400으로 응답
    patch: dict = {key: form[key] for key in _NUMERIC_SETTINGS_FIELDS if form.get(key)}
    patch["result_auto_skip"] = "result_auto_skip" in form
    if form.get("battle_mode") in {"2way", "3way"}:
        patch["battle_mode"] = form["battle_mode"]

    try:
        validated = SettingsModel(**{**store.settings, **patch}).model_dump(mode="python")
    except ValidationError as exc:
        return _form_error(request, f"설정 값이 올바르지 않습니다: {exc.errors()[0]['msg']}")

    await store.update_settings(validated)

    if is_htmx(request):
        return _htmx_toast("설정이 저장되었습니다.")
    return RedirectResponse(url="/manage?tab=settings", status_code=303)


# --- Import / Export ---


@router.get("/export")
async def export_data(store: DataStore = Depends(require_store)) -> Response:
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
) -> Response:
    """업로드된 JSON 파일로 전체 데이터를 교체합니다."""
    error = await import_json_upload(file, store)
    if error:
        return error
    return RedirectResponse(url="/manage?tab=data", status_code=303)
