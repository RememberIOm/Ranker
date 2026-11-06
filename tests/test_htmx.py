"""HTMX 요청에 돌려주는 조각 HTML과 헤더."""

import re

import httpx2
import pytest
from httpx2 import ASGITransport

from ranker.main import app
from ranker.schemas import MAX_ITEMS


@pytest.fixture()
async def client(_temp_db) -> httpx2.AsyncClient:
    """세션이 설정된 테스트 클라이언트를 반환합니다."""
    transport = ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as c:
        # 새 세션 생성
        resp = await c.post("/start", follow_redirects=False)
        assert resp.status_code == 303
        yield c


async def _setup_battle(client: httpx2.AsyncClient) -> None:
    """배틀에 필요한 항목과 기준을 설정합니다."""
    await client.post("/manage/add", data={"name": "Alpha"})
    await client.post("/manage/add", data={"name": "Beta"})
    await client.post("/manage/add", data={"name": "Gamma"})
    await client.post(
        "/manage/criteria",
        data={
            "key": ["story"],
            "label": ["Story"],
            "color": ["blue"],
            "weight": ["1.0"],
        },
    )


async def _vote_story(client: httpx2.AsyncClient) -> str:
    """대결 화면에 그려진 라운드로 story 기준에 A 승을 보내고 결과 HTML을 돌려줍니다."""
    page = (await client.get("/battle")).text
    response = await client.post(
        "/battle/vote",
        json={
            "item1_id": int(re.search(r'data-item1-id="(\d+)"', page).group(1)),
            "item2_id": int(re.search(r'data-item2-id="(\d+)"', page).group(1)),
            "round_token": re.search(r'data-round-token="([^"]+)"', page).group(1),
            "votes": {"story": "1"},
        },
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200
    return response.text


async def test_item_limit_keeps_the_form_instead_of_reloading(
    client: httpx2.AsyncClient,
) -> None:
    """app.js는 409를 낡은 화면으로 보고 새로고침하므로, 한도 초과는 409가 아니어야 입력이 남습니다."""
    names = "\n".join(f"항목 {i}" for i in range(MAX_ITEMS + 1))
    resp = await client.post(
        "/manage/add-bulk", data={"names": names}, headers={"HX-Request": "true"}
    )
    assert resp.status_code == 422
    assert f"{MAX_ITEMS:,}개" in resp.json()["detail"]


class TestBattleHTMX:
    async def test_get_battle_htmx_returns_partial(
        self, client: httpx2.AsyncClient
    ) -> None:
        """GET /battle + HX-Request → partial HTML (<!DOCTYPE 없음)"""
        await _setup_battle(client)
        resp = await client.get("/battle", headers={"HX-Request": "true"})
        assert resp.status_code == 200
        assert "<!DOCTYPE" not in resp.text
        assert "battle-state" in resp.text
        assert "criteria-list" in resp.text

    async def test_three_way_skip_is_saved_as_a_skip(
        self, client: httpx2.AsyncClient
    ) -> None:
        """3개 비교의 건너뛰기는 문자열 "skip"으로 저장됩니다."""
        await _setup_battle(client)
        await client.post(
            "/manage/settings", data={"battle_mode": "3way", "blind_mode": "on"}
        )
        resp = await client.get("/battle")
        state = {
            key: re.search(rf'data-{key}="([^"]+)"', resp.text).group(1)
            for key in ("item1-id", "item2-id", "item3-id", "round-token")
        }
        vote_resp = await client.post(
            "/battle/vote/3way",
            json={
                "item1_id": int(state["item1-id"]),
                "item2_id": int(state["item2-id"]),
                "item3_id": int(state["item3-id"]),
                "round_token": state["round-token"],
                "votes": {"story": "skip"},
            },
        )
        assert vote_resp.status_code == 200
        assert "건너뜀" in vote_resp.text
        assert 'id="battle-arena" hx-swap-oob' in vote_resp.text

    async def test_result_modal_has_hold_button_when_auto_skip(
        self, client: httpx2.AsyncClient
    ) -> None:
        """auto_skip 활성화 시 결과 모달이 "멈추기" 버튼과 노출된 auto-skip-area를 포함한다."""
        await _setup_battle(client)
        await client.post(
            "/manage/settings",
            data={
                "initial_sigma": "2.0",
                "battle_mode": "2way",
                "result_auto_skip": "on",
                "result_skip_seconds": "3",
            },
        )

        body = await _vote_story(client)
        assert 'data-auto-skip="true"' in body
        assert 'id="auto-skip-area"' in body
        # auto_skip 활성 → auto-skip-area는 hidden 아님
        auto_area = re.search(r'<div id="auto-skip-area"\s+class="([^"]*)"', body)
        assert auto_area, "auto-skip-area div를 찾을 수 없습니다"
        assert "hidden" not in auto_area.group(1)
        # auto_skip 활성 → click-skip-area는 hidden
        click_area = re.search(r'<div id="click-skip-area"\s+class="([^"]*)"', body)
        assert click_area, "click-skip-area div를 찾을 수 없습니다"
        assert "hidden" in click_area.group(1)
        # 멈추기 버튼이 존재해야 함
        assert 'data-battle="keep-result"' in body
        assert "멈추기" in body

    async def test_result_modal_no_hold_button_when_manual(
        self, client: httpx2.AsyncClient
    ) -> None:
        """auto_skip 비활성화(기본) 시 결과 모달은 click-skip 영역만 노출한다."""
        await _setup_battle(client)
        body = await _vote_story(client)
        assert 'data-auto-skip="false"' in body
        # auto_skip 비활성 → auto-skip-area는 hidden
        auto_area = re.search(r'<div id="auto-skip-area"\s+class="([^"]*)"', body)
        assert auto_area and "hidden" in auto_area.group(1)
        # auto_skip 비활성 → click-skip-area는 hidden 아님 (다음 대결 버튼 노출)
        click_area = re.search(r'<div id="click-skip-area"\s+class="([^"]*)"', body)
        assert click_area and "hidden" not in click_area.group(1)
        assert 'id="next-battle-btn"' in body


class TestManageHTMX:
    async def test_add_item_non_htmx_redirects(
        self, client: httpx2.AsyncClient
    ) -> None:
        """POST /manage/add (일반 요청) → 303 redirect"""
        resp = await client.post(
            "/manage/add",
            data={"name": "TestItem"},
            follow_redirects=False,
        )
        assert resp.status_code == 303

    async def test_edit_item_htmx_returns_row(self, client: httpx2.AsyncClient) -> None:
        """POST /manage/edit + HX-Request → 수정된 항목 행 HTML"""
        await client.post("/manage/add", data={"name": "Original"})
        resp = await client.get("/manage?tab=items")
        id_match = re.search(r'name="item_id" value="(\d+)"', resp.text)
        item_id = id_match.group(1)

        edit_resp = await client.post(
            "/manage/edit",
            data={"item_id": item_id, "new_name": "Updated"},
            headers={"HX-Request": "true"},
            follow_redirects=False,
        )
        assert edit_resp.status_code == 200
        assert "Updated" in edit_resp.text
        assert "<!DOCTYPE" not in edit_resp.text

    async def test_settings_htmx_returns_toast_trigger(
        self, client: httpx2.AsyncClient
    ) -> None:
        """POST /manage/settings + HX-Request → HX-Trigger 헤더"""
        resp = await client.post(
            "/manage/settings",
            data={"initial_sigma": "2.0", "battle_mode": "2way"},
            headers={"HX-Request": "true"},
            follow_redirects=False,
        )
        assert resp.status_code == 200
        assert "showToast" in resp.headers["HX-Trigger"]

    async def test_criteria_htmx_returns_toast_trigger(
        self, client: httpx2.AsyncClient
    ) -> None:
        """POST /manage/criteria + HX-Request → HX-Trigger 헤더"""
        resp = await client.post(
            "/manage/criteria",
            data={
                "key": ["story"],
                "label": ["Story"],
                "color": ["blue"],
                "weight": ["1.0"],
            },
            headers={"HX-Request": "true"},
            follow_redirects=False,
        )
        assert resp.status_code == 200
        assert "showToast" in resp.headers["HX-Trigger"]
