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
from ranker import database
from copy import deepcopy
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from weakref import WeakValueDictionary
from collections import Counter
from itertools import groupby
from typing import Any

from pydantic import ValidationError
from ranker.rating_engine import (
    ALGORITHM_VERSION,
    Posterior,
    ballot_ranking,
    fit_rankings,
)
from ranker.services import display_rating, display_uncertainty

from ranker.database import (
    get_db,
    save_session_data,
    transaction,
    StaleSessionError,
)
from ranker.schemas import (
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
# 세션 락과 DB 커넥션은 단일 uvicorn 워커에서 사용합니다.
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
        self._posterior_cache: dict[str, Posterior] = {}

    @classmethod
    async def create(cls, session_id: str) -> "DataStore":
        """비동기 팩토리 — DB에서 데이터를 로드한 DataStore를 반환합니다."""
        instance = cls(session_id)
        await instance._load_from_db()
        return instance

    async def _load_from_db(self) -> None:
        """같은 커넥션의 쓰기 중간 상태를 읽지 않습니다."""
        self._posterior_cache.clear()
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
            "SELECT state FROM ranking_models WHERE session_id = ?", (self._session_id,)
        ) as cursor:
            model = await cursor.fetchone()
            if model is None:
                raise InvalidSessionDataError("보존된 순위 관측을 찾을 수 없습니다.")
            self._data.update(json.loads(model["state"]))

    def _snapshot(self) -> dict[str, Any]:
        data = deepcopy(self._data)
        data["active_round"] = None
        return data

    def _reset_history(self) -> None:
        """구조 변경 전 상세 이력은 취소만 제한합니다. 학습 관측은 유지합니다."""
        for event in self._history:
            event["archived"] = True

    @property
    def active_round(self) -> dict[str, Any] | None:
        return deepcopy(self._data.get("active_round"))

    @property
    def history(self) -> list[dict[str, Any]]:
        return deepcopy(self._history)

    async def _save_to_db(self) -> None:
        """모든 저장 경로에서 읽기 가능한 상태를 검증한 뒤 단일 트랜잭션으로 기록합니다."""
        self._data = SessionDataModel.model_validate(self._data).model_dump(
            mode="python"
        )
        if len(self.export_json().encode("utf-8")) > MAX_BACKUP_BYTES:
            raise SessionSaveError(
                "랭킹의 백업 한도(64MB)에 도달했습니다. 백업 후 투표 이력을 정리해주세요."
            )
        try:
            self._revision = await save_session_data(
                self._session_id,
                self._data,
                created_at=self._created_at,
                last_accessed=time.time(),
                expected_revision=self._revision,
                history=self._history,
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
        async with self._vote_context():
            previous_sigma = self.settings["initial_sigma"]
            self._data["settings"].update(patch)
            self._data = SessionDataModel.model_validate(self._data).model_dump(
                mode="python"
            )
            if self.settings["initial_sigma"] != previous_sigma:
                await self._refit({c["key"] for c in self.criteria})
            self._invalidate_active_round()
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
                    c.update(old_stats[c["key"]])
                else:
                    c.update(battles=0, draws=0)

            self._data["criteria"] = criteria
            for field in ("observations", "exposures", "posteriors"):
                for key in removed:
                    self._data[field].pop(key, None)
            self._posterior_cache.clear()
            self._invalidate_active_round()
            if added or removed:
                self._reset_history()
            await self._save_to_db()

    # --- Items ---

    @property
    def items(self) -> list[dict[str, Any]]:
        return self._data["items"]

    def _next_id(self) -> int:
        ids = [item["id"] for item in self.items]
        ids.extend(
            i
            for rows in self._data["observations"].values()
            for row in rows
            for group in row["groups"]
            for i in group
        )
        ids.extend(
            event["payload"][key]
            for event in self._history
            for key in ("item1_id", "item2_id", "item3_id")
            if event["payload"].get(key) is not None
        )
        return max(ids, default=0) + 1

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
            await self._save_to_db()

    # --- Import / Export ---

    def export_json(self) -> str:
        core = self._snapshot()
        core.pop("posteriors", None)  # Derived cache is rebuilt from counted ballots.
        envelope = {"schema_version": 3, **core, "history": self.history}
        return json.dumps(
            envelope,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def parse_import(raw: str) -> dict[str, Any]:
        """현재 백업 형식만 검증합니다."""
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
        if (
            type(parsed.get("schema_version")) is not int
            or parsed["schema_version"] != 3
        ):
            raise InvalidSessionDataError(
                "백업 형식 3만 지원합니다. 이전 점수 요약은 새 모형에 이어 붙이지 않습니다."
            )
        required = {
            "schema_version",
            "settings",
            "criteria",
            "items",
            "active_round",
            "history",
            "observations",
            "exposures",
        }
        if set(parsed) != required:
            raise InvalidSessionDataError(
                "백업에 누락되거나 알 수 없는 필드가 있습니다."
            )
        core = {
            key: parsed[key]
            for key in (
                "settings",
                "criteria",
                "items",
                "active_round",
                "observations",
                "exposures",
            )
        }
        data = SessionDataModel.model_validate(core).model_dump(mode="python")
        data["active_round"] = None
        history = parsed["history"]
        if not isinstance(history, list) or len(history) > 100_000:
            raise InvalidSessionDataError("투표 이력 형식이 올바르지 않습니다.")
        seen: set[int] = set()
        allowed_ids = {item["id"] for item in data["items"]}
        for event in history:
            event_fields = {
                "id",
                "mode",
                "payload",
                "settings",
                "algorithm_version",
                "before_state",
                "after_state",
                "undone",
                "archived",
                "created_at",
            }
            if (
                not isinstance(event, dict)
                or set(event) != event_fields
                or type(event.get("id")) is not int
                or not 0 < event["id"] <= 2**63 - 1
                or event["id"] in seen
            ):
                raise InvalidSessionDataError("중복되거나 잘못된 투표 이력 ID입니다.")
            seen.add(event["id"])
            if (
                event.get("mode") not in ("2way", "3way")
                or not isinstance(event.get("created_at"), (int, float))
                or not 0 <= event["created_at"] <= 253_402_300_799
            ):
                raise InvalidSessionDataError(
                    "투표 이력의 방식·시각이 올바르지 않습니다."
                )
            if type(event.get("archived", False)) is not bool:
                raise InvalidSessionDataError("이력 보관 상태가 올바르지 않습니다.")
            if (
                type(event.get("undone")) is not bool
                or event.get("algorithm_version") != ALGORITHM_VERSION
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
            for vote in payload["votes"].values():
                try:
                    ballot_ranking(
                        [
                            payload[key]
                            for key in ("item1_id", "item2_id", "item3_id")
                            if payload.get(key) is not None
                        ],
                        vote,
                    )
                except ValueError as exc:
                    raise InvalidSessionDataError(
                        "투표 이력의 순위가 올바르지 않습니다."
                    ) from exc
            for field in ("before_state", "after_state"):
                state = event.get(field)
                if not isinstance(state, dict) or set(state) != {"items", "criteria"}:
                    raise InvalidSessionDataError("투표 복구 상태가 없습니다.")
                validated = SessionDataModel.model_validate(
                    {"settings": event["settings"], **state}
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
        # Undo must never remove observations that were absent from the backup.
        available = {
            key: Counter({row["groups"]: row["count"] for row in rows})
            for key, rows in data["observations"].items()
        }
        active_counts: Counter = Counter()
        active_exposures: Counter = Counter()
        active_item_ballots: Counter = Counter()
        for event in history:
            if event["undone"] or event["archived"]:
                continue
            payload = event["payload"]
            ids = [
                payload[k]
                for k in ("item1_id", "item2_id", "item3_id")
                if payload.get(k) is not None
            ]
            answered = False
            for key, vote in payload["votes"].items():
                active_exposures[key] += 1
                ranking = ballot_ranking(ids, vote)
                if ranking is not None:
                    active_counts[key, ranking] += 1
                    answered = True
            if answered:
                active_item_ballots.update(ids)
        if (
            any(
                count > available.get(key, {}).get(ranking, 0)
                for (key, ranking), count in active_counts.items()
            )
            or any(
                count > data["exposures"].get(key, 0)
                for key, count in active_exposures.items()
            )
            or any(
                active_item_ballots[item["id"]] > item["matches_played"]
                for item in data["items"]
            )
        ):
            raise InvalidSessionDataError(
                "취소 가능한 투표 이력과 보존 관측 수가 다릅니다."
            )
        return {
            "data": data,
            "history": sorted(history, key=lambda e: e["id"]),
        }

    @staticmethod
    def preview_import(raw: str) -> dict[str, Any]:
        parsed = DataStore.parse_import(raw)
        return {
            "items": len(parsed["data"]["items"]),
            "criteria": len(parsed["data"]["criteria"]),
            "history": len(parsed["history"]),
        }

    async def import_json(
        self, raw: str, expected_export_digest: str | None = None
    ) -> None:
        """검증한 현재 형식 백업으로 데이터를 교체합니다."""
        parsed = self.parse_import(raw)
        async with self._vote_context():
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
            await self._refit({c["key"] for c in self.criteria})
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
        async with _get_lock(self._session_id):
            await self._load_from_db()
            try:
                yield
            except BaseException:
                # A failed optimizer or save leaves both this object and SQLite usable.
                await asyncio.shield(self._load_from_db())
                raise

    def _vote_state(self, ids: set[int]) -> dict[str, Any]:
        return {
            "items": deepcopy([item for item in self.items if item["id"] in ids]),
            "criteria": deepcopy(self.criteria),
        }

    def posterior(self, key: str) -> Posterior:
        if key not in self._posterior_cache:
            state = self._data["posteriors"].get(key)
            self._posterior_cache[key] = (
                Posterior.from_dict(state)
                if state
                else fit_rankings([], initial_sigma=self.settings["initial_sigma"])
            )
        return self._posterior_cache[key]

    def recent_votes(self, limit: int = 5) -> list[dict[str, Any]]:
        """Only copy small payloads, not the entire audit trail and score snapshots."""
        result = []
        for event in reversed(self._history):
            if not event["undone"]:
                result.append(deepcopy(event["payload"]))
                if len(result) == limit:
                    break
        return result

    def response_rate(self, key: str) -> float:
        answered = sum(row["count"] for row in self._data["observations"].get(key, []))
        return (answered + 1) / (self._data["exposures"].get(key, 0) + 2)

    def _change_observation(self, key: str, ranking, amount: int) -> None:
        rows = self._data["observations"].setdefault(key, [])
        for row in rows:
            if tuple(tuple(group) for group in row["groups"]) == ranking:
                row["count"] += amount
                if row["count"] == 0:
                    rows.remove(row)
                elif row["count"] < 0:
                    raise InvalidSessionDataError("취소할 관측이 없습니다.")
                break
        else:
            if amount < 0:
                raise InvalidSessionDataError("취소할 관측이 없습니다.")
            rows.append({"groups": ranking, "count": amount})
        rows.sort(key=lambda row: tuple(tuple(g) for g in row["groups"]))

    async def _refit(self, keys: set[str]) -> None:
        """Fit all retained counted ballots from the configured prior, in a worker."""
        sigma = self.settings["initial_sigma"]
        observations = {
            key: deepcopy(self._data["observations"].get(key, [])) for key in keys
        }

        def calculate():
            fitted = {}
            for key in sorted(keys):
                rows = observations[key]
                posterior = fit_rankings(
                    [row["groups"] for row in rows],
                    counts=[row["count"] for row in rows],
                    initial_sigma=sigma,
                )
                fitted[key] = posterior, posterior.marginal_variances()
            return fitted

        fitted = await asyncio.to_thread(calculate)
        for key, (posterior, variances) in fitted.items():
            self._data["posteriors"][key] = posterior.to_dict()
            self._posterior_cache[key] = posterior
            counts: dict[int, int] = {}
            draws = 0
            rows = observations[key]
            for row in rows:
                for group in row["groups"]:
                    for iid in group:
                        counts[iid] = counts.get(iid, 0) + row["count"]
                if any(len(group) > 1 for group in row["groups"]):
                    draws += row["count"]
            criterion = next(c for c in self.criteria if c["key"] == key)
            criterion["battles"] = sum(row["count"] for row in rows)
            criterion["draws"] = draws
            for item in self.items:
                iid = item["id"]
                item["mu"][key] = posterior.mean(iid)
                item["sigma_sq"][key] = variances.get(iid, sigma**2)
                item["criterion_matches"][key] = counts.get(iid, 0)

    async def clear_history(self) -> None:
        """Drop detailed audit snapshots; counted raw rankings remain refittable."""
        async with self._vote_context():
            self._history = []
            await self._save_to_db()

    async def undo_last_vote(
        self, expected_event_id: int | None = None
    ) -> dict[str, Any]:
        async with self._vote_context():
            event = next(
                (
                    e
                    for e in reversed(self._history)
                    if not e["undone"] and not e["archived"]
                ),
                None,
            )
            if event is None:
                raise InvalidBattleVoteError("되돌릴 투표가 없습니다.")
            if expected_event_id is not None and event["id"] != expected_event_id:
                raise StaleBattleRoundError(
                    "이미 취소되었거나 투표 이력이 변경되었습니다."
                )
            payload = event["payload"]
            ids = [
                payload[key]
                for key in ("item1_id", "item2_id", "item3_id")
                if payload.get(key) is not None
            ]
            keys = set()
            for key, vote in payload["votes"].items():
                self._data["exposures"][key] -= 1
                ranking = ballot_ranking(ids, vote)
                if ranking is not None:
                    self._change_observation(key, ranking, -1)
                    keys.add(key)
            if keys:
                for iid in ids:
                    self.get_item(iid)["matches_played"] -= 1
            await self._refit(keys)
            event["undone"] = True
            self._invalidate_active_round()
            await self._save_to_db()
            return deepcopy(event)

    async def recalculate_ratings(
        self, settings_patch: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Full static refit of counted observations, including archived audit entries."""
        async with self._vote_context():
            self._data["settings"].update(settings_patch or {})
            self._data = SessionDataModel.model_validate(self._data).model_dump(
                mode="python"
            )
            await self._refit({c["key"] for c in self.criteria})
            self._invalidate_active_round()
            await self._save_to_db()
            return {
                "responses": sum(c["battles"] for c in self.criteria),
                "algorithm_version": ALGORITHM_VERSION,
                "items": deepcopy(self.items),
            }

    async def apply_battle_vote(self, payload: BattleVoteRequest) -> dict[str, Any]:
        return await self._apply_vote(payload, [payload.item1_id, payload.item2_id])

    async def apply_three_way_vote(
        self, payload: ThreeWayBattleVoteRequest
    ) -> dict[str, Any]:
        return await self._apply_vote(
            payload, [payload.item1_id, payload.item2_id, payload.item3_id]
        )

    async def _apply_vote(self, payload: Any, ids: list[int]) -> dict[str, Any]:
        async with self._vote_context():
            self._validate_active_round(payload, ids[2] if len(ids) == 3 else None)
            if any(self.get_item(iid) is None for iid in ids):
                raise BattleItemNotFoundError("대결 항목을 찾을 수 없습니다.")
            self._validate_vote_keys(payload.votes)
            try:
                rankings = {
                    key: ballot_ranking(ids, vote)
                    for key, vote in payload.votes.items()
                }
            except ValueError as exc:
                raise InvalidBattleVoteError(str(exc)) from exc
            before = self._vote_state(set(ids))
            old = {item["id"]: item for item in before["items"]}
            keys = {key for key, ranking in rankings.items() if ranking is not None}
            for key, ranking in rankings.items():
                self._data["exposures"][key] = self._data["exposures"].get(key, 0) + 1
                if ranking is not None:
                    self._change_observation(key, ranking, 1)
            await self._refit(keys)
            if keys:
                for iid in ids:
                    self.get_item(iid)["matches_played"] += 1
            results = []
            for criterion in self.criteria:
                key = criterion["key"]
                result = {
                    field: criterion[field] for field in ("key", "label", "color")
                }
                ranking = rankings[key]
                if ranking is None:
                    result["skipped"] = True
                    results.append(result)
                    continue
                if len(ids) == 2:
                    result["winner"] = payload.votes[key]
                    for index, iid in enumerate(ids, 1):
                        item = self.get_item(iid)
                        old_r = display_rating(self, old[iid]["mu"][key])
                        new_r = display_rating(self, item["mu"][key])
                        result.update(
                            {
                                f"old_r{index}": round(old_r, 1),
                                f"new_r{index}": round(new_r, 1),
                                f"diff_r{index}": round(new_r - old_r, 1),
                                f"sigma{index}": round(
                                    display_uncertainty(self, item["sigma_sq"][key]), 1
                                ),
                            }
                        )
                else:
                    result.update(
                        {
                            "best_id": ranking[0][0] if len(ranking[0]) == 1 else None,
                            "worst_id": ranking[-1][0]
                            if len(ranking[-1]) == 1
                            else None,
                            "middle_id": ranking[1][0] if len(ranking) == 3 else None,
                            "ratings": {},
                            "diffs": {},
                            "sigmas": {},
                        }
                    )
                    for iid in ids:
                        item = self.get_item(iid)
                        old_r = display_rating(self, old[iid]["mu"][key])
                        new_r = display_rating(self, item["mu"][key])
                        result["ratings"][str(iid)] = round(new_r, 1)
                        result["diffs"][str(iid)] = round(new_r - old_r, 1)
                        result["sigmas"][str(iid)] = round(
                            display_uncertainty(self, item["sigma_sq"][key]), 1
                        )
                results.append(result)
            event = {
                "id": max((e["id"] for e in self._history), default=0) + 1,
                "mode": "3way" if len(ids) == 3 else "2way",
                "payload": {
                    "item1_id": ids[0],
                    "item2_id": ids[1],
                    "round_token": payload.round_token,
                    "votes": deepcopy(payload.votes),
                },
                "settings": deepcopy(self.settings),
                "algorithm_version": ALGORITHM_VERSION,
                "before_state": before,
                "after_state": self._vote_state(set(ids)),
                "undone": False,
                "archived": False,
                "created_at": time.time(),
            }
            if len(ids) == 3:
                event["payload"]["item3_id"] = ids[2]
            self._history.append(event)
            self._invalidate_active_round()
            await self._save_to_db()
            response = {
                "results": results,
                "total_items": len(self.items),
                "next_url": payload.redirect_to or "/battle",
            }
            for index, iid in enumerate(ids, 1):
                response[f"a{index}_id"] = iid
                response[f"a{index}_name"] = self.get_item(iid)["name"]
            return response

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
