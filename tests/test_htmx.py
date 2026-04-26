"""HTMX partial 응답 통합 테스트"""

import httpx
import pytest
from httpx import ASGITransport

from main import app


@pytest.fixture()
async def client(_temp_db) -> httpx.AsyncClient:
    """세션이 설정된 테스트 클라이언트를 반환합니다."""
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        # 새 세션 생성
        resp = await c.post("/start", follow_redirects=False)
        assert resp.status_code == 303
        yield c


async def _setup_battle(client: httpx.AsyncClient) -> None:
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


class TestBattleHTMX:
    async def test_get_battle_htmx_returns_partial(
        self, client: httpx.AsyncClient
    ) -> None:
        """GET /battle + HX-Request → partial HTML (<!DOCTYPE 없음)"""
        await _setup_battle(client)
        resp = await client.get("/battle", headers={"HX-Request": "true"})
        assert resp.status_code == 200
        assert "<!DOCTYPE" not in resp.text
        assert "battle-state" in resp.text
        assert "criteria-list" in resp.text

    async def test_get_battle_normal_returns_full_page(
        self, client: httpx.AsyncClient
    ) -> None:
        """GET /battle (일반 요청) → full HTML"""
        await _setup_battle(client)
        resp = await client.get("/battle")
        assert resp.status_code == 200
        assert "<!DOCTYPE" in resp.text

    async def test_post_vote_htmx_returns_html(self, client: httpx.AsyncClient) -> None:
        """POST /battle/vote + HX-Request → 결과 모달 HTML + OOB swap"""
        await _setup_battle(client)
        # 배틀 페이지에서 라운드 토큰 추출
        resp = await client.get("/battle")
        text = resp.text
        # round_token 추출
        import re

        token_match = re.search(r'data-round-token="([^"]+)"', text)
        assert token_match, "라운드 토큰을 찾을 수 없습니다"
        round_token = token_match.group(1)

        # item IDs 추출
        id1_match = re.search(r'data-item1-id="(\d+)"', text)
        id2_match = re.search(r'data-item2-id="(\d+)"', text)
        assert id1_match and id2_match
        item1_id = int(id1_match.group(1))
        item2_id = int(id2_match.group(1))

        vote_resp = await client.post(
            "/battle/vote",
            json={
                "item1_id": item1_id,
                "item2_id": item2_id,
                "round_token": round_token,
                "votes": {"story": "1"},
            },
            headers={"HX-Request": "true"},
        )
        assert vote_resp.status_code == 200
        assert "result-modal" in vote_resp.text
        assert "hx-swap-oob" in vote_resp.text

    async def test_post_vote_non_htmx_returns_json(
        self, client: httpx.AsyncClient
    ) -> None:
        """POST /battle/vote (일반 요청) → JSON"""
        await _setup_battle(client)
        resp = await client.get("/battle")
        import re

        token_match = re.search(r'data-round-token="([^"]+)"', resp.text)
        id1_match = re.search(r'data-item1-id="(\d+)"', resp.text)
        id2_match = re.search(r'data-item2-id="(\d+)"', resp.text)

        vote_resp = await client.post(
            "/battle/vote",
            json={
                "item1_id": int(id1_match.group(1)),
                "item2_id": int(id2_match.group(1)),
                "round_token": token_match.group(1),
                "votes": {"story": "1"},
            },
        )
        assert vote_resp.status_code == 200
        data = vote_resp.json()
        assert "results" in data
        assert "next_url" in data

    async def test_result_modal_has_hold_button_when_auto_skip(
        self, client: httpx.AsyncClient
    ) -> None:
        """auto_skip 활성화 시 결과 모달이 "유지" 버튼과 노출된 auto-skip-area를 포함한다."""
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

        resp = await client.get("/battle")
        import re

        token = re.search(r'data-round-token="([^"]+)"', resp.text).group(1)
        id1 = int(re.search(r'data-item1-id="(\d+)"', resp.text).group(1))
        id2 = int(re.search(r'data-item2-id="(\d+)"', resp.text).group(1))

        vote_resp = await client.post(
            "/battle/vote",
            json={
                "item1_id": id1,
                "item2_id": id2,
                "round_token": token,
                "votes": {"story": "1"},
            },
            headers={"HX-Request": "true"},
        )
        assert vote_resp.status_code == 200
        body = vote_resp.text
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
        # 유지 버튼이 존재해야 함
        assert "cancelAutoSkip()" in body
        assert "유지" in body

    async def test_result_modal_no_hold_button_when_manual(
        self, client: httpx.AsyncClient
    ) -> None:
        """auto_skip 비활성화(기본) 시 결과 모달은 click-skip 영역만 노출한다."""
        await _setup_battle(client)

        resp = await client.get("/battle")
        import re

        token = re.search(r'data-round-token="([^"]+)"', resp.text).group(1)
        id1 = int(re.search(r'data-item1-id="(\d+)"', resp.text).group(1))
        id2 = int(re.search(r'data-item2-id="(\d+)"', resp.text).group(1))

        vote_resp = await client.post(
            "/battle/vote",
            json={
                "item1_id": id1,
                "item2_id": id2,
                "round_token": token,
                "votes": {"story": "1"},
            },
            headers={"HX-Request": "true"},
        )
        assert vote_resp.status_code == 200
        body = vote_resp.text
        assert 'data-auto-skip="false"' in body
        # auto_skip 비활성 → auto-skip-area는 hidden
        auto_area = re.search(r'<div id="auto-skip-area"\s+class="([^"]*)"', body)
        assert auto_area and "hidden" in auto_area.group(1)
        # auto_skip 비활성 → click-skip-area는 hidden 아님 (다음 대결 버튼 노출)
        click_area = re.search(r'<div id="click-skip-area"\s+class="([^"]*)"', body)
        assert click_area and "hidden" not in click_area.group(1)
        assert 'id="next-battle-btn"' in body


class TestManageHTMX:
    async def test_add_item_htmx_returns_list(self, client: httpx.AsyncClient) -> None:
        """POST /manage/add + HX-Request → 항목 리스트 HTML"""
        resp = await client.post(
            "/manage/add",
            data={"name": "TestItem"},
            headers={"HX-Request": "true"},
            follow_redirects=False,
        )
        assert resp.status_code == 200
        assert "TestItem" in resp.text
        assert "<!DOCTYPE" not in resp.text

    async def test_add_item_non_htmx_redirects(self, client: httpx.AsyncClient) -> None:
        """POST /manage/add (일반 요청) → 303 redirect"""
        resp = await client.post(
            "/manage/add",
            data={"name": "TestItem"},
            follow_redirects=False,
        )
        assert resp.status_code == 303

    async def test_delete_item_htmx_returns_empty(
        self, client: httpx.AsyncClient
    ) -> None:
        """POST /manage/delete + HX-Request → 빈 응답"""
        # 항목 추가
        await client.post("/manage/add", data={"name": "ToDelete"})
        # 항목 ID 얻기
        resp = await client.get("/manage?tab=items")
        import re

        id_match = re.search(r'name="item_id" value="(\d+)"', resp.text)
        assert id_match
        item_id = id_match.group(1)

        del_resp = await client.post(
            "/manage/delete",
            data={"item_id": item_id},
            headers={"HX-Request": "true"},
            follow_redirects=False,
        )
        assert del_resp.status_code == 200
        assert del_resp.text == ""

    async def test_edit_item_htmx_returns_row(self, client: httpx.AsyncClient) -> None:
        """POST /manage/edit + HX-Request → 수정된 항목 행 HTML"""
        await client.post("/manage/add", data={"name": "Original"})
        resp = await client.get("/manage?tab=items")
        import re

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
        self, client: httpx.AsyncClient
    ) -> None:
        """POST /manage/settings + HX-Request → HX-Trigger 헤더"""
        resp = await client.post(
            "/manage/settings",
            data={"initial_sigma": "2.0", "battle_mode": "2way"},
            headers={"HX-Request": "true"},
            follow_redirects=False,
        )
        assert resp.status_code == 200
        assert "HX-Trigger" in resp.headers or "hx-trigger" in resp.headers
        trigger = resp.headers.get("HX-Trigger") or resp.headers.get("hx-trigger")
        assert "showToast" in trigger

    async def test_criteria_htmx_returns_toast_trigger(
        self, client: httpx.AsyncClient
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
        trigger = resp.headers.get("HX-Trigger") or resp.headers.get("hx-trigger")
        assert trigger and "showToast" in trigger
