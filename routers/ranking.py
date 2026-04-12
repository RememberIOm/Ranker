# routers/ranking.py
# 세션별 DataStore를 사용하여 랭킹 페이지를 렌더링합니다.

from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse

from deps import require_store
from services import composite_rating, display_rating, display_uncertainty
from store import DataStore
from template_env import templates

router = APIRouter(prefix="/ranking", tags=["ranking"])


@router.get("", response_class=HTMLResponse)
async def get_ranking(
    request: Request,
    sort_by: str = "total",
    store: DataStore = Depends(require_store),
):
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

    # 가중 합산 방식으로 total 계산 — services.composite_rating과 동일 로직
    initial_sq = store.settings["initial_sigma"] ** 2
    ranked = []
    for item in items:
        row: dict = {"name": item["name"], "matches": item["matches_played"], "id": item["id"]}
        for c in criteria:
            mu_val = item["mu"].get(c["key"], 0.0)
            sq_val = item["sigma_sq"].get(c["key"], initial_sq)
            row[c["key"]] = round(display_rating(store, mu_val), 1)
            row[c["key"] + "_sigma"] = round(display_uncertainty(store, sq_val), 1)
        row["total"] = round(composite_rating(store, item), 1)
        avg_sigma = sum(
            display_uncertainty(store, item["sigma_sq"].get(c["key"], initial_sq))
            for c in criteria
        ) / len(criteria) if criteria else 0
        row["avg_sigma"] = round(avg_sigma, 1)
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
