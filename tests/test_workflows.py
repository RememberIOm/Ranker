"""랭킹 목록, 백업 확인, 기록 화면의 요청 단위 회귀 테스트."""

import hashlib
import json
import re

import httpx
import pytest

from main import app
from routers.ranking import histogram, ranking_rows
from store import get_store


@pytest.fixture
async def client(_temp_db):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
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
    store = await get_store(client.cookies.get("session_id"))
    store.items[0]["mu"]["name"] = 0.00001
    store.items[1]["mu"]["name"] = 0.00002
    await store.save()
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


async def test_import_requires_preview_and_detects_changes(client):
    await client.post("/manage/add", data={"name": "보존 항목"})
    raw = (await client.get("/manage/export")).text
    response = await client.post(
        "/manage/import", files={"file": ("backup.json", raw, "application/json")}
    )
    assert response.status_code == 200
    assert "confirmed_digest" in response.text
    digest = re.search(
        r'name="confirmed_digest" value="([a-f0-9]+)"', response.text
    ).group(1)
    await client.post("/manage/add", data={"name": "새 항목"})
    response = await client.post(
        "/manage/import", data={"raw_json": raw, "confirmed_digest": digest}
    )
    assert response.status_code == 409
    assert "새 항목" in (await client.get("/manage/export")).text
    current = (await client.get("/manage/export")).text
    digest = hashlib.sha256((current + raw).encode()).hexdigest()
    response = await client.post(
        "/manage/import", data={"raw_json": raw, "confirmed_digest": digest}
    )
    assert response.status_code == 303
    assert "새 항목" not in (await client.get("/manage/export")).text


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
    # 기존 업로드 한도보다 큰, 앱이 직접 생성한 백업을 왕복합니다.
    names = "\n".join(f"{index} " + "긴이름" * 100 for index in range(1200))
    response = await client.post("/manage/add-bulk", data={"names": names})
    assert response.status_code == 303
    raw = (await client.get("/manage/export")).text
    assert len(raw.encode()) > 1_000_000
    digest = hashlib.sha256((raw + raw).encode()).hexdigest()
    response = await client.post(
        "/manage/import", data={"raw_json": raw, "confirmed_digest": digest}
    )
    assert response.status_code == 303
    assert len(json.loads((await client.get("/manage/export")).text)["items"]) == 1200


async def test_import_race_returns_conflict(client, monkeypatch):
    from store import DataStore, InvalidSessionDataError

    raw = (await client.get("/manage/export")).text
    digest = hashlib.sha256((raw + raw).encode()).hexdigest()

    async def conflict(self, raw, expected_export_digest=None):
        raise InvalidSessionDataError("changed")

    monkeypatch.setattr(DataStore, "import_json", conflict)
    response = await client.post(
        "/manage/import", data={"raw_json": raw, "confirmed_digest": digest}
    )
    assert response.status_code == 409


async def test_capacity_error_explains_recovery_and_preserves_data(client, monkeypatch):
    import store

    await client.post("/manage/add", data={"name": "보존 항목"})
    monkeypatch.setattr(store, "MAX_BACKUP_BYTES", 1)
    response = await client.post("/manage/add", data={"name": "저장 불가"})
    assert response.status_code == 500
    assert "투표 이력을 정리" in response.text
    export = (await client.get("/manage/export")).text
    assert "보존 항목" in export and "저장 불가" not in export


async def test_long_name_rejected_without_breaking_existing_board(client):
    await client.post("/manage/add", data={"name": "보존 항목"})
    response = await client.post("/manage/add", data={"name": "x" * 501})
    assert response.status_code == 422
    response = await client.get("/ranking")
    assert response.status_code == 200 and "보존 항목" in response.text
