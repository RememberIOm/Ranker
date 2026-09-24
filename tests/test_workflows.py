"""랭킹 목록, 백업 확인, 기록 화면의 요청 단위 회귀 테스트."""

import json

import httpx2
import pytest

from ranker.main import app
from ranker.routers.ranking import histogram, ranking_rows
from ranker.store import open_store


@pytest.fixture
async def client(_temp_db):
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.post("/start")
        yield client


async def test_library_switch_and_recovery_do_not_lose_boards(client):
    first = client.cookies.get("session_id")
    code = client.cookies.get("ranker_library")
    await client.post("/manage/add", data={"name": "처음 항목"})
    await client.post("/collections/create", data={"name": "음식"})
    second = client.cookies.get("session_id")
    assert first != second
    await client.post("/collections/switch", data={"session_id": first})
    assert client.cookies.get("session_id") == first
    assert "처음 항목" in (await client.get("/ranking")).text
    client.cookies.clear()
    response = await client.post("/collections/recover", data={"code": code})
    assert response.status_code == 303
    response = await client.get("/collections")
    assert "음식" in response.text
    assert first in response.text and second in response.text


async def test_other_library_cannot_switch_or_rename(client):
    victim = client.cookies.get("session_id")
    client.cookies.clear()
    await client.post("/start")
    assert (
        await client.post("/collections/switch", data={"session_id": victim})
    ).status_code == 404
    assert (
        await client.post(
            "/collections/rename", data={"session_id": victim, "name": "다른 이름"}
        )
    ).status_code == 404


async def test_reserved_criterion_names_and_raw_sort(client):
    await client.post("/manage/add", data={"name": "Alpha"})
    await client.post("/manage/add", data={"name": "Beta"})
    await client.post(
        "/manage/criteria",
        data={"key": "name", "label": "이름", "color": "blue", "weight": "1"},
    )
    store = await open_store(client.cookies.get("session_id"))
    store.items[0]["mu"]["name"] = 0.00001
    store.items[1]["mu"]["name"] = 0.00002
    rows = ranking_rows(store, "criterion:name")
    assert [r["name"] for r in rows] == ["Beta", "Alpha"]
    assert [r["rank"] for r in rows] == [1, 2]
    response = await client.get("/ranking?sort_by=criterion:name")
    assert response.status_code == 200
    assert "Alpha" in response.text
    response = await client.get("/ranking?q=Beta")
    assert "Alpha" not in response.text
    assert "Beta" in response.text


def test_histogram_bounds_even_extreme_scores():
    result = histogram([-1e10, 0, 1e10])
    assert len(result["labels"]) <= 20
    assert sum(result["counts"]) == 3


async def _stage(client, raw):
    return await client.post(
        "/manage/import", files={"file": ("backup.json", raw, "application/json")}
    )


async def test_import_requires_preview_and_detects_changes(client):
    await client.post("/manage/add", data={"name": "보존 항목"})
    raw = (await client.get("/manage/export")).text
    response = await _stage(client, raw)
    assert response.status_code == 200
    assert "/manage/import/confirm" in response.text
    await client.post("/manage/add", data={"name": "새 항목"})
    response = await client.post("/manage/import/confirm")
    assert response.status_code == 409
    assert "새 항목" in (await client.get("/manage/export")).text
    await _stage(client, raw)
    response = await client.post("/manage/import/confirm")
    assert response.status_code == 303
    assert "새 항목" not in (await client.get("/manage/export")).text
    # 한 번 적용한 미리보기는 다시 쓸 수 없습니다.
    assert (await client.post("/manage/import/confirm")).status_code == 409


async def test_reading_pages_does_not_expire_a_staged_import(client):
    await client.post("/manage/add-bulk", data={"names": "Alpha\nBeta"})
    raw = (await client.get("/manage/export")).text
    await _stage(client, raw)
    for path in ("/battle", "/battle", "/ranking", "/history"):
        assert (await client.get(path)).status_code == 200
    assert (await client.post("/manage/import/confirm")).status_code == 303


async def test_empty_and_nonfinite_import_leave_data(client):
    await client.post("/manage/add", data={"name": "보존 항목"})
    for raw in ["{}", '{"items": [{"mu": {"story": 1e309}}]}']:
        response = await client.post(
            "/manage/import", files={"file": ("backup.json", raw, "application/json")}
        )
        assert response.status_code == 400
    assert "보존 항목" in (await client.get("/manage/export")).text


async def test_criteria_response_contains_persistent_id(client):
    response = await client.post(
        "/manage/criteria",
        data={"key": "", "label": "새 기준", "color": "blue", "weight": "1"},
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200
    key = json.loads((await client.get("/manage/export")).text)["criteria"][0]["key"]
    assert f'value="{key}"' in response.text
    assert 'id="criteria-form"' in response.text


async def test_native_backup_over_one_megabyte_can_be_restored(client):
    for batch in range(2):
        names = "\n".join(f"{batch}-{i} " + "긴이름" * 30 for i in range(1100))
        response = await client.post("/manage/add-bulk", data={"names": names})
        assert response.status_code == 303
    raw = (await client.get("/manage/export")).text
    assert len(raw.encode()) > 1_000_000
    assert (await _stage(client, raw)).status_code == 200
    response = await client.post("/manage/import/confirm")
    assert response.status_code == 303
    assert len(json.loads((await client.get("/manage/export")).text)["items"]) == 2200


async def test_capacity_error_explains_recovery_and_preserves_data(client, monkeypatch):
    from ranker import database

    await client.post("/manage/add", data={"name": "보존 항목"})
    monkeypatch.setattr(database, "MAX_BACKUP_BYTES", 1)
    response = await client.post("/manage/add", data={"name": "저장 불가"})
    assert response.status_code == 409
    assert "투표 기록을 정리" in response.text
    monkeypatch.undo()
    export = (await client.get("/manage/export")).text
    assert "보존 항목" in export and "저장 불가" not in export


async def test_long_name_rejected_without_breaking_existing_board(client):
    await client.post("/manage/add", data={"name": "보존 항목"})
    response = await client.post("/manage/add", data={"name": "x" * 501})
    assert response.status_code == 422
    response = await client.get("/ranking")
    assert response.status_code == 200 and "보존 항목" in response.text


async def test_focus_label_follows_item_after_position_shuffle(client, monkeypatch):
    from ranker.routers import battle

    await client.post("/manage/add-bulk", data={"names": "Alpha\nBeta"})
    monkeypatch.setattr(
        battle,
        "get_match_pair",
        lambda store, focus_id=None: (store.items[1], store.items[0]),
    )
    response = await client.get("/battle/focus/1")
    assert response.status_code == 200
    assert (
        "&#39;Alpha&#39; 집중 평가 중" in response.text
        or "'Alpha' 집중 평가 중" in response.text
    )
    assert "'Beta' 집중 평가 중" not in response.text


async def test_cross_site_start_cannot_replace_library(client):
    original = client.cookies.get("ranker_library")
    sid = client.cookies.get("session_id")
    response = await client.post(
        "/start",
        headers={"Origin": "https://unrelated.example", "Sec-Fetch-Site": "cross-site"},
    )
    assert response.status_code == 403
    assert client.cookies.get("ranker_library") == original
    assert client.cookies.get("session_id") == sid


async def test_same_origin_writes_work_behind_tls_proxy(client):
    response = await client.post(
        "/manage/add",
        data={"name": "정상 항목"},
        headers={"Origin": "https://test", "Sec-Fetch-Site": "same-origin"},
    )
    assert response.status_code == 303


async def test_collections_page_creates_nothing_for_a_new_visitor(_temp_db):
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://test"
    ) as fresh:
        response = await fresh.get("/collections")
        assert response.status_code == 200
        assert "ranker_library" not in response.headers.get("set-cookie", "")
        assert "복구 코드 보기" not in response.text


async def test_unlisted_ranking_can_be_kept(client):
    from ranker.store import create_store

    orphan = await create_store("b" * 32)
    client.cookies.set("session_id", orphan.session_id)
    page = await client.get("/collections")
    assert "목록에 넣기" in page.text
    response = await client.post("/collections/keep-current", data={"name": "옛 랭킹"})
    assert response.status_code == 303
    page = await client.get("/collections")
    assert "옛 랭킹" in page.text and "목록에 넣기" not in page.text


async def test_refresh_keeps_the_same_round(client):
    await client.post("/manage/add-bulk", data={"names": "Alpha\nBeta\nGamma"})
    first = (await client.get("/battle")).text
    second = (await client.get("/battle")).text
    token = first.split('data-round-token="')[1].split('"')[0]
    assert f'data-round-token="{token}"' in second


async def test_oversized_form_is_rejected(client):
    response = await client.post(
        "/manage/add-bulk", data={"names": "x" * (1024 * 1024 + 10)}
    )
    assert response.status_code == 413


async def test_form_errors_render_a_page_and_scripts_get_json(client):
    response = await client.post("/collections/switch", data={"session_id": "0" * 32})
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/html")
    assert "돌아가기" in response.text
    client.cookies.clear()
    response = await client.post(
        "/battle/vote",
        json={
            "item1_id": 1,
            "item2_id": 2,
            "round_token": "x" * 20,
            "votes": {"a": "1"},
        },
    )
    assert response.status_code == 401
    assert "detail" in response.json()


async def test_history_is_paginated(client):
    from ranker.schemas import BattleVoteRequest

    await client.post("/manage/add-bulk", data={"names": "Alpha\nBeta"})
    store = await open_store(client.cookies.get("session_id"))
    keys = [c["key"] for c in store.criteria]
    for _ in range(55):
        token = await store.issue_battle_round([1, 2])
        await store.apply_vote(
            BattleVoteRequest(
                item1_id=1,
                item2_id=2,
                round_token=token,
                votes=dict.fromkeys(keys, "1"),
            )
        )
    first = (await client.get("/history")).text
    assert "1 / 2" in first and first.count('<li class="card p-5') == 50
    second = (await client.get("/history?page=2")).text
    assert second.count('<li class="card p-5') == 5
