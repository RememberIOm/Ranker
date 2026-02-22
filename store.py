# store.py
# 세션 기반 JSON 데이터 저장소 — 각 사용자가 독립된 데이터를 운용합니다.
# UUID 세션 ID를 키로 사용하며, 세션별 JSON 파일을 /data/sessions/ 에 저장합니다.

import os
import json
import threading
import time
from pathlib import Path
from typing import Any

# 환경 변수 SESSION_DIR이 설정되어 있으면 해당 경로를 사용하고,
# 로컬 개발 환경(uvicorn 실행)에서는 권한 오류를 피하기 위해 './data/sessions'를 사용합니다.
SESSION_DIR = Path(os.getenv("SESSION_DIR", "./data/sessions"))
SESSION_DIR.mkdir(parents=True, exist_ok=True)

# 세션 만료 시간 (7일)
SESSION_TTL_SECONDS = 7 * 24 * 60 * 60

# Thread-safe 쓰기를 위한 Lock (세션 ID별)
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _get_lock(session_id: str) -> threading.Lock:
    """세션별 Lock을 반환합니다 (lazy init)."""
    with _locks_guard:
        if session_id not in _locks:
            _locks[session_id] = threading.Lock()
        return _locks[session_id]


def _default_data() -> dict[str, Any]:
    """초기 JSON 스키마 — 새 세션 또는 파일이 없을 때 생성됩니다."""
    return {
        "settings": {
            "elo_k_max": 100,
            "elo_k_min": 30,
            "elo_decay_factor": 50,
            "match_smart_rate": 0.8,
            "match_score_range": 300,
            "elo_draw_max": 0.33,
            "elo_draw_scale": 300.0,
            "initial_rating": 1200.0,
            "normalize_target": 1200.0,
            "normalize_threshold": 1.0,
            # Battle UI 설정
            "result_auto_skip": False,
            "result_skip_seconds": 3.0,
        },
        "criteria": [
            {"key": "story", "label": "스토리", "color": "blue", "weight": 1.2},
            {"key": "visual", "label": "작화", "color": "purple", "weight": 1.0},
            {"key": "ost", "label": "OST", "color": "pink", "weight": 0.8},
            {"key": "voice", "label": "성우", "color": "green", "weight": 0.8},
            {"key": "char", "label": "캐릭터", "color": "indigo", "weight": 1.0},
            {"key": "fun", "label": "재미", "color": "red", "weight": 1.2},
        ],
        "items": [],
    }


class DataStore:
    """
    세션별 JSON 데이터 저장소.
    메모리에 데이터를 캐싱하고, 변경 시 세션 파일에 동기적으로 기록합니다.
    """

    def __init__(self, session_id: str):
        self._session_id = session_id
        self._path = SESSION_DIR / f"{session_id}.json"
        self._data: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        if self._path.exists():
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
            # 누락 필드에 기본값 병합 (하위 호환성)
            defaults = _default_data()
            for key in defaults:
                data.setdefault(key, defaults[key])
            if isinstance(data.get("settings"), dict):
                for k, v in defaults["settings"].items():
                    data["settings"].setdefault(k, v)
            return data
        return _default_data()

    def _save(self) -> None:
        lock = _get_lock(self._session_id)
        with lock:
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)

    # --- Settings ---

    @property
    def settings(self) -> dict[str, Any]:
        return self._data["settings"]

    def update_settings(self, patch: dict[str, Any]) -> None:
        self._data["settings"].update(patch)
        self._save()

    # --- Criteria ---

    @property
    def criteria(self) -> list[dict[str, Any]]:
        return self._data["criteria"]

    def set_criteria(self, criteria: list[dict[str, Any]]) -> None:
        """평가 기준 전체 교체 — 기존 아이템의 ratings도 동기화합니다."""
        old_keys = {c["key"] for c in self._data["criteria"]}
        new_keys = {c["key"] for c in criteria}
        added = new_keys - old_keys
        removed = old_keys - new_keys

        initial = self._data["settings"]["initial_rating"]

        for item in self._data["items"]:
            for key in added:
                item["ratings"].setdefault(key, initial)
            for key in removed:
                item["ratings"].pop(key, None)

        self._data["criteria"] = criteria
        self._save()

    # --- Items ---

    @property
    def items(self) -> list[dict[str, Any]]:
        return self._data["items"]

    def _next_id(self) -> int:
        if not self._data["items"]:
            return 1
        return max(item["id"] for item in self._data["items"]) + 1

    def add_item(self, name: str) -> dict[str, Any]:
        initial = self._data["settings"]["initial_rating"]
        item = {
            "id": self._next_id(),
            "name": name.strip(),
            "ratings": {c["key"]: initial for c in self._data["criteria"]},
            "matches_played": 0,
        }
        self._data["items"].append(item)
        self._save()
        return item

    def add_items_bulk(self, names: list[str]) -> int:
        """여러 항목을 한번에 추가합니다. 추가된 개수를 반환합니다."""
        count = 0
        initial = self._data["settings"]["initial_rating"]
        for name in names:
            stripped = name.strip()
            if not stripped:
                continue
            item = {
                "id": self._next_id(),
                "name": stripped,
                "ratings": {c["key"]: initial for c in self._data["criteria"]},
                "matches_played": 0,
            }
            self._data["items"].append(item)
            count += 1
        if count:
            self._save()
        return count

    def get_item(self, item_id: int) -> dict[str, Any] | None:
        for item in self._data["items"]:
            if item["id"] == item_id:
                return item
        return None

    def update_item(self, item_id: int, **fields: Any) -> bool:
        item = self.get_item(item_id)
        if not item:
            return False
        item.update(fields)
        self._save()
        return True

    def delete_item(self, item_id: int) -> bool:
        before = len(self._data["items"])
        self._data["items"] = [i for i in self._data["items"] if i["id"] != item_id]
        if len(self._data["items"]) < before:
            self._save()
            return True
        return False

    def save(self) -> None:
        """외부에서 메모리 데이터 변경 후 명시적으로 저장할 때 사용합니다."""
        self._save()

    # --- Import / Export ---

    def export_json(self) -> str:
        return json.dumps(self._data, ensure_ascii=False, indent=2)

    def import_json(self, raw: str) -> None:
        """JSON 문자열로부터 전체 데이터를 교체합니다."""
        parsed = json.loads(raw)
        defaults = _default_data()
        for key in defaults:
            parsed.setdefault(key, defaults[key])
        self._data = parsed
        self._save()

    def delete_session(self) -> None:
        """세션 데이터 파일을 삭제합니다."""
        if self._path.exists():
            self._path.unlink()


# --- 세션 관리자 ---

# 메모리 캐시: session_id → (DataStore, last_access_timestamp)
_session_cache: dict[str, tuple[DataStore, float]] = {}
_cache_lock = threading.Lock()


def get_store(session_id: str) -> DataStore:
    """세션 ID에 해당하는 DataStore를 반환합니다 (캐시 활용)."""
    with _cache_lock:
        if session_id in _session_cache:
            store, _ = _session_cache[session_id]
            _session_cache[session_id] = (store, time.time())
            return store

    store = DataStore(session_id)
    with _cache_lock:
        _session_cache[session_id] = (store, time.time())
    return store


def session_exists(session_id: str) -> bool:
    """세션 파일이 존재하는지 확인합니다."""
    return (SESSION_DIR / f"{session_id}.json").exists()


def cleanup_expired_sessions() -> int:
    """만료된 세션 파일을 정리합니다. 삭제된 개수를 반환합니다."""
    now = time.time()
    removed = 0
    for f in SESSION_DIR.glob("*.json"):
        if (now - f.stat().st_mtime) > SESSION_TTL_SECONDS:
            f.unlink(missing_ok=True)
            with _cache_lock:
                _session_cache.pop(f.stem, None)
            removed += 1
    return removed
