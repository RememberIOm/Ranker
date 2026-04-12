import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

import store


class StoreValidationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_session_dir = store.SESSION_DIR
        store.SESSION_DIR = Path(self.tempdir.name)
        store.SESSION_DIR.mkdir(parents=True, exist_ok=True)
        store._session_cache.clear()
        store._locks.clear()

    async def asyncTearDown(self) -> None:
        store._session_cache.clear()
        store._locks.clear()
        store.SESSION_DIR = self.original_session_dir
        self.tempdir.cleanup()

    async def test_import_json_repairs_missing_ratings(self) -> None:
        """import_json은 _load와 동일한 관대 파싱을 사용하여 누락된 ratings를 자동 보정합니다."""
        session = await store.get_store("a" * 32)

        await session.import_json(
            """
            {
              "criteria": [
                {"key": "story", "label": "스토리", "color": "blue", "weight": 1.0}
              ],
              "items": [
                {"id": 1, "name": "Alpha", "ratings": {}, "matches_played": 0}
              ]
            }
            """
        )

        self.assertEqual(session.items[0]["ratings"]["story"], 1200.0)

    async def test_delete_session_clears_runtime_cache_and_lock(self) -> None:
        session_id = "b" * 32
        session = await store.get_store(session_id)
        await session.save()
        store._get_lock(session_id)

        session.delete_session()

        self.assertFalse((store.SESSION_DIR / f"{session_id}.json").exists())
        self.assertNotIn(session_id, store._session_cache)
        self.assertNotIn(session_id, store._locks)

    async def test_load_legacy_session_repairs_missing_ratings(self) -> None:
        session_id = "c" * 32
        legacy_payload = """
        {
          "settings": {
            "initial_rating": 1400
          },
          "criteria": [
            {"key": "story", "label": "스토리", "color": "blue"},
            {"key": "visual", "label": "작화", "color": "purple"}
          ],
          "items": [
            {"id": 1, "name": "Alpha", "ratings": {"story": 1510}, "matches_played": 3}
          ]
        }
        """
        (store.SESSION_DIR / f"{session_id}.json").write_text(legacy_payload, encoding="utf-8")

        session = await store.get_store(session_id)

        self.assertEqual(session.items[0]["ratings"]["story"], 1510.0)
        self.assertEqual(session.items[0]["ratings"]["visual"], 1400.0)
