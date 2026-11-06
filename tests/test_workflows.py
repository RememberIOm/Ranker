"""랭킹 목록, 백업 확인, 기록 화면의 요청 단위 회귀 테스트."""

import json
import re

import httpx2
import pytest

from ranker.main import app
from ranker.routers import battle
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
    for batch in range(3):
        names = "\n".join(f"{batch}-{i} " + "긴이름" * 50 for i in range(500))
        response = await client.post("/manage/add-bulk", data={"names": names})
        assert response.status_code == 303
    raw = (await client.get("/manage/export")).text
    assert len(raw.encode()) > 1_000_000
    assert (await _stage(client, raw)).status_code == 200
    response = await client.post("/manage/import/confirm")
    assert response.status_code == 303
    assert len(json.loads((await client.get("/manage/export")).text)["items"]) == 1500


async def test_capacity_error_explains_recovery_and_preserves_data(client, monkeypatch):
    from ranker import database

    await client.post("/manage/add", data={"name": "보존 항목"})
    monkeypatch.setattr(database, "MAX_BACKUP_BYTES", 1)
    response = await client.post("/manage/add", data={"name": "저장 불가"})
    assert response.status_code == 422
    assert "내역을 비우세요" in response.text
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


async def test_empty_battle_pages_leave_shared_guidance_unchanged(client):
    response = await client.get("/battle")
    assert "항목이 2개 이상" in response.text
    await client.post("/manage/criteria", data={})
    for url in ("/battle", "/battle/focus/1"):
        response = await client.get(url)
        assert response.status_code == 200 and "평가 기준이 없습니다" in response.text
    # Starlette writes the request into the context it is given.
    assert "request" not in battle._EMPTY_NO_CRITERIA


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


def _undo_form(page: str) -> dict[str, str]:
    """내역 화면의 '마지막 투표 취소' 폼이 보내는 값을 그대로 읽습니다."""
    form = re.search(r'<form action="/history/undo".*?</form>', page, re.DOTALL).group(
        0
    )
    return dict(
        re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"', form)
    )


async def _vote_on_page(client, page: str):
    """대결 화면에 그려진 라운드로 모든 기준에 A 승을 보냅니다."""
    state = {
        key: re.search(rf'data-{key}="([^"]+)"', page).group(1)
        for key in ("item1-id", "item2-id", "round-token")
    }
    keys = json.loads(re.search(r"data-criteria-keys='([^']*)'", page).group(1))
    return await client.post(
        "/battle/vote",
        json={
            "item1_id": int(state["item1-id"]),
            "item2_id": int(state["item2-id"]),
            "round_token": state["round-token"],
            "votes": dict.fromkeys(keys, "1"),
        },
    )


async def test_undo_form_from_before_a_history_clear_cannot_undo_a_newer_vote(client):
    await client.post("/manage/add-bulk", data={"names": "Alpha\nBeta"})
    for _ in range(3):
        page = (await client.get("/battle")).text
        assert (await _vote_on_page(client, page)).status_code == 200
    stale_form = _undo_form((await client.get("/history")).text)
    # 다른 탭에서 내역을 비우고 같은 수만큼 다시 투표합니다.
    await client.post("/history/clear")
    for _ in range(3):
        page = (await client.get("/battle")).text
        assert (await _vote_on_page(client, page)).status_code == 200
    assert (await client.post("/history/undo", data=stale_form)).status_code == 409
    store = await open_store(client.cookies.get("session_id"))
    assert not any(event["undone"] for event in await store.history_events())


async def test_open_battle_survives_changes_that_keep_its_ballot_valid(client):
    await client.post("/manage/add-bulk", data={"names": "Alpha\nBeta\nGamma"})
    criteria = json.loads((await client.get("/manage/export")).text)["criteria"]
    relabeled = {
        "key": [c["key"] for c in criteria],
        "label": [c["label"] + "!" for c in criteria],
        "color": [c["color"] for c in criteria],
        "weight": [str(c["weight"]) for c in criteria],
    }

    async def undo():
        page = (await client.get("/history")).text
        return await client.post("/history/undo", data=_undo_form(page))

    changes = {
        "settings": lambda: client.post("/manage/settings", data={"blind_mode": "on"}),
        "add item": lambda: client.post("/manage/add", data={"name": "Delta"}),
        "relabel criteria": lambda: client.post("/manage/criteria", data=relabeled),
        "undo": undo,
        "recalculate": lambda: client.post("/history/recalculate"),
    }
    for name, change in changes.items():
        page = (await client.get("/battle")).text
        # 다른 탭에서 대결 내용과 무관한 변경을 합니다.
        assert (await change()).status_code == 303, name
        assert (await _vote_on_page(client, page)).status_code == 200, name


async def test_ballot_the_ranking_no_longer_accepts_asks_for_reload(client):
    await client.post("/manage/add-bulk", data={"names": "Alpha\nBeta\nGamma"})
    page = (await client.get("/battle")).text
    await client.post(
        "/manage/criteria",
        data={"key": "", "label": "새 기준", "color": "blue", "weight": "1"},
    )
    assert (await _vote_on_page(client, page)).status_code == 409
    page = (await client.get("/battle")).text
    item_id = re.search(r'data-item1-id="(\d+)"', page).group(1)
    await client.post("/manage/delete", data={"item_id": item_id})
    assert (await _vote_on_page(client, page)).status_code == 409


async def test_emptying_an_old_library_keeps_its_recovery_code(client):
    from ranker.database import transaction
    from ranker.store import cleanup_pending_imports

    code = client.cookies.get("ranker_library")
    session_id = client.cookies.get("session_id")
    await client.post("/collections/delete", data={"session_id": session_id})
    async with transaction() as db:
        await db.execute("UPDATE libraries SET created_at = 0")
    await cleanup_pending_imports()
    client.cookies.clear()
    response = await client.post("/collections/recover", data={"code": code})
    assert response.status_code == 303


async def test_failed_import_apply_can_be_retried(client, monkeypatch):
    from ranker import store
    from ranker.rating_engine import FitConvergenceError

    await client.post("/manage/add-bulk", data={"names": "Alpha\nBeta"})
    raw = (await client.get("/manage/export")).text
    await client.post("/manage/add", data={"name": "새 항목"})
    assert (await _stage(client, raw)).status_code == 200

    def fail(*args, **kwargs):
        raise FitConvergenceError("수렴하지 않았습니다.")

    monkeypatch.setattr(store, "fit_rankings", fail)
    assert (await client.post("/manage/import/confirm")).status_code == 503
    monkeypatch.undo()
    assert (await client.post("/manage/import/confirm")).status_code == 303
    assert "새 항목" not in (await client.get("/manage/export")).text
