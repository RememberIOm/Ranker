import tempfile
from collections.abc import Callable, Coroutine
from pathlib import Path

import pytest

import database
import store


@pytest.fixture()
async def _temp_db():
    """임시 SQLite DB를 생성하고 스키마를 초기화합니다."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)

    original_path = database.DB_PATH
    database.DB_PATH = db_path
    await database.init_db()
    store._locks.clear()
    yield
    store._locks.clear()
    await database.close_db()
    database.DB_PATH = original_path
    db_path.unlink(missing_ok=True)


@pytest.fixture()
async def temp_store(_temp_db) -> store.DataStore:
    """기본 세션 ID로 DataStore를 반환합니다."""
    s = await store.get_store("a" * 32)
    await s.save()
    return s


@pytest.fixture()
async def store_factory(
    _temp_db,
) -> Callable[[str], Coroutine[None, None, store.DataStore]]:
    """임의 session_id로 DataStore를 생성할 수 있는 팩토리"""
    return store.get_store


@pytest.fixture()
async def store_with_items(temp_store: store.DataStore) -> store.DataStore:
    """2개 항목(Alpha, Beta)이 추가된 DataStore를 반환합니다."""
    await temp_store.add_item("Alpha")
    await temp_store.add_item("Beta")
    return temp_store


@pytest.fixture()
async def store_with_three_items(temp_store: store.DataStore) -> store.DataStore:
    """3개 항목(Alpha, Beta, Gamma)이 추가된 DataStore를 반환합니다."""
    await temp_store.add_item("Alpha")
    await temp_store.add_item("Beta")
    await temp_store.add_item("Gamma")
    return temp_store
