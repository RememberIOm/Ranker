# store.py
# 세션 기반 SQLite 데이터 저장소 — 각 사용자가 독립된 데이터를 운용합니다.
# UUID 세션 ID를 키로 사용하며, 단일 SQLite DB에 모든 세션을 저장합니다.

import asyncio
import json
import logging
import os
import secrets
import time
import sqlite3
import hashlib
import database
from copy import deepcopy
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from weakref import WeakValueDictionary
from itertools import groupby
from typing import Any

from pydantic import ValidationError
from rating_engine import update_ratings, ALGORITHM_VERSION

from database import (
    get_db,
    _insert_session_data,
    transaction,
    StaleSessionError,
)
from schemas import (
    ActiveRoundModel,
    MAX_BACKUP_BYTES,
    BattleVoteRequest,
    SessionDataModel,
    ThreeWayBattleVoteRequest,
)

logger = logging.getLogger("ranker.store")

# 세션 만료 시간 (7일)
SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", str(7 * 24 * 60 * 60)))

# asyncio.Lock은 실행 중인 이벤트 루프 안에서 생성해야 하므로 lazy init
# cooperative scheduling 덕분에 await 없는 구간은 원자적 — dict 가드 불필요
# ⚠️ 단일 uvicorn 워커 전제 — 멀티 워커(Gunicorn) 환경에서는 프로세스 간 Lock을
#    공유할 수 없으므로 filelock 패키지로 교체 필요. fly.toml 참고.
_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()


class InvalidBattleVoteError(ValueError):
    """투표 페이로드가 현재 세션 상태와 맞지 않을 때 발생합니다."""


class StaleBattleRoundError(RuntimeError):
    """이미 처리되었거나 만료된 대결 라운드일 때 발생합니다."""


class BattleItemNotFoundError(LookupError):
    """대결 중인 항목을 찾을 수 없을 때 발생합니다."""


class SessionSaveError(RuntimeError):
    """세션 저장에 실패했을 때 발생합니다 (디스크 풀, 권한 거부 등)."""


class InvalidSessionDataError(ValueError):
    """세션 데이터가 손상되었거나 현재 스키마로 복구할 수 없을 때 발생합니다."""


def _get_lock(session_id: str) -> asyncio.Lock:
    """세션별 asyncio.Lock을 반환합니다 (lazy init)."""
    lock = _locks.get(session_id)
    if lock is None:
        lock = asyncio.Lock()
        _locks[session_id] = lock
    return lock


def _default_data() -> dict[str, Any]:
    """초기 스키마 — 새 세션 생성 시 사용됩니다."""
    return SessionDataModel().model_dump(mode="python")


_VOTE_OUTCOMES = {"1": 1.0, "2": 0.0, "draw": 0.5}


def _coerce(value: Any, cast: type, default: Any) -> Any:
    """형변환 실패 시 default를 반환하는 관대 변환 — 과거 포맷 보정용."""
    try:
        return cast(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _normalize_loaded_data(data: Any) -> dict[str, Any]:
    """과거 세션 포맷을 현재 스키마로 최대한 보정합니다."""
    defaults = _default_data()
    if not isinstance(data, dict):
        raise InvalidSessionDataError("세션 최상위 구조가 객체가 아닙니다.")

    settings_raw = data.get("settings")
    settings = defaults["settings"].copy()
    if isinstance(settings_raw, dict):
        # Elo→BT 마이그레이션: 구 설정 키 감지 시 변환
        if "elo_draw_max" in settings_raw and "draw_prior_max" not in settings_raw:
            settings["draw_prior_max"] = settings_raw.get("elo_draw_max", 0.33)
            settings["draw_prior_strength"] = 10
            draw_scale = _coerce(settings_raw.get("elo_draw_scale", 300.0), float, None)
            settings["draw_bandwidth"] = (
                draw_scale / 173.72 if draw_scale is not None else 1.5
            )
            settings["initial_sigma"] = 2.0
            settings["hierarchical_strength"] = 5.0
            settings["display_center"] = _coerce(
                settings_raw.get("initial_rating", 1200.0), float, 1200.0
            )
            settings["display_scale"] = 173.72
            if "result_auto_skip" in settings_raw:
                settings["result_auto_skip"] = settings_raw["result_auto_skip"]
            if "result_skip_seconds" in settings_raw:
                settings["result_skip_seconds"] = settings_raw["result_skip_seconds"]
        else:
            for key in settings:
                if key in settings_raw:
                    settings[key] = settings_raw[key]

    criteria_raw = data.get("criteria")
    criteria: list[dict[str, Any]] = []
    if isinstance(criteria_raw, list):
        seen_keys: set[str] = set()
        for raw_criterion in criteria_raw:
            if not isinstance(raw_criterion, dict):
                continue

            key = raw_criterion.get("key")
            label = raw_criterion.get("label")
            if not isinstance(key, str) or not key.strip():
                continue
            if not isinstance(label, str) or not label.strip():
                continue

            normalized_key = key.strip()
            if normalized_key in seen_keys:
                continue
            seen_keys.add(normalized_key)

            color = raw_criterion.get("color")
            if not isinstance(color, str) or not color.strip():
                color = "gray"

            normalized_weight = _coerce(raw_criterion.get("weight", 1.0), float, 1.0)
            if normalized_weight <= 0:
                normalized_weight = 1.0

            normalized_battles = max(
                0, _coerce(raw_criterion.get("battles", 0), int, 0)
            )
            normalized_draws = max(0, _coerce(raw_criterion.get("draws", 0), int, 0))
            # 손상된 import 방어: draws > battles면 Beta prior beta_param이 음수가 될 수 있음.
            # 무승부는 전체 배틀의 부분집합이라는 invariant를 강제 보정.
            normalized_draws = min(normalized_draws, normalized_battles)

            criteria.append(
                {
                    "key": normalized_key,
                    "label": label.strip(),
                    "color": color.strip(),
                    "weight": normalized_weight,
                    "battles": normalized_battles,
                    "draws": normalized_draws,
                }
            )

    if not criteria:
        criteria = defaults["criteria"]

    default_sigma = float(defaults["settings"]["initial_sigma"])
    initial_sigma = _coerce(
        settings.get("initial_sigma", default_sigma), float, default_sigma
    )
    initial_sigma_sq = initial_sigma**2

    display_center = _coerce(settings.get("display_center", 1200.0), float, 1200.0)
    display_scale = _coerce(settings.get("display_scale", 173.72), float, 173.72)
    if display_scale <= 0:
        display_scale = 173.72

    items_raw = data.get("items")
    items: list[dict[str, Any]] = []
    if isinstance(items_raw, list):
        seen_ids: set[int] = set()
        next_generated_id = 1
        allowed_keys = [criterion["key"] for criterion in criteria]

        for raw_item in items_raw:
            if not isinstance(raw_item, dict):
                continue

            item_id = raw_item.get("id")
            if not isinstance(item_id, int) or item_id <= 0 or item_id in seen_ids:
                while next_generated_id in seen_ids:
                    next_generated_id += 1
                item_id = next_generated_id
            seen_ids.add(item_id)
            next_generated_id = max(next_generated_id, item_id + 1)

            name = raw_item.get("name")
            if not isinstance(name, str) or not name.strip():
                name = f"Item {item_id}"

            matches_played = max(0, _coerce(raw_item.get("matches_played", 0), int, 0))

            criterion_matches_raw = raw_item.get("criterion_matches")
            if not isinstance(criterion_matches_raw, dict):
                criterion_matches_raw = {}
            criterion_matches: dict[str, int] = {
                key: max(0, _coerce(criterion_matches_raw.get(key, 0), int, 0))
                for key in allowed_keys
            }

            # Elo→BT 마이그레이션: "ratings" 존재 + "mu" 부재 시 변환
            mu_raw = raw_item.get("mu")
            ratings_raw = raw_item.get("ratings")
            is_legacy = isinstance(ratings_raw, dict) and not isinstance(mu_raw, dict)

            if is_legacy:
                mu: dict[str, float] = {}
                sigma_sq: dict[str, float] = {}
                for key in allowed_keys:
                    old_r = _coerce(
                        ratings_raw.get(key, display_center), float, display_center
                    )
                    mu[key] = (old_r - display_center) / display_scale
                    cm = criterion_matches.get(key, 0)
                    sigma_sq[key] = max(0.1, initial_sigma_sq / (1.0 + cm * 0.25))
            else:
                if not isinstance(mu_raw, dict):
                    mu_raw = {}
                sigma_sq_raw = raw_item.get("sigma_sq")
                if not isinstance(sigma_sq_raw, dict):
                    sigma_sq_raw = {}
                mu = {
                    key: _coerce(mu_raw.get(key, 0.0), float, 0.0)
                    for key in allowed_keys
                }
                sigma_sq = {
                    key: max(
                        0.01,
                        _coerce(
                            sigma_sq_raw.get(key, initial_sigma_sq),
                            float,
                            initial_sigma_sq,
                        ),
                    )
                    for key in allowed_keys
                }

            items.append(
                {
                    "id": item_id,
                    "name": name.strip(),
                    "mu": mu,
                    "sigma_sq": sigma_sq,
                    "matches_played": matches_played,
                    "criterion_matches": criterion_matches,
                }
            )

    # active_round (진행 중인 배틀 라운드) 복원 — DB에 영속화되어 VM 재시작 후에도 투표 가능.
    # 검증 실패(같은 ID, 잘못된 토큰 등) 시 None으로 관대 복원 — 전체 로드 실패를 피함.
    active_round: dict[str, Any] | None = None
    if isinstance(data.get("active_round"), dict):
        try:
            active_round = ActiveRoundModel.model_validate(
                data["active_round"]
            ).model_dump(mode="python")
        except ValidationError:
            active_round = None

    return {
        "settings": settings,
        "criteria": criteria,
        "items": items,
        "active_round": active_round,
    }


class DataStore:
    """
    세션별 SQLite 데이터 저장소.
    메모리에 데이터를 로드하고, 변경 시 SQLite에 비동기적으로 기록합니다.
    직접 생성하지 말고 DataStore.create(session_id)를 사용하세요.
    """

    def __init__(self, session_id: str) -> None:
        self._session_id = session_id
        self._data: dict[str, Any] = {}  # create()에서 채워짐 (active_round 포함)
        self._created_at: float = 0.0
        self._revision: int | None = None
        self._history: list[dict[str, Any]] = []
        self._baseline: dict[str, Any] | None = None
        self._replaying = False

    @classmethod
    async def create(cls, session_id: str) -> "DataStore":
        """비동기 팩토리 — DB에서 데이터를 로드한 DataStore를 반환합니다."""
        instance = cls(session_id)
        await instance._load_from_db()
        return instance

    async def _load_from_db(self) -> None:
        """같은 커넥션의 쓰기 중간 상태를 읽지 않습니다."""
        async with database.db_write_lock:
            try:
                await self._read_snapshot()
                self._data = SessionDataModel.model_validate(self._data).model_dump(
                    mode="python"
                )
            except (ValidationError, json.JSONDecodeError) as exc:
                raise InvalidSessionDataError(
                    "저장된 데이터를 읽을 수 없습니다. 백업으로 복구해주세요."
                ) from exc

    async def _read_snapshot(self) -> None:
        """SQLite에서 전체 세션 데이터를 메모리 dict로 조립합니다."""
        db = get_db()

        # 세션 메타
        async with db.execute(
            "SELECT settings, created_at, revision FROM sessions WHERE id = ?",
            (self._session_id,),
        ) as cursor:
            row = await cursor.fetchone()

        if row is None:
            if self._revision is not None:
                raise StaleSessionError("세션이 삭제되었습니다. 새로 시작해주세요.")
            self._data = _default_data()
            self._created_at = time.time()
            return

        self._revision = row["revision"]
        self._created_at = row["created_at"]
        self._data = {
            "settings": json.loads(row["settings"]),
            "criteria": [],
            "items": [],
            "active_round": None,
        }

        # 기준
        async with db.execute(
            "SELECT key, label, color, weight, battles, draws "
            "FROM criteria WHERE session_id = ? ORDER BY sort_order",
            (self._session_id,),
        ) as cursor:
            self._data["criteria"] = [
                {
                    "key": r["key"],
                    "label": r["label"],
                    "color": r["color"],
                    "weight": r["weight"],
                    "battles": r["battles"],
                    "draws": r["draws"],
                }
                async for r in cursor
            ]

        # 항목 + 레이팅 (JOIN으로 한번에 조회)
        async with db.execute(
            "SELECT i.id, i.name, i.matches_played, "
            "       r.criterion_key, r.mu, r.sigma_sq, r.criterion_matches "
            "FROM items i "
            "LEFT JOIN item_ratings r ON i.session_id = r.session_id AND i.id = r.item_id "
            "WHERE i.session_id = ? "
            "ORDER BY i.id, r.criterion_key",
            (self._session_id,),
        ) as cursor:
            rows = await cursor.fetchall()

        items: list[dict[str, Any]] = []
        for item_id, group in groupby(rows, key=lambda r: r["id"]):
            mu: dict[str, float] = {}
            sigma_sq: dict[str, float] = {}
            criterion_matches: dict[str, int] = {}
            name = ""
            matches_played = 0
            for r in group:
                name = r["name"]
                matches_played = r["matches_played"]
                if r["criterion_key"] is not None:
                    mu[r["criterion_key"]] = r["mu"]
                    sigma_sq[r["criterion_key"]] = r["sigma_sq"]
                    criterion_matches[r["criterion_key"]] = r["criterion_matches"]
            items.append(
                {
                    "id": item_id,
                    "name": name,
                    "mu": mu,
                    "sigma_sq": sigma_sq,
                    "matches_played": matches_played,
                    "criterion_matches": criterion_matches,
                }
            )
        self._data["items"] = items

        # 진행 중 라운드
        async with db.execute(
            "SELECT token, item1_id, item2_id, item3_id, issued_at "
            "FROM active_rounds WHERE session_id = ?",
            (self._session_id,),
        ) as cursor:
            ar_row = await cursor.fetchone()

        if ar_row:
            ar: dict[str, Any] = {
                "token": ar_row["token"],
                "item1_id": ar_row["item1_id"],
                "item2_id": ar_row["item2_id"],
                "issued_at": ar_row["issued_at"],
            }
            if ar_row["item3_id"] is not None:
                ar["item3_id"] = ar_row["item3_id"]
            self._data["active_round"] = ar

        async with db.execute(
            "SELECT event FROM vote_events WHERE session_id = ? ORDER BY id",
            (self._session_id,),
        ) as cursor:
            self._history = [
                json.loads(row["event"]) for row in await cursor.fetchall()
            ]
        async with db.execute(
            "SELECT state FROM history_baselines WHERE session_id = ?",
            (self._session_id,),
        ) as cursor:
            baseline = await cursor.fetchone()
            self._baseline = (
                json.loads(baseline["state"]) if baseline else self._snapshot()
            )

    def _snapshot(self) -> dict[str, Any]:
        data = deepcopy(self._data)
        data["active_round"] = None
        return data

    def _reset_history(self) -> None:
        """원시 이력을 보존하고 구조 변경 이후를 새 재계산 기준점으로 삼습니다."""
        for event in self._history:
            event["archived"] = True
        self._baseline = self._snapshot()

    @property
    def active_round(self) -> dict[str, Any] | None:
        return deepcopy(self._data.get("active_round"))

    @property
    def history(self) -> list[dict[str, Any]]:
        return deepcopy(self._history)

    async def _save_to_db(self) -> None:
        """메모리 상태를 SQLite에 기록합니다 (단일 트랜잭션)."""
        if len(self.export_json().encode("utf-8")) > MAX_BACKUP_BYTES:
            raise SessionSaveError(
                "랭킹의 백업 한도(64MB)에 도달했습니다. 백업 후 투표 이력을 정리해주세요."
            )
        try:
            self._revision = await _insert_session_data(
                get_db(),
                self._session_id,
                self._data,
                created_at=self._created_at,
                last_accessed=time.time(),
                expected_revision=self._revision,
                history=self._history,
                baseline=self._baseline or self._snapshot(),
            )
        except (OSError, sqlite3.Error) as exc:
            logger.error(
                "session_save_failed — session_id=%s: %s", self._session_id, exc
            )
            raise SessionSaveError(
                f"세션 저장에 실패했습니다: {self._session_id}"
            ) from exc

    def _invalidate_active_round(self) -> None:
        """진행 중인 라운드를 무효화합니다. 호출자가 _save_to_db()로 DB 반영을 책임집니다."""
        self._data["active_round"] = None

    def get_item(self, item_id: int) -> dict[str, Any] | None:
        for item in self._data["items"]:
            if item["id"] == item_id:
                return item
        return None

    def _validate_active_round(self, payload: Any, item3_id: int | None = None) -> None:
        """라운드 토큰과 항목 구성이 현재 active_round와 일치하는지 검증합니다."""
        ar = self._data.get("active_round")
        if (
            not ar
            or ar["token"] != payload.round_token
            or ar["item1_id"] != payload.item1_id
            or ar["item2_id"] != payload.item2_id
            or ar.get("item3_id") != item3_id
        ):
            raise StaleBattleRoundError(
                "이 대결은 만료되었거나 이미 처리되었습니다. 새로고침 후 다시 시도해주세요."
            )

    def _validate_vote_keys(self, votes: dict[str, Any]) -> None:
        """제출된 투표 기준이 현재 criteria와 정확히 일치하는지 검증합니다."""
        allowed = {criterion["key"] for criterion in self._data["criteria"]}
        submitted = set(votes)
        if unknown := submitted - allowed:
            raise InvalidBattleVoteError(
                f"알 수 없는 투표 기준이 포함되어 있습니다: {sorted(unknown)}"
            )
        if missing := allowed - submitted:
            raise InvalidBattleVoteError(
                f"투표가 누락된 기준이 있습니다: {sorted(missing)}"
            )

    # --- Settings ---

    @property
    def settings(self) -> dict[str, Any]:
        return self._data["settings"]

    async def update_settings(self, patch: dict[str, Any]) -> None:
        async with _get_lock(self._session_id):
            await self._load_from_db()
            self._data["settings"].update(patch)
            await self._save_to_db()

    # --- Criteria ---

    @property
    def criteria(self) -> list[dict[str, Any]]:
        return self._data["criteria"]

    async def set_criteria(self, criteria: list[dict[str, Any]]) -> None:
        """평가 기준 전체 교체 — 기존 아이템의 mu/sigma_sq도 동기화합니다."""
        async with _get_lock(self._session_id):
            await self._load_from_db()
            old_keys = {c["key"] for c in self._data["criteria"]}
            new_keys = {c["key"] for c in criteria}
            added = new_keys - old_keys
            removed = old_keys - new_keys

            initial_sq = self._data["settings"]["initial_sigma"] ** 2

            for item in self._data["items"]:
                cm = item.setdefault("criterion_matches", {})
                for key in added:
                    item["mu"].setdefault(key, 0.0)
                    item["sigma_sq"].setdefault(key, initial_sq)
                    cm.setdefault(key, 0)
                for key in removed:
                    item["mu"].pop(key, None)
                    item["sigma_sq"].pop(key, None)
                    cm.pop(key, None)

            # 기존 기준의 배틀 통계(draws/battles) 보존 — key가 동일하면 이력 유지
            old_stats = {
                c["key"]: {"battles": c.get("battles", 0), "draws": c.get("draws", 0)}
                for c in self._data["criteria"]
            }
            for c in criteria:
                if c["key"] in old_stats:
                    c.setdefault("battles", old_stats[c["key"]]["battles"])
                    c.setdefault("draws", old_stats[c["key"]]["draws"])

            self._data["criteria"] = criteria
            self._invalidate_active_round()
            self._reset_history()
            await self._save_to_db()

    # --- Items ---

    @property
    def items(self) -> list[dict[str, Any]]:
        return self._data["items"]

    def _next_id(self) -> int:
        if not self._data["items"]:
            return 1
        return max(item["id"] for item in self._data["items"]) + 1

    def _new_item(self, item_id: int, name: str) -> dict[str, Any]:
        initial_sq = self._data["settings"]["initial_sigma"] ** 2
        keys = [c["key"] for c in self._data["criteria"]]
        return {
            "id": item_id,
            "name": name,
            "mu": {k: 0.0 for k in keys},
            "sigma_sq": {k: initial_sq for k in keys},
            "matches_played": 0,
            "criterion_matches": {k: 0 for k in keys},
        }

    async def add_item(self, name: str) -> dict[str, Any]:
        async with _get_lock(self._session_id):
            await self._load_from_db()
            item = self._new_item(self._next_id(), name.strip())
            self._data["items"].append(item)
            self._invalidate_active_round()
            self._reset_history()
            await self._save_to_db()
            return item

    async def add_items_bulk(self, names: list[str]) -> int:
        """여러 항목을 한번에 추가합니다. 추가된 개수를 반환합니다."""
        async with _get_lock(self._session_id):
            await self._load_from_db()
            stripped = [n.strip() for n in names if n.strip()]
            next_id = self._next_id()
            for offset, name in enumerate(stripped):
                self._data["items"].append(self._new_item(next_id + offset, name))
            if stripped:
                self._invalidate_active_round()
                self._reset_history()
                await self._save_to_db()
            return len(stripped)

    async def update_item(self, item_id: int, **fields: Any) -> bool:
        async with _get_lock(self._session_id):
            await self._load_from_db()
            item = self.get_item(item_id)
            if not item:
                return False
            item.update(fields)
            if set(fields) - {"name"}:
                self._reset_history()
            await self._save_to_db()
            return True

    async def delete_item(self, item_id: int) -> bool:
        async with _get_lock(self._session_id):
            await self._load_from_db()
            before = len(self._data["items"])
            self._data["items"] = [i for i in self._data["items"] if i["id"] != item_id]
            if len(self._data["items"]) < before:
                self._invalidate_active_round()
                self._reset_history()
                await self._save_to_db()
                return True
            return False

    async def save(self) -> None:
        """외부에서 메모리 데이터 변경 후 명시적으로 저장할 때 사용합니다."""
        async with _get_lock(self._session_id):
            self._reset_history()
            await self._save_to_db()

    # --- Import / Export ---

    def export_json(self) -> str:
        envelope = {
            "schema_version": 2,
            **self._snapshot(),
            "history": self.history,
            "history_baseline": self._baseline or self._snapshot(),
        }
        return json.dumps(
            envelope, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        )

    @staticmethod
    def parse_import(raw: str) -> dict[str, Any]:
        """현행 백업은 엄격히 검증하고 버전 없는 과거 파일만 보정합니다."""
        parsed = json.loads(
            raw,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"유한하지 않은 수치: {value}")
            ),
        )
        if (
            not isinstance(parsed, dict)
            or not isinstance(parsed.get("items"), list)
            or not isinstance(parsed.get("criteria"), list)
        ):
            raise InvalidSessionDataError(
                "항목과 기준이 포함된 백업 파일이 필요합니다."
            )
        version = parsed.get("schema_version")
        if version not in (None, 1, 2):
            raise InvalidSessionDataError("지원하지 않는 백업 버전입니다.")
        if version == 2:
            if "settings" not in parsed or (
                parsed.get("history") and "history_baseline" not in parsed
            ):
                raise InvalidSessionDataError(
                    "백업에 설정 또는 이력 기준점이 없습니다."
                )
            unknown = set(parsed) - {
                "schema_version",
                "settings",
                "criteria",
                "items",
                "active_round",
                "history",
                "history_baseline",
            }
            if unknown:
                raise InvalidSessionDataError("백업에 알 수 없는 필드가 있습니다.")
            core = {
                key: parsed[key]
                for key in ("settings", "criteria", "items", "active_round")
                if key in parsed
            }
        else:
            core = _normalize_loaded_data(parsed)
        data = SessionDataModel.model_validate(core).model_dump(mode="python")
        data["active_round"] = None
        history = parsed.get("history", []) if version == 2 else []
        baseline = (
            SessionDataModel.model_validate(
                parsed.get("history_baseline", data)
            ).model_dump(mode="python")
            if version == 2
            else deepcopy(data)
        )
        if not isinstance(history, list) or len(history) > 100_000:
            raise InvalidSessionDataError("투표 이력 형식이 올바르지 않습니다.")
        seen: set[int] = set()
        allowed_ids = {item["id"] for item in data["items"]}
        if {item["id"] for item in baseline["items"]} != allowed_ids or [
            c["key"] for c in baseline["criteria"]
        ] != [c["key"] for c in data["criteria"]]:
            raise InvalidSessionDataError("이력 기준점과 현재 항목·기준이 다릅니다.")
        for event in history:
            if (
                not isinstance(event, dict)
                or type(event.get("id")) is not int
                or not 0 < event["id"] <= 2**63 - 1
                or event["id"] in seen
            ):
                raise InvalidSessionDataError("중복되거나 잘못된 투표 이력 ID입니다.")
            seen.add(event["id"])
            if (
                event.get("mode") not in ("2way", "3way")
                or not isinstance(event.get("created_at"), (int, float))
                or not 0 <= event["created_at"] <= 1e12
            ):
                raise InvalidSessionDataError(
                    "투표 이력의 방식·시각이 올바르지 않습니다."
                )
            if type(event.get("archived", False)) is not bool:
                raise InvalidSessionDataError("이력 보관 상태가 올바르지 않습니다.")
            if type(event.get("undone")) is not bool or not isinstance(
                event.get("algorithm_version"), str
            ):
                raise InvalidSessionDataError(
                    "투표 이력 메타데이터가 올바르지 않습니다."
                )
            request_type = (
                ThreeWayBattleVoteRequest
                if event.get("mode") == "3way"
                else BattleVoteRequest
            )
            payload = request_type.model_validate(event.get("payload")).model_dump(
                mode="python"
            )
            event["payload"] = payload
            event["archived"] = event.get("archived", False)
            ids = {payload["item1_id"], payload["item2_id"]}
            if payload.get("item3_id") is not None:
                ids.add(payload["item3_id"])
            if not event.get("archived", False) and not ids <= allowed_ids:
                raise InvalidSessionDataError("투표 이력에 없는 항목이 포함되었습니다.")
            for field in ("before_state", "after_state"):
                state = event.get(field)
                if not isinstance(state, dict) or set(state) != {"items", "criteria"}:
                    raise InvalidSessionDataError("투표 복구 상태가 없습니다.")
                validated = SessionDataModel.model_validate(
                    {"settings": event.get("settings", baseline["settings"]), **state}
                )
                event[field] = {
                    "items": [
                        item.model_dump(mode="python") for item in validated.items
                    ],
                    "criteria": [
                        criterion.model_dump(mode="python")
                        for criterion in validated.criteria
                    ],
                }
                event["settings"] = validated.settings.model_dump(mode="python")
                criterion_keys = {criterion.key for criterion in validated.criteria}
                if {
                    item.id for item in validated.items
                } != ids or criterion_keys != set(payload["votes"]):
                    raise InvalidSessionDataError(
                        "투표 복구 상태가 해당 대결과 다릅니다."
                    )
                if not event.get("archived", False) and criterion_keys != {
                    c["key"] for c in data["criteria"]
                }:
                    raise InvalidSessionDataError("현재 투표 이력의 기준이 다릅니다.")
        return {
            "data": data,
            "history": sorted(history, key=lambda e: e["id"]),
            "baseline": baseline,
            "legacy": version != 2,
        }

    @staticmethod
    def preview_import(raw: str) -> dict[str, Any]:
        parsed = DataStore.parse_import(raw)
        return {
            "items": len(parsed["data"]["items"]),
            "criteria": len(parsed["data"]["criteria"]),
            "history": len(parsed["history"]),
            "legacy": parsed["legacy"],
            "replaces_existing": True,
        }

    async def import_json(
        self, raw: str, expected_export_digest: str | None = None
    ) -> None:
        """JSON 문자열로부터 전체 데이터를 교체합니다.

        _load()와 동일한 관대 파싱을 사용하여 이전 버전 Export 파일도 수용합니다.
        """
        parsed = self.parse_import(raw)
        async with _get_lock(self._session_id):
            await self._load_from_db()
            if (
                expected_export_digest is not None
                and hashlib.sha256(self.export_json().encode()).hexdigest()
                != expected_export_digest
            ):
                raise InvalidSessionDataError(
                    "확인 후 데이터가 변경되었습니다. 다시 미리보기해주세요."
                )
            self._data = parsed["data"]
            self._history = parsed["history"]
            self._baseline = parsed["baseline"]
            self._invalidate_active_round()
            await self._save_to_db()

    async def issue_battle_round(
        self,
        item1_id: int,
        item2_id: int,
        item3_id: int | None = None,
    ) -> str:
        """배틀 라운드 토큰을 발급하고 DB에 영속화합니다.

        DB 저장으로 VM 재시작/Fly.io 자동 스케일다운 후에도 사용자가 이어서 투표 가능.
        3-way 모드에서는 item3_id를 함께 저장합니다.
        """
        async with _get_lock(self._session_id):
            await self._load_from_db()
            ids = [item1_id, item2_id] + ([item3_id] if item3_id is not None else [])
            if len(ids) != len(set(ids)) or any(
                self.get_item(iid) is None for iid in ids
            ):
                raise BattleItemNotFoundError(
                    "대결 항목이 변경되었습니다. 새로고침해주세요."
                )
            token = secrets.token_urlsafe(24)
            round_data: dict[str, Any] = {
                "token": token,
                "item1_id": item1_id,
                "item2_id": item2_id,
                "issued_at": time.time(),
            }
            if item3_id is not None:
                round_data["item3_id"] = item3_id
            self._data["active_round"] = round_data
            await self._save_to_db()
            return token

    @asynccontextmanager
    async def _vote_context(self) -> AsyncIterator[None]:
        if self._replaying:
            yield
        else:
            async with _get_lock(self._session_id):
                await self._load_from_db()
                yield

    def _vote_state(self, ids: set[int]) -> dict[str, Any]:
        return {
            "items": deepcopy([item for item in self.items if item["id"] in ids]),
            "criteria": deepcopy(self.criteria),
        }

    async def _finish_vote(
        self, payload: Any, mode: str, before: dict[str, Any]
    ) -> None:
        self._invalidate_active_round()
        if self._replaying:
            return
        ids = {item["id"] for item in before["items"]}
        event = {
            "id": max((event["id"] for event in self._history), default=0) + 1,
            "mode": mode,
            "payload": {
                "item1_id": payload.item1_id,
                "item2_id": payload.item2_id,
                "round_token": payload.round_token,
                "votes": deepcopy(payload.votes),
            },
            "settings": deepcopy(self.settings),
            "algorithm_version": ALGORITHM_VERSION,
            "before_state": before,
            "after_state": self._vote_state(ids),
            "undone": False,
            "archived": False,
            "created_at": time.time(),
        }
        if mode == "3way":
            event["payload"]["item3_id"] = payload.item3_id
        self._history.append(event)
        await self._save_to_db()

    async def clear_history(self) -> None:
        """현재 평점을 보존하고 이후 투표의 재계산 기준점으로 삼습니다."""
        async with _get_lock(self._session_id):
            await self._load_from_db()
            self._history = []
            self._reset_history()
            await self._save_to_db()

    async def undo_last_vote(
        self, expected_event_id: int | None = None
    ) -> dict[str, Any]:
        """현재 기준점 이후의 마지막 유효 투표를 되돌립니다."""
        async with _get_lock(self._session_id):
            await self._load_from_db()
            event = next(
                (
                    event
                    for event in reversed(self._history)
                    if not event["undone"] and not event.get("archived", False)
                ),
                None,
            )
            if event is None:
                raise InvalidBattleVoteError(
                    "되돌릴 투표가 없습니다. 항목·기준 변경 이후의 투표만 되돌릴 수 있습니다."
                )
            if expected_event_id is not None and event["id"] != expected_event_id:
                raise StaleBattleRoundError(
                    "이미 취소되었거나 투표 이력이 변경되었습니다."
                )
            replacements = {item["id"]: item for item in event["before_state"]["items"]}
            self._data["items"] = [
                {**deepcopy(replacements.get(item["id"], item)), "name": item["name"]}
                for item in self.items
            ]
            self._data["criteria"] = deepcopy(event["before_state"]["criteria"])
            event["undone"] = True
            self._invalidate_active_round()
            await self._save_to_db()
            return deepcopy(event)

    async def replay_history(
        self, settings_patch: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """저장된 기준점에서 원시 투표를 재계산합니다. patch 지정 시 전체 이력에 적용합니다."""
        async with _get_lock(self._session_id):
            await self._load_from_db()
            replay = DataStore(self._session_id)
            replay._replaying = True
            replay._data = deepcopy(self._baseline or self._snapshot())
            events = deepcopy(self._history)
            applied = 0
            current_settings = deepcopy(self.settings)
            current_names = {item["id"]: item["name"] for item in self.items}
            for event in events:
                if event["undone"] or event.get("archived", False):
                    continue
                replay._data["settings"] = deepcopy(
                    event.get("settings", replay.settings)
                )
                if settings_patch is not None:
                    replay._data["settings"] = {**current_settings, **settings_patch}
                replay._data = SessionDataModel.model_validate(replay._data).model_dump(
                    mode="python"
                )
                payload = event["payload"]
                replay._data["active_round"] = {
                    "token": payload["round_token"],
                    "item1_id": payload["item1_id"],
                    "item2_id": payload["item2_id"],
                    "item3_id": payload.get("item3_id"),
                    "issued_at": event["created_at"],
                }
                ids = {payload["item1_id"], payload["item2_id"]}
                if payload.get("item3_id") is not None:
                    ids.add(payload["item3_id"])
                event["before_state"] = replay._vote_state(ids)
                event["settings"] = deepcopy(replay.settings)
                if event["mode"] == "3way":
                    await replay.apply_three_way_vote(
                        ThreeWayBattleVoteRequest.model_validate(payload)
                    )
                else:
                    await replay.apply_battle_vote(
                        BattleVoteRequest.model_validate(payload)
                    )
                event["after_state"] = replay._vote_state(ids)
                event.setdefault("source_algorithm_version", event["algorithm_version"])
                event["algorithm_version"] = ALGORITHM_VERSION
                applied += 1
            replay._data["settings"] = {**current_settings, **(settings_patch or {})}
            for item in replay.items:
                item["name"] = current_names[item["id"]]
            self._data = SessionDataModel.model_validate(replay._data).model_dump(
                mode="python"
            )
            self._history = events
            self._invalidate_active_round()
            await self._save_to_db()
            return {
                "replayed": applied,
                "algorithm_version": ALGORITHM_VERSION,
                "settings_mode": "current"
                if settings_patch is not None
                else "recorded",
                "items": deepcopy(self.items),
            }

    async def apply_battle_vote(self, payload: BattleVoteRequest) -> dict[str, Any]:
        from services import (
            bt_update,
            hierarchical_shrinkage,
            display_rating,
            display_uncertainty,
        )

        async with self._vote_context():
            self._validate_active_round(payload)

            a1 = self.get_item(payload.item1_id)
            a2 = self.get_item(payload.item2_id)
            if not a1 or not a2:
                self._invalidate_active_round()
                raise BattleItemNotFoundError("대결 항목을 찾을 수 없습니다.")

            self._validate_vote_keys(payload.votes)

            before = self._vote_state({payload.item1_id, payload.item2_id})
            selected_keys = {
                key
                for key, vote in payload.votes.items()
                if vote not in ("skip", {"skip": "skip"})
            }
            criteria = self._data["criteria"]
            initial_sq = self._data["settings"]["initial_sigma"] ** 2
            results: list[dict[str, Any]] = []

            for criterion in criteria:
                key = criterion["key"]
                winner = payload.votes[key]
                if winner == "skip":
                    results.append(
                        {
                            "key": key,
                            "label": criterion["label"],
                            "color": criterion["color"],
                            "skipped": True,
                            "winner": "skip",
                        }
                    )
                    continue

                old_mu1 = a1["mu"].get(key, 0.0)
                old_sq1 = a1["sigma_sq"].get(key, initial_sq)
                old_mu2 = a2["mu"].get(key, 0.0)
                old_sq2 = a2["sigma_sq"].get(key, initial_sq)

                # winner는 BattleVoteRequest의 Literal["1", "2", "draw"]로 검증되지만,
                # 검증 우회 경로에서 silent 무승부로 흡수되지 않도록 fail-fast.
                outcome = _VOTE_OUTCOMES.get(winner)
                if outcome is None:
                    raise InvalidBattleVoteError(
                        f"기준 '{key}'에 알 수 없는 투표 값이 포함되어 있습니다: {winner!r}"
                    )

                new_mu1, new_sq1, new_mu2, new_sq2 = bt_update(
                    old_mu1,
                    old_sq1,
                    old_mu2,
                    old_sq2,
                    outcome,
                )

                a1["mu"][key] = new_mu1
                a1["sigma_sq"][key] = new_sq1
                a2["mu"][key] = new_mu2
                a2["sigma_sq"][key] = new_sq2

                # 기준별 배틀 통계 누적 (무승부 확률 실측 보정용)
                criterion["battles"] = criterion.get("battles", 0) + 1
                if winner == "draw":
                    criterion["draws"] = criterion.get("draws", 0) + 1

                # Per-item-per-criterion 카운트 증가
                if "criterion_matches" not in a1:
                    a1["criterion_matches"] = {}
                if "criterion_matches" not in a2:
                    a2["criterion_matches"] = {}
                a1["criterion_matches"][key] = a1["criterion_matches"].get(key, 0) + 1
                a2["criterion_matches"][key] = a2["criterion_matches"].get(key, 0) + 1

                old_disp1 = display_rating(self, old_mu1)
                new_disp1 = display_rating(self, new_mu1)
                old_disp2 = display_rating(self, old_mu2)
                new_disp2 = display_rating(self, new_mu2)

                results.append(
                    {
                        "key": key,
                        "label": criterion["label"],
                        "color": criterion["color"],
                        "winner": winner,
                        "old_r1": round(old_disp1, 1),
                        "new_r1": round(new_disp1, 1),
                        "diff_r1": round(new_disp1 - old_disp1, 1),
                        "old_r2": round(old_disp2, 1),
                        "new_r2": round(new_disp2, 1),
                        "diff_r2": round(new_disp2 - old_disp2, 1),
                        "sigma1": round(display_uncertainty(self, new_sq1), 1),
                        "sigma2": round(display_uncertainty(self, new_sq2), 1),
                    }
                )

            # 모든 기준 업데이트 후 계층적 축소
            if self._data["settings"]["hierarchical_strength"] > 0:
                hierarchical_shrinkage(self, a1, keys=selected_keys)
                hierarchical_shrinkage(self, a2, keys=selected_keys)

            if selected_keys:
                a1["matches_played"] += 1
                a2["matches_played"] += 1
            for result in results:
                if result.get("skipped"):
                    continue
                for index, item in enumerate((a1, a2), 1):
                    final = display_rating(self, item["mu"][result["key"]])
                    result[f"new_r{index}"] = round(final, 1)
                    original = next(i for i in before["items"] if i["id"] == item["id"])
                    result[f"diff_r{index}"] = round(
                        final - display_rating(self, original["mu"][result["key"]]), 1
                    )
            await self._finish_vote(payload, "2way", before)

            return {
                "a1_id": a1["id"],
                "a2_id": a2["id"],
                "a1_name": a1["name"],
                "a2_name": a2["name"],
                "results": results,
                "total_items": len(self._data["items"]),
                "next_url": payload.redirect_to or "/battle",
            }

    async def apply_three_way_vote(
        self, payload: ThreeWayBattleVoteRequest
    ) -> dict[str, Any]:
        """3-way 배틀 투표를 처리합니다.

        기준별 best/worst 선택을 3개 쌍대비교로 분해하여 BT 업데이트합니다.
        동시 업데이트: 원본 값에서 모든 그래디언트를 계산 후 일괄 적용하여
        순차 적용 시 발생하는 업데이트 순서 편향을 제거합니다.
        """
        from services import (
            hierarchical_shrinkage,
            display_rating,
            display_uncertainty,
        )

        async with self._vote_context():
            self._validate_active_round(payload, item3_id=payload.item3_id)

            item_ids = [payload.item1_id, payload.item2_id, payload.item3_id]
            items_3 = [self.get_item(iid) for iid in item_ids]
            if not all(items_3):
                self._invalidate_active_round()
                raise BattleItemNotFoundError("대결 항목을 찾을 수 없습니다.")

            self._validate_vote_keys(payload.votes)

            before = self._vote_state(set(item_ids))
            selected_keys = {
                key
                for key, vote in payload.votes.items()
                if vote not in ("skip", {"skip": "skip"})
            }
            criteria = self._data["criteria"]
            initial_sq = self._data["settings"]["initial_sigma"] ** 2
            results: list[dict[str, Any]] = []
            id_str = {iid: str(iid) for iid in item_ids}

            for criterion in criteria:
                key = criterion["key"]
                vote = payload.votes[key]
                if vote in ("skip", {"skip": "skip"}):
                    results.append(
                        {
                            "key": key,
                            "label": criterion["label"],
                            "color": criterion["color"],
                            "skipped": True,
                        }
                    )
                    continue

                # best/worst/tied ID 추출
                # id_key는 클라이언트에서 문자열로 전달되므로 정수 변환·중복·소속 검증을 모두 InvalidBattleVoteError로 통일
                best_id: int | None = None
                worst_id: int | None = None
                tied_ids: list[int] = []
                best_count = 0
                worst_count = 0
                seen_item_ids: set[int] = set()
                for id_key, role in vote.items():
                    try:
                        item_id = int(id_key)
                    except (TypeError, ValueError):
                        raise InvalidBattleVoteError(
                            f"기준 '{key}'에 숫자가 아닌 항목 ID가 포함되어 있습니다: {id_key!r}"
                        )
                    if item_id in seen_item_ids:
                        raise InvalidBattleVoteError(
                            f"기준 '{key}'에서 항목 ID {item_id}가 중복 등장했습니다."
                        )
                    seen_item_ids.add(item_id)
                    if role == "best":
                        best_id = item_id
                        best_count += 1
                    elif role == "worst":
                        worst_id = item_id
                        worst_count += 1
                    elif role == "tied":
                        tied_ids.append(item_id)
                if best_count > 1 or worst_count > 1:
                    raise InvalidBattleVoteError(
                        f"기준 '{key}'에서 best 또는 worst가 중복되었습니다."
                    )
                # 모드 공통 검증: 역할이 부여된 ID는 모두 대결 3개 항목이어야 함.
                # seen_item_ids 중복 검사와 결합되어 이후 모드 분해에서
                # 자기 자신 비교 쌍이 발생할 수 없음을 보장한다.
                if not seen_item_ids.issubset(item_ids):
                    raise InvalidBattleVoteError(
                        f"기준 '{key}'의 투표 ID가 대결 항목에 없습니다."
                    )

                old_ratings: dict[str, float] = {}
                for item in items_3:
                    old_ratings[id_str[item["id"]]] = display_rating(
                        self, item["mu"].get(key, 0.0)
                    )

                # 결과용 ID (모드에 따라 None 가능)
                best_id_result: int | None = best_id
                worst_id_result: int | None = worst_id
                middle_id_result: int | None = None

                if best_id is not None and worst_id is None and len(tied_ids) == 2:
                    # Mode A: best only — best 1명 + tied 2명
                    tied_a, tied_b = tied_ids
                    pairs = [
                        (best_id, tied_a, 1.0),
                        (best_id, tied_b, 1.0),
                        (tied_a, tied_b, 0.5),
                    ]

                elif (
                    best_id is not None and worst_id is not None and len(tied_ids) == 0
                ):
                    # Mode B: 순위 완전 결정 — best > middle > worst
                    # (best == worst는 vote가 ID 키 dict + seen 중복 검사로 원천 불가)
                    middle_id_result = [
                        iid for iid in item_ids if iid != best_id and iid != worst_id
                    ][0]
                    pairs = [
                        (best_id, middle_id_result, 1.0),
                        (best_id, worst_id, 1.0),
                        (middle_id_result, worst_id, 1.0),
                    ]

                elif best_id is None and worst_id is not None and len(tied_ids) == 2:
                    # Mode C: worst only — worst 1명 + tied 2명
                    tied_a, tied_b = tied_ids
                    pairs = [
                        (tied_a, worst_id, 1.0),
                        (tied_b, worst_id, 1.0),
                        (tied_a, tied_b, 0.5),
                    ]

                elif best_id is None and worst_id is None and len(tied_ids) == 3:
                    # Mode D: 모두 무승부 — 3개 항목 모두 tied
                    a, b, c = tied_ids
                    pairs = [
                        (a, b, 0.5),
                        (a, c, 0.5),
                        (b, c, 0.5),
                    ]

                else:
                    raise InvalidBattleVoteError(
                        f"기준 '{key}'의 투표 조합이 올바르지 않습니다."
                    )

                item_by_id = {item["id"]: item for item in items_3}

                ratings = {
                    iid: (
                        item_by_id[iid]["mu"].get(key, 0.0),
                        item_by_id[iid]["sigma_sq"].get(key, initial_sq),
                    )
                    for iid in item_ids
                }
                for iid, (mu, sigma_sq) in update_ratings(ratings, pairs).items():
                    item_by_id[iid]["mu"][key] = mu
                    item_by_id[iid]["sigma_sq"][key] = sigma_sq

                # 기준별 배틀 통계 — 3 쌍 = 3 배틀
                criterion["battles"] = criterion.get("battles", 0) + 3
                draw_count = sum(1 for _, _, outcome in pairs if outcome == 0.5)
                if draw_count > 0:
                    criterion["draws"] = criterion.get("draws", 0) + draw_count

                # Per-item-per-criterion 카운트 — 각 항목은 2 쌍에 참여
                for item in items_3:
                    if "criterion_matches" not in item:
                        item["criterion_matches"] = {}
                    item["criterion_matches"][key] = (
                        item["criterion_matches"].get(key, 0) + 2
                    )

                # 결과 수집
                new_ratings: dict[str, float] = {}
                diffs: dict[str, float] = {}
                sigmas: dict[str, float] = {}
                for item in items_3:
                    k = id_str[item["id"]]
                    new_r = display_rating(self, item["mu"].get(key, 0.0))
                    new_ratings[k] = round(new_r, 1)
                    diffs[k] = round(new_r - old_ratings[k], 1)
                    sigmas[k] = round(
                        display_uncertainty(
                            self, item["sigma_sq"].get(key, initial_sq)
                        ),
                        1,
                    )

                results.append(
                    {
                        "key": key,
                        "label": criterion["label"],
                        "color": criterion["color"],
                        "best_id": best_id_result,
                        "worst_id": worst_id_result,
                        "middle_id": middle_id_result,
                        "ratings": new_ratings,
                        "diffs": diffs,
                        "sigmas": sigmas,
                    }
                )

            # 계층적 축소
            if self._data["settings"]["hierarchical_strength"] > 0:
                for item in items_3:
                    hierarchical_shrinkage(self, item, keys=selected_keys)

            if selected_keys:
                for item in items_3:
                    item["matches_played"] += 1
            for result in results:
                if result.get("skipped"):
                    continue
                for item in items_3:
                    k = str(item["id"])
                    final = display_rating(self, item["mu"][result["key"]])
                    original = next(i for i in before["items"] if i["id"] == item["id"])
                    result["ratings"][k] = round(final, 1)
                    result["diffs"][k] = round(
                        final - display_rating(self, original["mu"][result["key"]]), 1
                    )
            await self._finish_vote(payload, "3way", before)

            return {
                "a1_id": items_3[0]["id"],
                "a2_id": items_3[1]["id"],
                "a3_id": items_3[2]["id"],
                "a1_name": items_3[0]["name"],
                "a2_name": items_3[1]["name"],
                "a3_name": items_3[2]["name"],
                "results": results,
                "total_items": len(self._data["items"]),
                "next_url": payload.redirect_to or "/battle",
            }

    async def delete_session(self) -> None:
        """세션 데이터를 DB에서 삭제합니다."""
        self._invalidate_active_round()
        await delete_session(self._session_id)


# --- 세션 관리자 ---


async def get_store(session_id: str) -> DataStore:
    """세션 ID에 해당하는 DataStore를 반환합니다."""
    store = await DataStore.create(session_id)
    # 읽기만 지속하는 세션의 갱신은 5분에 한 번으로 제한합니다.
    async with transaction() as db:
        now = time.time()
        await db.execute(
            "UPDATE sessions SET last_accessed = ? WHERE id = ? AND last_accessed < ?",
            (now, session_id, now - 300),
        )
    return store


async def session_exists(session_id: str) -> bool:
    """확정된 세션의 존재 여부를 확인합니다."""
    async with database.db_write_lock:
        async with get_db().execute(
            "SELECT 1 FROM sessions WHERE id = ?", (session_id,)
        ) as cursor:
            return await cursor.fetchone() is not None


async def delete_session(session_id: str) -> None:
    """진행 중인 세션 명령 뒤에 삭제하고 새 저장은 revision으로 차단합니다."""
    async with _get_lock(session_id):
        async with transaction() as db:
            await db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))


async def cleanup_expired_sessions() -> int:
    """보관함에 등록되지 않은 만료 세션만 삭제합니다."""
    cutoff = time.time() - SESSION_TTL_SECONDS
    db = get_db()
    async with database.db_write_lock:
        async with db.execute(
            "SELECT id FROM sessions WHERE last_accessed < ? AND id NOT IN (SELECT session_id FROM boards)",
            (cutoff,),
        ) as cursor:
            expired = [row["id"] for row in await cursor.fetchall()]
    removed = 0
    for session_id in expired:
        async with _get_lock(session_id):
            async with transaction() as db:
                cursor = await db.execute(
                    "DELETE FROM sessions WHERE id = ? AND last_accessed < ? AND id NOT IN (SELECT session_id FROM boards)",
                    (session_id, cutoff),
                )
                removed += cursor.rowcount
    if removed:
        logger.info("cleanup_expired_sessions — removed %d sessions", removed)
    return removed
