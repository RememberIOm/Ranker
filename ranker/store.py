# store.py
# 세션 기반 SQLite 데이터 저장소 — 각 사용자가 독립된 데이터를 운용합니다.
# 투표 기록은 요청마다 불러오지 않고, 내역·취소·백업에서만 읽습니다.

import asyncio
import json
import logging
import os
import secrets
import sqlite3
import time
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from copy import deepcopy
from itertools import groupby
from typing import Any
from weakref import WeakValueDictionary

from pydantic import ValidationError

from ranker import database
from ranker.database import (
    BackupLimitError,
    EventChanges,
    SessionSaveError,
    StaleSessionError,
    transaction,
)
from ranker.rating_engine import Posterior, ballot_ranking, fit_rankings
from ranker.schemas import (
    BackupModel,
    BattleVoteRequest,
    SessionDataModel,
    ThreeWayBattleVoteRequest,
    VotePayloadModel,
)
from ranker.services import display_rating, display_uncertainty

__all__ = [
    "BackupLimitError",
    "BattleItemNotFoundError",
    "DataStore",
    "InvalidBattleVoteError",
    "InvalidSessionDataError",
    "SessionSaveError",
    "StaleBattleRoundError",
    "StaleImportError",
    "StaleSessionError",
    "cleanup_expired_sessions",
    "create_store",
    "delete_session",
    "open_store",
]

logger = logging.getLogger("ranker.store")

# 내 랭킹 목록에 없는 세션의 만료 시간 (7일)
SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", str(7 * 24 * 60 * 60)))
PENDING_IMPORT_TTL_SECONDS = 3600
BACKUP_SCHEMA_VERSION = 4

# asyncio.Lock은 실행 중인 이벤트 루프 안에서 생성해야 하므로 lazy init합니다.
# 세션 락과 DB 커넥션은 단일 uvicorn 워커에서 사용합니다.
_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()


class InvalidBattleVoteError(ValueError):
    """투표 내용이 현재 랭킹과 맞지 않습니다."""


class StaleBattleRoundError(RuntimeError):
    """이미 처리되었거나 만료된 대결, 또는 이미 바뀐 투표 기록입니다."""


class BattleItemNotFoundError(LookupError):
    """대결 중인 항목을 찾을 수 없습니다."""


class InvalidSessionDataError(ValueError):
    """저장된 데이터나 백업을 현재 형식으로 읽을 수 없습니다."""


class StaleImportError(RuntimeError):
    """미리보기 뒤에 랭킹이 바뀌었거나 미리보기가 만료되었습니다."""


def _get_lock(session_id: str) -> asyncio.Lock:
    lock = _locks.get(session_id)
    if lock is None:
        lock = asyncio.Lock()
        _locks[session_id] = lock
    return lock


def _vote_ids(payload: dict[str, Any]) -> list[int]:
    return [
        payload[key]
        for key in ("item1_id", "item2_id", "item3_id")
        if payload.get(key) is not None
    ]


def _dumps(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


class DataStore:
    """한 랭킹의 현재 상태. open_store() 또는 create_store()로 얻습니다."""

    def __init__(self, session_id: str) -> None:
        self._session_id = session_id
        self._data: dict[str, Any] = {}
        self._created_at = 0.0
        self._revision: int | None = None
        self._recent: list[dict[str, Any]] = []
        self._events = EventChanges()
        self._posterior_cache: dict[str, Posterior] = {}

    @property
    def session_id(self) -> str:
        return self._session_id

    # --- Loading and saving ---

    async def _load(self, *, touch: bool = False) -> bool:
        """같은 커넥션의 쓰기 중간 상태를 읽지 않도록 트랜잭션 안에서 읽습니다."""
        self._posterior_cache.clear()
        self._events = EventChanges()
        async with transaction() as db:
            found = await self._read_snapshot(db)
            if found and touch:
                # 읽기만 하는 세션의 접근 시각 갱신은 5분에 한 번으로 제한합니다.
                now = time.time()
                await db.execute(
                    "UPDATE sessions SET last_accessed = ? WHERE id = ? AND last_accessed < ?",
                    (now, self._session_id, now - 300),
                )
        if not found:
            return False
        try:
            self._data = SessionDataModel.model_validate(self._data).model_dump(
                mode="python"
            )
        except ValidationError as exc:
            raise InvalidSessionDataError(
                "저장된 랭킹을 읽을 수 없습니다. 백업 파일로 복구해주세요."
            ) from exc
        return True

    async def _reload(self) -> None:
        if not await self._load():
            raise StaleSessionError(
                "랭킹이 삭제되었습니다. 내 랭킹에서 다시 열어주세요."
            )

    async def _read_snapshot(self, db) -> bool:
        sid = (self._session_id,)
        async with db.execute(
            "SELECT settings, created_at, revision, next_item_id FROM sessions WHERE id = ?",
            sid,
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return False
        self._revision = row["revision"]
        self._created_at = row["created_at"]
        data: dict[str, Any] = {
            "settings": json.loads(row["settings"]),
            "next_item_id": row["next_item_id"],
            "active_round": None,
        }
        async with db.execute(
            "SELECT key, label, color, weight, battles, draws "
            "FROM criteria WHERE session_id = ? ORDER BY sort_order",
            sid,
        ) as cursor:
            data["criteria"] = [dict(r) for r in await cursor.fetchall()]
        async with db.execute(
            "SELECT i.id, i.name, i.matches_played, "
            "       r.criterion_key, r.mu, r.sigma_sq, r.criterion_matches "
            "FROM items i "
            "LEFT JOIN item_ratings r ON i.session_id = r.session_id AND i.id = r.item_id "
            "WHERE i.session_id = ? ORDER BY i.id, r.criterion_key",
            sid,
        ) as cursor:
            rows = await cursor.fetchall()
        data["items"] = []
        for item_id, group in groupby(rows, key=lambda r: r["id"]):
            group = list(group)
            rated = [r for r in group if r["criterion_key"] is not None]
            data["items"].append(
                {
                    "id": item_id,
                    "name": group[0]["name"],
                    "matches_played": group[0]["matches_played"],
                    "mu": {r["criterion_key"]: r["mu"] for r in rated},
                    "sigma_sq": {r["criterion_key"]: r["sigma_sq"] for r in rated},
                    "criterion_matches": {
                        r["criterion_key"]: r["criterion_matches"] for r in rated
                    },
                }
            )
        async with db.execute(
            "SELECT token, item1_id, item2_id, item3_id, issued_at "
            "FROM active_rounds WHERE session_id = ?",
            sid,
        ) as cursor:
            active = await cursor.fetchone()
        if active:
            data["active_round"] = {
                key: value for key, value in dict(active).items() if value is not None
            }
        async with db.execute(
            "SELECT event FROM vote_events WHERE session_id = ? AND undone = 0 "
            "ORDER BY id DESC LIMIT 5",
            sid,
        ) as cursor:
            self._recent = [
                json.loads(r["event"])["payload"] for r in await cursor.fetchall()
            ]
        async with db.execute(
            "SELECT state FROM ranking_models WHERE session_id = ?", sid
        ) as cursor:
            model = await cursor.fetchone()
        if model is None:
            raise InvalidSessionDataError("보존된 순위 관측을 찾을 수 없습니다.")
        data.update(json.loads(model["state"]))
        self._data = data
        return True

    def _core(self) -> dict[str, Any]:
        """백업에 들어가는 현재 상태. 평점 모형은 관측에서 다시 계산합니다."""
        return {
            key: deepcopy(self._data[key])
            for key in (
                "settings",
                "criteria",
                "items",
                "next_item_id",
                "observations",
                "exposures",
            )
        }

    async def _save_to_db(self) -> None:
        """검증한 상태와 투표 기록 변경을 단일 트랜잭션으로 기록합니다."""
        self._data = SessionDataModel.model_validate(self._data).model_dump(
            mode="python"
        )
        try:
            self._revision = await database.save_session_data(
                self._session_id,
                self._data,
                created_at=self._created_at,
                last_accessed=time.time(),
                expected_revision=self._revision,
                events=self._events,
                core_bytes=len(_dumps(self._core()).encode()),
            )
        except (OSError, sqlite3.Error) as exc:
            logger.error(
                "session_save_failed — session_id=%s: %s", self._session_id, exc
            )
            raise SessionSaveError(
                "저장하지 못했습니다. 잠시 후 다시 시도해주세요."
            ) from exc
        self._events = EventChanges()

    @asynccontextmanager
    async def _mutation(self) -> AsyncIterator[None]:
        """세션 락 안에서 최신 상태를 읽고, 실패하면 저장 전 상태로 되돌립니다."""
        async with _get_lock(self._session_id):
            await self._reload()
            try:
                yield
            except BaseException:
                await asyncio.shield(self._load())
                raise

    # --- Read access ---

    @property
    def settings(self) -> dict[str, Any]:
        return self._data["settings"]

    @property
    def criteria(self) -> list[dict[str, Any]]:
        return self._data["criteria"]

    @property
    def items(self) -> list[dict[str, Any]]:
        return self._data["items"]

    @property
    def active_round(self) -> dict[str, Any] | None:
        return deepcopy(self._data["active_round"])

    def get_item(self, item_id: int) -> dict[str, Any] | None:
        for item in self._data["items"]:
            if item["id"] == item_id:
                return item
        return None

    def recent_votes(self) -> list[dict[str, Any]]:
        """취소되지 않은 최근 투표 다섯 건의 대결 구성입니다."""
        return deepcopy(self._recent)

    def response_rate(self, key: str) -> float:
        answered = sum(row["count"] for row in self._data["observations"].get(key, []))
        return (answered + 1) / (self._data["exposures"].get(key, 0) + 2)

    def posterior(self, key: str) -> Posterior:
        if key not in self._posterior_cache:
            state = self._data["posteriors"].get(key)
            self._posterior_cache[key] = (
                Posterior.from_dict(state)
                if state
                else fit_rankings([], initial_sigma=self.settings["initial_sigma"])
            )
        return self._posterior_cache[key]

    async def history_events(self) -> list[dict[str, Any]]:
        return await database.fetch_events(self._session_id)

    async def history_page(self, page: int, size: int = 50) -> dict[str, Any]:
        total = await database.count_events(self._session_id)
        pages = max(1, -(-total // size))
        page = min(max(1, page), pages)
        events = await database.fetch_events(
            self._session_id, limit=size, offset=(page - 1) * size, newest_first=True
        )
        last = await database.fetch_events(
            self._session_id, limit=1, newest_first=True, active_only=True
        )
        return {
            "events": events,
            "page": page,
            "pages": pages,
            "total": total,
            "last_event_id": last[0]["id"] if last else None,
        }

    # --- Settings, criteria and items ---

    async def update_settings(self, patch: dict[str, Any]) -> None:
        async with self._mutation():
            previous_sigma = self.settings["initial_sigma"]
            self._data["settings"].update(patch)
            self._data = SessionDataModel.model_validate(self._data).model_dump(
                mode="python"
            )
            if self.settings["initial_sigma"] != previous_sigma:
                await self._refit({c["key"] for c in self.criteria})
            self._data["active_round"] = None
            await self._save_to_db()

    async def set_criteria(self, criteria: list[dict[str, Any]]) -> None:
        """평가 기준 전체 교체. 같은 key의 통계와 관측은 유지합니다."""
        async with self._mutation():
            old = {c["key"]: c for c in self.criteria}
            new_keys = {c["key"] for c in criteria}
            added = new_keys - old.keys()
            removed = old.keys() - new_keys
            initial_sq = self.settings["initial_sigma"] ** 2
            for item in self.items:
                for key in added:
                    item["mu"][key] = 0.0
                    item["sigma_sq"][key] = initial_sq
                    item["criterion_matches"][key] = 0
                for key in removed:
                    for field in ("mu", "sigma_sq", "criterion_matches"):
                        item[field].pop(key)
            self._data["criteria"] = [
                {
                    **c,
                    "battles": old[c["key"]]["battles"] if c["key"] in old else 0,
                    "draws": old[c["key"]]["draws"] if c["key"] in old else 0,
                }
                for c in criteria
            ]
            for field in ("observations", "exposures", "posteriors"):
                for key in removed:
                    self._data[field].pop(key, None)
            self._posterior_cache.clear()
            self._data["active_round"] = None
            if added or removed:
                # 구조가 바뀌기 전 기록은 보관하되 개별 취소는 막습니다.
                self._events.archive_all = True
            await self._save_to_db()

    def _new_item(self, name: str) -> dict[str, Any]:
        item_id = self._data["next_item_id"]
        self._data["next_item_id"] += 1
        initial_sq = self.settings["initial_sigma"] ** 2
        keys = [c["key"] for c in self.criteria]
        return {
            "id": item_id,
            "name": name,
            "mu": dict.fromkeys(keys, 0.0),
            "sigma_sq": dict.fromkeys(keys, initial_sq),
            "matches_played": 0,
            "criterion_matches": dict.fromkeys(keys, 0),
        }

    async def add_items(self, names: list[str]) -> int:
        """이름 목록을 추가하고 추가한 개수를 반환합니다."""
        names = [n.strip() for n in names if n.strip()]
        if not names:
            return 0
        async with self._mutation():
            self.items.extend(self._new_item(name) for name in names)
            self._data["active_round"] = None
            await self._save_to_db()
        return len(names)

    async def rename_item(self, item_id: int, name: str) -> bool:
        async with self._mutation():
            item = self.get_item(item_id)
            if not item:
                return False
            item["name"] = name.strip()
            await self._save_to_db()
            return True

    async def delete_item(self, item_id: int) -> bool:
        """과거 비교는 상대 점수 계산에 남기고 화면에서만 제외합니다."""
        async with self._mutation():
            if not self.get_item(item_id):
                return False
            self._data["items"] = [i for i in self.items if i["id"] != item_id]
            self._data["active_round"] = None
            self._events.archive_all = True
            await self._save_to_db()
            return True

    # --- Backup ---

    async def export_json(self) -> str:
        async with _get_lock(self._session_id):
            await self._reload()
            history = await database.fetch_events(self._session_id)
        return _dumps(
            {
                "schema_version": BACKUP_SCHEMA_VERSION,
                **self._core(),
                "history": history,
            }
        )

    @staticmethod
    def parse_import(raw: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """백업 형식 4를 검증하고 (현재 상태, 투표 기록)을 반환합니다."""

        def reject_constant(value: str):
            raise InvalidSessionDataError(f"유한하지 않은 수치가 있습니다: {value}")

        parsed = json.loads(raw, parse_constant=reject_constant)
        if not isinstance(parsed, dict) or parsed.get("schema_version") != 4:
            raise InvalidSessionDataError("백업 형식 4 파일만 가져올 수 있습니다.")
        backup = BackupModel.model_validate(parsed)
        data = SessionDataModel.model_validate(
            {
                key: parsed[key]
                for key in (
                    "settings",
                    "criteria",
                    "items",
                    "next_item_id",
                    "observations",
                    "exposures",
                )
            }
        ).model_dump(mode="python")
        history = [event.model_dump(mode="python") for event in backup.history]
        if len({e["id"] for e in history}) != len(history):
            raise InvalidSessionDataError("투표 기록 ID가 중복되었습니다.")
        history.sort(key=lambda e: e["id"])

        item_ids = {item["id"] for item in data["items"]}
        criterion_keys = {c["key"] for c in data["criteria"]}
        active_counts: Counter = Counter()
        active_exposures: Counter = Counter()
        active_item_ballots: Counter = Counter()
        for event in history:
            payload = event["payload"]
            ids = _vote_ids(payload)
            if event["archived"]:
                continue
            if not set(ids) <= item_ids or set(payload["votes"]) != criterion_keys:
                raise InvalidSessionDataError(
                    "보관되지 않은 투표 기록이 현재 항목·기준과 맞지 않습니다."
                )
            if event["undone"]:
                continue
            answered = False
            for key, vote in payload["votes"].items():
                active_exposures[key] += 1
                ranking = ballot_ranking(ids, vote)
                if ranking is not None:
                    active_counts[key, ranking] += 1
                    answered = True
            if answered:
                active_item_ballots.update(ids)
        # 취소 가능한 기록이 보존된 관측보다 많으면 취소가 관측을 음수로 만듭니다.
        available = {
            key: Counter({row["groups"]: row["count"] for row in rows})
            for key, rows in data["observations"].items()
        }
        if (
            any(
                count > available.get(key, Counter())[ranking]
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
                "취소할 수 있는 투표 기록이 보존된 관측 수보다 많습니다."
            )
        return data, history

    @staticmethod
    def preview_import(raw: str) -> dict[str, int]:
        data, history = DataStore.parse_import(raw)
        return {
            "items": len(data["items"]),
            "criteria": len(data["criteria"]),
            "history": len(history),
        }

    async def _replace_with(
        self, data: dict[str, Any], history: list[dict[str, Any]]
    ) -> None:
        self._data = {**data, "posteriors": {}, "active_round": None}
        self._events.replace = history
        await self._refit({c["key"] for c in self.criteria})

    async def import_json(self, raw: str) -> None:
        """검증한 백업으로 현재 랭킹을 교체합니다."""
        data, history = self.parse_import(raw)
        async with self._mutation():
            await self._replace_with(data, history)
            await self._save_to_db()

    async def stage_import(self, raw: str) -> dict[str, int]:
        """백업을 검증해 보관하고, 확인하면 같은 상태에서만 교체합니다."""
        preview = self.preview_import(raw)
        async with transaction() as db:
            await db.execute(
                "INSERT INTO pending_imports (session_id, revision, raw, created_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(session_id) DO UPDATE SET "
                "revision=excluded.revision, raw=excluded.raw, created_at=excluded.created_at",
                (self._session_id, self._revision, raw, time.time()),
            )
        return preview

    async def apply_staged_import(self) -> None:
        async with self._mutation():
            async with transaction() as db:
                async with db.execute(
                    "SELECT revision, raw, created_at FROM pending_imports WHERE session_id = ?",
                    (self._session_id,),
                ) as cursor:
                    row = await cursor.fetchone()
                await db.execute(
                    "DELETE FROM pending_imports WHERE session_id = ?",
                    (self._session_id,),
                )
            if (
                row is None
                or row["revision"] != self._revision
                or row["created_at"] < time.time() - PENDING_IMPORT_TTL_SECONDS
            ):
                raise StaleImportError(
                    "미리보기 뒤에 랭킹이 바뀌었거나 미리보기 시간이 지났습니다. "
                    "파일을 다시 선택해주세요."
                )
            await self._replace_with(*self.parse_import(row["raw"]))
            await self._save_to_db()

    # --- Battles ---

    def reusable_round(
        self, size: int, focus_id: int | None = None
    ) -> tuple[str, list[dict[str, Any]]] | None:
        """새로고침·다른 탭에서도 같은 대결을 이어서 보여줍니다."""
        active = self._data["active_round"]
        if not active:
            return None
        ids = _vote_ids(active)
        items = [self.get_item(iid) for iid in ids]
        if (
            len(ids) != size
            or (focus_id is not None and focus_id not in ids)
            or any(item is None for item in items)
        ):
            return None
        return active["token"], items

    async def issue_battle_round(self, ids: list[int]) -> str:
        """대결 라운드만 기록합니다. 나머지 상태는 다시 쓰지 않습니다."""
        if len(ids) not in (2, 3) or len(set(ids)) != len(ids):
            raise BattleItemNotFoundError("대결 항목은 서로 다른 2~3개여야 합니다.")
        round_data = {
            "token": secrets.token_urlsafe(24),
            "item1_id": ids[0],
            "item2_id": ids[1],
            "issued_at": time.time(),
        }
        if len(ids) == 3:
            round_data["item3_id"] = ids[2]
        async with _get_lock(self._session_id):
            try:
                await database.save_active_round(self._session_id, round_data)
            except LookupError as exc:
                raise BattleItemNotFoundError(
                    "대결 항목이 바뀌었습니다. 새로고침해주세요."
                ) from exc
        self._data["active_round"] = round_data
        return round_data["token"]

    def _validate_active_round(self, payload: Any, ids: list[int]) -> None:
        active = self._data["active_round"]
        if (
            not active
            or active["token"] != payload.round_token
            or _vote_ids(active) != ids
        ):
            raise StaleBattleRoundError(
                "이 대결은 이미 저장되었거나 바뀌었습니다. 새로고침하면 이어서 할 수 있습니다."
            )

    def _change_observation(self, key: str, ranking, amount: int) -> None:
        rows = self._data["observations"].setdefault(key, [])
        for row in rows:
            if tuple(tuple(group) for group in row["groups"]) == ranking:
                row["count"] += amount
                if row["count"] < 0:
                    raise InvalidSessionDataError("취소할 관측이 없습니다.")
                if row["count"] == 0:
                    rows.remove(row)
                break
        else:
            if amount < 0:
                raise InvalidSessionDataError("취소할 관측이 없습니다.")
            rows.append({"groups": ranking, "count": amount})
        rows.sort(key=lambda row: tuple(tuple(g) for g in row["groups"]))

    async def _refit(self, keys: set[str]) -> None:
        """보존된 모든 순위 응답을 설정된 사전분포에서 다시 적합합니다."""
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
            counts: Counter = Counter()
            draws = 0
            rows = observations[key]
            for row in rows:
                for group in row["groups"]:
                    for iid in group:
                        counts[iid] += row["count"]
                if any(len(group) > 1 for group in row["groups"]):
                    draws += row["count"]
            criterion = next(c for c in self.criteria if c["key"] == key)
            criterion["battles"] = sum(row["count"] for row in rows)
            criterion["draws"] = draws
            for item in self.items:
                iid = item["id"]
                item["mu"][key] = posterior.mean(iid)
                item["sigma_sq"][key] = variances.get(iid, sigma**2)
                item["criterion_matches"][key] = counts[iid]

    async def apply_vote(
        self, payload: BattleVoteRequest | ThreeWayBattleVoteRequest
    ) -> dict[str, Any]:
        ids = [payload.item1_id, payload.item2_id]
        if isinstance(payload, ThreeWayBattleVoteRequest):
            ids.append(payload.item3_id)
        async with self._mutation():
            self._validate_active_round(payload, ids)
            if any(self.get_item(iid) is None for iid in ids):
                raise BattleItemNotFoundError("대결 항목을 찾을 수 없습니다.")
            keys = [c["key"] for c in self.criteria]
            if set(payload.votes) != set(keys):
                raise InvalidBattleVoteError("모든 기준에 답하거나 건너뛰어야 합니다.")
            try:
                rankings = {
                    key: ballot_ranking(ids, vote)
                    for key, vote in payload.votes.items()
                }
            except ValueError as exc:
                raise InvalidBattleVoteError(str(exc)) from exc
            old = {iid: dict(self.get_item(iid)["mu"]) for iid in ids}
            answered = {key for key, ranking in rankings.items() if ranking is not None}
            for key, ranking in rankings.items():
                self._data["exposures"][key] = self._data["exposures"].get(key, 0) + 1
                if ranking is not None:
                    self._change_observation(key, ranking, 1)
            await self._refit(answered)
            if answered:
                for iid in ids:
                    self.get_item(iid)["matches_played"] += 1
            results = [
                self._criterion_result(criterion, ids, rankings, old, payload.votes)
                for criterion in self.criteria
            ]
            stored = VotePayloadModel(
                item1_id=ids[0],
                item2_id=ids[1],
                item3_id=ids[2] if len(ids) == 3 else None,
                votes=payload.votes,
            ).model_dump(mode="python")
            self._events.insert = {
                "mode": "3way" if len(ids) == 3 else "2way",
                "created_at": time.time(),
                "undone": False,
                "archived": False,
                "payload": stored,
                "names": {str(iid): self.get_item(iid)["name"] for iid in ids},
                "labels": {c["key"]: c["label"] for c in self.criteria},
            }
            self._data["active_round"] = None
            await self._save_to_db()
            self._recent = [stored, *self._recent][:5]
        response = {
            "results": results,
            "total_items": len(self.items),
            "next_url": payload.redirect_to or "/battle",
        }
        for index, iid in enumerate(ids, 1):
            response[f"a{index}_id"] = iid
            response[f"a{index}_name"] = self.get_item(iid)["name"]
        return response

    def _criterion_result(self, criterion, ids, rankings, old, votes) -> dict:
        key = criterion["key"]
        result = {field: criterion[field] for field in ("key", "label", "color")}
        ranking = rankings[key]
        if ranking is None:
            return {**result, "skipped": True}

        def change(iid: int) -> tuple[float, float, float]:
            item = self.get_item(iid)
            old_r = display_rating(self, old[iid][key])
            new_r = display_rating(self, item["mu"][key])
            sigma = display_uncertainty(self, item["sigma_sq"][key])
            return round(old_r, 1), round(new_r, 1), round(sigma, 1)

        if len(ids) == 2:
            result["winner"] = votes[key]
            for index, iid in enumerate(ids, 1):
                old_r, new_r, sigma = change(iid)
                result |= {
                    f"old_r{index}": old_r,
                    f"new_r{index}": new_r,
                    f"diff_r{index}": round(new_r - old_r, 1),
                    f"sigma{index}": sigma,
                }
            return result
        result |= {
            "best_id": ranking[0][0] if len(ranking[0]) == 1 else None,
            "worst_id": ranking[-1][0] if len(ranking[-1]) == 1 else None,
            "middle_id": ranking[1][0] if len(ranking) == 3 else None,
            "ratings": {},
            "diffs": {},
            "sigmas": {},
        }
        for iid in ids:
            old_r, new_r, sigma = change(iid)
            result["ratings"][str(iid)] = new_r
            result["diffs"][str(iid)] = round(new_r - old_r, 1)
            result["sigmas"][str(iid)] = sigma
        return result

    # --- History ---

    async def undo_last_vote(
        self, expected_event_id: int | None = None
    ) -> dict[str, Any]:
        """마지막 투표의 관측을 빼고 전체를 다시 적합합니다."""
        async with self._mutation():
            last = await database.fetch_events(
                self._session_id, limit=1, newest_first=True, active_only=True
            )
            if not last:
                raise StaleBattleRoundError("취소할 투표가 없습니다.")
            event = last[0]
            if expected_event_id is not None and event["id"] != expected_event_id:
                raise StaleBattleRoundError(
                    "이미 취소되었거나 투표 기록이 바뀌었습니다. 새로고침해주세요."
                )
            ids = _vote_ids(event["payload"])
            answered = set()
            for key, vote in event["payload"]["votes"].items():
                self._data["exposures"][key] -= 1
                ranking = ballot_ranking(ids, vote)
                if ranking is not None:
                    self._change_observation(key, ranking, -1)
                    answered.add(key)
            if answered:
                for iid in ids:
                    self.get_item(iid)["matches_played"] -= 1
            await self._refit(answered)
            self._events.undo = event["id"]
            self._data["active_round"] = None
            await self._save_to_db()
            return {**event, "undone": True}

    async def recalculate_ratings(
        self, settings_patch: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """보관된 기록을 포함한 모든 순위 응답을 현재 설정으로 다시 적합합니다."""
        async with self._mutation():
            self._data["settings"].update(settings_patch or {})
            self._data = SessionDataModel.model_validate(self._data).model_dump(
                mode="python"
            )
            await self._refit({c["key"] for c in self.criteria})
            self._data["active_round"] = None
            await self._save_to_db()
            return {
                "responses": sum(c["battles"] for c in self.criteria),
                "items": deepcopy(self.items),
            }

    async def clear_history(self) -> None:
        """상세 기록만 지웁니다. 순위 응답 횟수는 남아 다시 계산할 수 있습니다."""
        async with self._mutation():
            self._events.replace = []
            await self._save_to_db()


# --- 세션 관리 ---


async def open_store(session_id: str) -> DataStore | None:
    """저장된 랭킹을 엽니다. 없으면 None입니다."""
    store = DataStore(session_id)
    return store if await store._load(touch=True) else None


async def create_store(session_id: str) -> DataStore:
    """기본 기준을 가진 새 랭킹을 저장합니다."""
    store = DataStore(session_id)
    store._data = SessionDataModel().model_dump(mode="python")
    store._created_at = time.time()
    async with _get_lock(session_id):
        await store._save_to_db()
    return store


async def delete_session(session_id: str) -> None:
    """진행 중인 세션 명령 뒤에 삭제하고 새 저장은 revision으로 차단합니다."""
    async with _get_lock(session_id), transaction() as db:
        await db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))


async def cleanup_expired_sessions() -> int:
    """목록에 없는 만료 세션, 빈 목록, 오래된 가져오기 미리보기를 정리합니다."""
    now = time.time()
    cutoff = now - SESSION_TTL_SECONDS
    async with transaction() as db:
        async with db.execute(
            "SELECT id FROM sessions WHERE last_accessed < ? "
            "AND id NOT IN (SELECT session_id FROM boards)",
            (cutoff,),
        ) as cursor:
            expired = [row["id"] for row in await cursor.fetchall()]
        await db.execute(
            "DELETE FROM libraries WHERE created_at < ? "
            "AND id NOT IN (SELECT library_id FROM boards)",
            (cutoff,),
        )
        await db.execute(
            "DELETE FROM pending_imports WHERE created_at < ?",
            (now - PENDING_IMPORT_TTL_SECONDS,),
        )
    removed = 0
    for session_id in expired:
        async with _get_lock(session_id), transaction() as db:
            cursor = await db.execute(
                "DELETE FROM sessions WHERE id = ? AND last_accessed < ? "
                "AND id NOT IN (SELECT session_id FROM boards)",
                (session_id, cutoff),
            )
            removed += cursor.rowcount
    if removed:
        logger.info("cleanup_expired_sessions — removed %d sessions", removed)
    return removed
