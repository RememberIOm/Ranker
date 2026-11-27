"""관리 화면: 항목 추가·편집·삭제, 평가 기준, 설정, 백업 내보내기와 불러오기."""

import hashlib
import json
import re
import unicodedata
from typing import Annotated

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import ValidationError

from ranker import database
from ranker.deps import (
    StoreDep,
    UploadTooLargeError,
    error_response,
    import_error_message,
    is_htmx,
    read_backup_upload,
)
from ranker.schemas import CriterionModel, SettingsModel, safe_relative_path
from ranker.store import DataStore
from ranker.template_env import templates

router = APIRouter(prefix="/manage", tags=["manage"])


def _safe_redirect(url: str, fallback: str) -> str:
    """외부 URL로의 오픈 리다이렉트를 막습니다."""
    try:
        return safe_relative_path(url) or fallback
    except ValueError:
        return fallback


_VALID_TABS = frozenset({"items", "criteria", "settings", "data"})


def _sorted_items(store: DataStore) -> list:
    return sorted(store.items, key=lambda x: x["name"])


def _toast(response: Response, message: str, toast_type: str = "success") -> Response:
    """HX-Trigger 헤더로 app.js의 토스트를 띄웁니다."""
    response.headers["HX-Trigger"] = json.dumps(
        {"showToast": {"message": message, "type": toast_type}}
    )
    return response


def _item_list_response(
    request: Request, store: DataStore, redirect_url: str = "/manage?tab=items"
) -> Response:
    """HTMX면 항목 목록 조각을, 폼 제출이면 관리 화면으로 돌려보냅니다."""
    if is_htmx(request):
        return templates.TemplateResponse(
            request,
            "partials/manage_item_list.html",
            {
                "items": _sorted_items(store),
                "criteria": store.criteria,
                "update_counts": True,
            },
        )
    return RedirectResponse(
        url=_safe_redirect(redirect_url, "/manage?tab=items"), status_code=303
    )


@router.get("", response_class=HTMLResponse)
async def manage_page(
    request: Request, store: StoreDep, tab: str = "items"
) -> HTMLResponse:
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
async def add_item(
    request: Request, name: Annotated[str, Form()], store: StoreDep
) -> Response:
    await store.add_items([name])
    return _item_list_response(request, store)


@router.post("/add-bulk")
async def add_items_bulk(
    request: Request, names: Annotated[str, Form()], store: StoreDep
) -> Response:
    """줄바꿈으로 구분된 이름 목록을 한번에 추가합니다."""
    await store.add_items(names.splitlines())
    return _item_list_response(request, store)


@router.post("/delete")
async def delete_item(
    request: Request,
    item_id: Annotated[int, Form()],
    store: StoreDep,
    redirect_url: Annotated[str, Form()] = "/manage?tab=items",
) -> Response:
    await store.delete_item(item_id)
    return _item_list_response(request, store, redirect_url)


@router.post("/edit")
async def edit_item(
    request: Request,
    item_id: Annotated[int, Form()],
    new_name: Annotated[str, Form()],
    store: StoreDep,
    redirect_url: Annotated[str, Form()] = "/manage?tab=items",
) -> Response:
    if new_name.strip():
        await store.rename_item(item_id, new_name)
    return _item_list_response(request, store, redirect_url)


# --- Criteria ---


@router.post("/criteria")
async def update_criteria(request: Request, store: StoreDep) -> Response:
    """평가 기준을 폼 데이터로 일괄 교체합니다. key가 비어있으면 자동 생성합니다."""

    form = await request.form()

    keys = form.getlist("key")
    labels = form.getlist("label")
    colors = form.getlist("color")
    weights = form.getlist("weight")

    if not (len(keys) == len(labels) == len(colors) == len(weights)):
        return error_response(
            request,
            400,
            "입력한 내용이 올바르지 않습니다. 다시 확인하세요.",
            back_url="/manage?tab=criteria",
        )

    # 폼에 이미 지정된 키와 기존 저장 키를 모두 예약해 충돌 및 삭제 기준 데이터 승계를 막습니다.
    reserved_keys: set[str] = {k.strip() for k in keys if k.strip()} | {
        c["key"] for c in store.criteria
    }
    used_keys: set[str] = set()
    new_criteria = []

    for raw_key, lbl, clr, w in zip(keys, labels, colors, weights, strict=True):
        lbl = lbl.strip()
        if not lbl:
            continue

        key = raw_key.strip()
        if not key:
            key = _generate_key(lbl, reserved_keys)
            reserved_keys.add(key)
        if key in used_keys:
            return error_response(
                request,
                400,
                "같은 기준이 두 번 들어 있습니다. 새로고침한 뒤 다시 저장하세요.",
                back_url="/manage?tab=criteria",
            )

        try:
            weight_val = float(w) if w else 1.0
        except ValueError:
            return error_response(
                request,
                400,
                f"'{lbl}'의 가중치에는 숫자만 입력하세요.",
                back_url="/manage?tab=criteria",
            )

        used_keys.add(key)
        new_criteria.append(
            {
                "key": key,
                "label": lbl,
                "color": clr.strip() or "gray",
                "weight": weight_val,
            }
        )

    if len(new_criteria) > 64:
        return error_response(
            request,
            400,
            "평가 기준은 최대 64개까지 만들 수 있습니다.",
            back_url="/manage?tab=criteria",
        )

    try:
        for criterion in new_criteria:
            CriterionModel(**criterion)  # key 형식·weight 범위 검증
    except ValidationError:
        return error_response(
            request,
            400,
            "평가 기준을 저장하지 못했습니다. 이름과 가중치를 확인하세요.",
            back_url="/manage?tab=criteria",
        )

    await store.set_criteria(new_criteria)

    if is_htmx(request):
        response = templates.TemplateResponse(
            request,
            "partials/manage_criteria_form.html",
            {"criteria": store.criteria, "items": store.items, "update_counts": True},
        )
        return _toast(response, "평가 기준을 저장했습니다.")
    return RedirectResponse(url="/manage?tab=criteria", status_code=303)


_MAX_CRITERION_KEY_LEN = 100


def _generate_key(label: str, existing: set[str]) -> str:
    """label로부터 안전한 key를 생성합니다. 길이 한도를 지키고 충돌 시 숫자 접미사를 붙입니다."""
    ascii_label = (
        unicodedata.normalize("NFKD", label).encode("ascii", "ignore").decode()
    )
    base = re.sub(r"[^a-z0-9]+", "_", ascii_label.lower()).strip("_")

    if not base:
        base = "c_" + hashlib.sha256(label.encode()).hexdigest()[:8]

    base = base[:_MAX_CRITERION_KEY_LEN]
    key = base
    counter = 2
    while key in existing:
        suffix = f"_{counter}"
        key = f"{base[: _MAX_CRITERION_KEY_LEN - len(suffix)]}{suffix}"
        counter += 1
    return key


# --- Settings ---


_NUMERIC_SETTINGS_FIELDS = (
    "initial_sigma",
    "display_center",
    "display_scale",
    "result_skip_seconds",
)


@router.post("/settings")
async def update_settings(request: Request, store: StoreDep) -> Response:
    form = await request.form()

    # 형변환·범위 검증은 SettingsModel에 위임 — 잘못된 값은 400으로 응답
    patch: dict = {key: form[key] for key in _NUMERIC_SETTINGS_FIELDS if form.get(key)}
    patch["result_auto_skip"] = "result_auto_skip" in form
    patch["blind_mode"] = "blind_mode" in form
    if "battle_mode" in form:
        patch["battle_mode"] = form["battle_mode"]

    try:
        validated = SettingsModel(**{**store.settings, **patch}).model_dump(
            mode="python"
        )
    except ValidationError:
        return error_response(
            request,
            400,
            "설정값이 올바르지 않습니다. 입력 범위를 확인하세요.",
            back_url="/manage?tab=settings",
        )

    await store.update_settings({key: validated[key] for key in patch})

    if is_htmx(request):
        return _toast(HTMLResponse(""), "설정을 저장했습니다.")
    return RedirectResponse(url="/manage?tab=settings", status_code=303)


# --- Import / Export ---


@router.get("/export")
async def export_data(store: StoreDep) -> Response:
    return Response(
        content=await store.export_json(),
        media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=ranker_data.json"},
    )


@router.post("/import")
async def import_data(
    request: Request,
    file: Annotated[UploadFile, File()],
    store: StoreDep,
) -> Response:
    """백업을 검증해 서버에 잠시 보관하고, 교체될 내용을 먼저 보여줍니다."""
    try:
        preview = await store.stage_import(await read_backup_upload(file))
    except UploadTooLargeError as exc:
        return error_response(
            request,
            413,
            f"{exc} 현재 데이터는 바뀌지 않았습니다.",
            back_url="/manage?tab=data",
        )
    except (ValueError, ValidationError) as exc:
        return error_response(
            request,
            400,
            f"{import_error_message(exc)} 현재 데이터는 바뀌지 않았습니다.",
            back_url="/manage?tab=data",
        )
    current_history = await database.count_events(store.session_id)
    return templates.TemplateResponse(
        request,
        "import_preview.html",
        {
            "preview": preview,
            "current_items": len(store.items),
            "current_criteria": len(store.criteria),
            "current_history": current_history,
        },
    )


@router.post("/import/confirm")
async def confirm_import(store: StoreDep) -> Response:
    await store.apply_staged_import()
    return RedirectResponse(url="/manage?tab=data", status_code=303)
