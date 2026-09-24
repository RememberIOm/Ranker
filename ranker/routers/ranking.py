"""원점수 정렬과 필터를 제공하는 랭킹 화면."""

import asyncio
import math
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from ranker.deps import require_store
from ranker.services import (
    composite_rating,
    display_rating,
    display_uncertainty,
    rank_uncertainty,
)
from ranker.store import DataStore
from ranker.template_env import templates

router = APIRouter(prefix="/ranking", tags=["ranking"])


def ranking_rows(store: DataStore, sort_by: str) -> list[dict[str, Any]]:
    """기준 키를 표시용 메타데이터와 분리하고 반올림 전 공동 순위를 계산합니다."""
    rows = []
    for item in store.items:
        scores = {
            c["key"]: {
                "rating": display_rating(store, item["mu"][c["key"]]),
                "sigma": display_uncertainty(store, item["sigma_sq"][c["key"]]),
                "matches": item["criterion_matches"].get(c["key"], 0),
            }
            for c in store.criteria
        }
        rows.append(
            {
                "id": item["id"],
                "name": item["name"],
                "matches": item["matches_played"],
                "scores": scores,
                "total": composite_rating(store, item),
                "under_evaluated": any(s["matches"] < 5 for s in scores.values()),
            }
        )
    criterion_key = sort_by.removeprefix("criterion:")
    for row in rows:
        row["sort_score"] = (
            row["total"]
            if sort_by == "total"
            else row["scores"][criterion_key]["rating"]
        )
    rows.sort(key=lambda row: (-row["sort_score"], row["name"], row["id"]))
    previous = None
    rank = 0
    for index, row in enumerate(rows, 1):
        if row["sort_score"] != previous:
            rank = index
        row["rank"] = rank
        previous = row["sort_score"]
    return rows


def histogram(scores: list[float]) -> dict[str, list]:
    """최대 20개 구간으로 표시해 점수 범위에 비례한 메모리 증가를 막습니다."""
    if not scores:
        return {"labels": [], "counts": []}
    low, high = min(scores), max(scores)
    width = max(1.0, math.ceil((high - low + 1) / 20))
    count = min(20, int((high - low) // width) + 1)
    counts = [0] * count
    for score in scores:
        counts[min(count - 1, int((score - low) // width))] += 1
    return {
        "labels": [
            f"{low + i * width:,.1f}–{low + (i + 1) * width:,.1f}" for i in range(count)
        ],
        "counts": counts,
    }


_CHART_COLORS = {
    "red": "#ef4444",
    "orange": "#f97316",
    "yellow": "#eab308",
    "green": "#22c55e",
    "teal": "#14b8a6",
    "cyan": "#06b6d4",
    "blue": "#3b82f6",
    "indigo": "#6366f1",
    "purple": "#a855f7",
    "pink": "#ec4899",
    "gray": "#71717a",
}


@router.get("", response_class=HTMLResponse)
async def get_ranking(
    request: Request,
    sort_by: str = "total",
    q: str = "",
    filter: str = "all",
    store: DataStore = Depends(require_store),
) -> HTMLResponse:
    selected = next(
        (c for c in store.criteria if sort_by == "criterion:" + c["key"]), None
    )
    if selected is None:
        sort_by = "total"
    if filter not in {"all", "uncertain", "top"}:
        filter = "all"
    ranked = ranking_rows(store, sort_by)
    uncertainty = await asyncio.to_thread(rank_uncertainty, store, sort_by)
    if uncertainty:
        for row in ranked:
            row["rank_interval"] = uncertainty["intervals"][row["id"]]
    total_items = len(ranked)
    q = q.strip()[:500]
    ranked = [r for r in ranked if q.casefold() in r["name"].casefold()]
    if filter == "uncertain":
        ranked = [r for r in ranked if r["under_evaluated"]]
    elif filter == "top":
        ranked = [r for r in ranked if r["rank"] <= 5]
    return templates.TemplateResponse(
        request,
        "ranking.html",
        {
            "items": ranked,
            "criteria": store.criteria,
            "sort_by": sort_by,
            "q": q,
            "filter": filter,
            "total_items": total_items,
            "adjacent_confidence": uncertainty and uncertainty["adjacent_confidence"],
            "chart_data": {
                **histogram([r["sort_score"] for r in ranked]),
                "category": selected["label"] if selected else "종합 점수",
                "color": _CHART_COLORS[selected["color"]] if selected else "#7c3aed",
            },
        },
    )
