import tempfile
from collections.abc import Callable, Coroutine
from pathlib import Path

import pytest

import store


@pytest.fixture()
async def _temp_session_dir():
    """임시 디렉토리를 생성하고 store.SESSION_DIR을 교체합니다."""
    tempdir = tempfile.TemporaryDirectory()
    original_session_dir = store.SESSION_DIR
    store.SESSION_DIR = Path(tempdir.name)
    store.SESSION_DIR.mkdir(parents=True, exist_ok=True)
    store._session_cache.clear()
    store._locks.clear()
    yield
    store._session_cache.clear()
    store._locks.clear()
    store.SESSION_DIR = original_session_dir
    tempdir.cleanup()


@pytest.fixture()
async def temp_store(_temp_session_dir) -> store.DataStore:
    """기본 세션 ID로 DataStore를 반환합니다."""
    return await store.get_store("a" * 32)


@pytest.fixture()
async def store_factory(
    _temp_session_dir,
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
