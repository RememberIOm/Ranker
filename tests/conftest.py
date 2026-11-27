from collections.abc import AsyncIterator, Callable, Coroutine
from pathlib import Path

import httpx2
import pytest

from ranker import database, store
from ranker.main import app


@pytest.fixture()
async def _temp_db(tmp_path: Path):
    """임시 SQLite DB를 생성하고 스키마를 초기화합니다."""
    original_path = database.DB_PATH
    database.DB_PATH = tmp_path / "ranker.db"
    await database.init_db()
    store._locks.clear()
    yield
    store._locks.clear()
    await database.close_db()
    database.DB_PATH = original_path


@pytest.fixture()
async def client(_temp_db) -> AsyncIterator[httpx2.AsyncClient]:
    """세션이 열린 테스트 HTTP 클라이언트를 반환합니다."""
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://test"
    ) as c:
        resp = await c.post("/start", follow_redirects=False)
        assert resp.status_code == 303
        yield c


@pytest.fixture()
async def temp_store(_temp_db) -> store.DataStore:
    """기본 세션 ID로 새 DataStore를 반환합니다."""
    return await store.create_store("a" * 32, "test-library", "테스트 랭킹")


@pytest.fixture()
async def store_factory(
    _temp_db,
) -> Callable[[str], Coroutine[None, None, store.DataStore]]:
    """임의 session_id로 새 DataStore를 만드는 팩토리"""
    return lambda session_id: store.create_store(
        session_id, "test-library", "테스트 랭킹"
    )


@pytest.fixture()
async def store_with_items(temp_store: store.DataStore) -> store.DataStore:
    """2개 항목(Alpha, Beta)이 추가된 DataStore를 반환합니다."""
    await temp_store.add_items(["Alpha", "Beta"])
    return temp_store


@pytest.fixture()
async def store_with_three_items(temp_store: store.DataStore) -> store.DataStore:
    """3개 항목(Alpha, Beta, Gamma)이 추가된 DataStore를 반환합니다."""
    await temp_store.add_items(["Alpha", "Beta", "Gamma"])
    return temp_store
