"""입력 경계와 세션 쿠키 회귀 검증."""

import httpx
import pytest
from pydantic import ValidationError

from ranker.main import app
from ranker.schemas import CriterionModel, ItemModel, SettingsModel


@pytest.mark.parametrize("weight", [float("inf"), float("nan"), -1, 0])
def test_reject_invalid_weight(weight: float) -> None:
    with pytest.raises(ValidationError):
        CriterionModel(key="name", label="이름", color="blue", weight=weight)


@pytest.mark.parametrize("value", [float("inf"), float("nan"), 1e100])
def test_reject_nonfinite_rating(value: float) -> None:
    with pytest.raises(ValidationError):
        ItemModel(id=1, name="항목", mu={"quality": value})


def test_independent_blind_defaults() -> None:
    settings = SettingsModel()
    assert "hierarchical_strength" not in settings.model_dump()
    assert settings.blind_mode


async def test_start_replaces_existing_cookie(_temp_db) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.post("/start")
        old = client.cookies.get("session_id")
        response = await client.post("/start")
        assert client.cookies.get("session_id") != old
        assert (
            len(
                [
                    v
                    for v in response.headers.get_list("set-cookie")
                    if v.startswith("session_id=")
                ]
            )
            == 1
        )


async def test_missing_htmx_session_redirects_whole_page(_temp_db) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/manage", headers={"HX-Request": "true"})
        assert response.status_code == 200
        assert response.headers["HX-Redirect"] == "/"


@pytest.mark.parametrize("field", ["initial_sigma"])
def test_reject_underflowing_scale(field: str) -> None:
    with pytest.raises(ValidationError):
        SettingsModel(**{field: 1e-200})
