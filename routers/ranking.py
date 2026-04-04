# routers/ranking.py
# 세션별 DataStore를 사용하여 랭킹 페이지를 렌더링합니다.

from fastapi import APIRouter, Request, Cookie
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from deps import get_session_store

router = APIRouter(prefix="/ranking", tags=["ranking"])
templates = Jinja2Templates(directory="templates")


@router.get("", response_class=HTMLResponse)
async def get_ranking(
    request: Request,
    sort_by: str = "total",
    session_id: str | None = Cookie(default=None),
):
    store = await get_session_store(request, session_id)
    if not store:
        return RedirectResponse(url="/", status_code=303)

    criteria = store.criteria
    items = store.items

    valid_sort_keys = {"total"} | {c["key"] for c in criteria}
    if sort_by not in valid_sort_keys:
        sort_by = "total"

    if not items:
        return templates.TemplateResponse(request, "ranking.html", {
            "items": [],
            "criteria": criteria,
            "sort_by": sort_by,
            "chart_data": {"labels": [], "counts": [], "category": ""},
        })

    # 가중 합산 방식으로 total 계산
    weight_map = {c["key"]: c["weight"] for c in criteria}
    total_weight = sum(weight_map.values()) or 1.0

    ranked = []
    for item in items:
        row: dict = {"name": item["name"], "matches": item["matches_played"], "id": item["id"]}
        weighted_sum = 0.0
        for c in criteria:
            val = item["ratings"].get(c["key"], store.settings["initial_rating"])
            row[c["key"]] = round(val, 1)
            weighted_sum += val * c["weight"]
        row["total"] = round(weighted_sum / total_weight, 1)
        ranked.append(row)

    ranked.sort(key=lambda x: x.get(sort_by, 0), reverse=True)

    # 차트 데이터 (히스토그램)
    scores = [x.get(sort_by, 0) for x in ranked]
    if scores:
        min_s = int(min(scores))
        max_s = int(max(scores)) + 1
        min_bucket = (min_s // 50) * 50
        max_bucket = ((max_s // 50) + 1) * 50
    else:
        min_bucket, max_bucket = 800, 1800

    labels = []
    counts = []
    for i in range(min_bucket, max_bucket, 50):
        labels.append(str(i))
        counts.append(sum(1 for s in scores if i <= s < i + 50))

    cat_label = "종합 점수"
    if sort_by != "total":
        for c in criteria:
            if c["key"] == sort_by:
                cat_label = c["label"].upper()
                break

    chart_data = {"labels": labels, "counts": counts, "category": cat_label}

    return templates.TemplateResponse(request, "ranking.html", {
        "items": ranked,
        "criteria": criteria,
        "sort_by": sort_by,
        "chart_data": chart_data,
    })
