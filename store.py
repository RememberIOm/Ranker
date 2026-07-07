# store.py
# 세션 기반 SQLite 데이터 저장소 — 각 사용자가 독립된 데이터를 운용합니다.
# UUID 세션 ID를 키로 사용하며, 단일 SQLite DB에 모든 세션을 저장합니다.

import asyncio
import json
import logging
import os
import secrets
import time
from itertools import groupby
from typing import Any

from pydantic import ValidationError

from database import get_db, _insert_session_data, db_write_lock
from schemas import (
    ActiveRoundModel,
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
_locks: dict[str, asyncio.Lock] = {}


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
    if session_id not in _locks:
        _locks[session_id] = asyncio.Lock()
    return _locks[session_id]


def _default_data() -> dict[str, Any]:
    """초기 스키마 — 새 세션 생성 시 사용됩니다."""
    return SessionDataModel().model_dump(mode="python")


_VOTE_OUTCOMES = {"1": 1.0, "2": 0.0, "draw": 0.5}


def _coerce(value: Any, cast: type, default: Any) -> Any:
    """형변환 실패 시 default를 반환하는 관대 변환 — 과거 포맷 보정용."""
    try:
        return cast(value)
    except (TypeError, ValueError):
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
            settings["draw_bandwidth"] = draw_scale / 173.72 if draw_scale is not None else 1.5
            settings["initial_sigma"] = 2.0
            settings["hierarchical_strength"] = 5.0
            settings["display_center"] = _coerce(settings_raw.get("initial_rating", 1200.0), float, 1200.0)
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

            normalized_battles = max(0, _coerce(raw_criterion.get("battles", 0), int, 0))
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
    initial_sigma = _coerce(settings.get("initial_sigma", default_sigma), float, default_sigma)
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
                    old_r = _coerce(ratings_raw.get(key, display_center), float, display_center)
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
                    key: _coerce(mu_raw.get(key, 0.0), float, 0.0) for key in allowed_keys
                }
                sigma_sq = {
                    key: max(0.01, _coerce(sigma_sq_raw.get(key, initial_sigma_sq), float, initial_sigma_sq))
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

    @classmethod
    async def create(cls, session_id: str) -> "DataStore":
        """비동기 팩토리 — DB에서 데이터를 로드한 DataStore를 반환합니다."""
        instance = cls(session_id)
        await instance._load_from_db()
        return instance

    async def _load_from_db(self) -> None:
        """SQLite에서 전체 세션 데이터를 메모리 dict로 조립합니다."""
        db = get_db()

        # 세션 메타
        async with db.execute(
            "SELECT settings, created_at FROM sessions WHERE id = ?",
            (self._session_id,),
        ) as cursor:
            row = await cursor.fetchone()

        if row is None:
            self._data = _default_data()
            self._created_at = time.time()
            return

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

    async def _save_to_db(self) -> None:
        """메모리 상태를 SQLite에 기록합니다 (단일 트랜잭션)."""
        try:
            await _insert_session_data(
                get_db(),
                self._session_id,
                self._data,
                created_at=self._created_at,
                last_accessed=time.time(),
            )
        except OSError as exc:
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
            raise InvalidBattleVoteError(f"투표가 누락된 기준이 있습니다: {sorted(missing)}")

    # --- Settings ---

    @property
    def settings(self) -> dict[str, Any]:
        return self._data["settings"]

    async def update_settings(self, patch: dict[str, Any]) -> None:
        async with _get_lock(self._session_id):
            self._data["settings"].update(patch)
            await self._save_to_db()

    # --- Criteria ---

    @property
    def criteria(self) -> list[dict[str, Any]]:
        return self._data["criteria"]

    async def set_criteria(self, criteria: list[dict[str, Any]]) -> None:
        """평가 기준 전체 교체 — 기존 아이템의 mu/sigma_sq도 동기화합니다."""
        async with _get_lock(self._session_id):
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
            item = self._new_item(self._next_id(), name.strip())
            self._data["items"].append(item)
            self._invalidate_active_round()
            await self._save_to_db()
            return item

    async def add_items_bulk(self, names: list[str]) -> int:
        """여러 항목을 한번에 추가합니다. 추가된 개수를 반환합니다."""
        async with _get_lock(self._session_id):
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
            item = self.get_item(item_id)
            if not item:
                return False
            item.update(fields)
            await self._save_to_db()
            return True

    async def delete_item(self, item_id: int) -> bool:
        async with _get_lock(self._session_id):
            before = len(self._data["items"])
            self._data["items"] = [i for i in self._data["items"] if i["id"] != item_id]
            if len(self._data["items"]) < before:
                self._invalidate_active_round()
                await self._save_to_db()
                return True
            return False

    async def save(self) -> None:
        """외부에서 메모리 데이터 변경 후 명시적으로 저장할 때 사용합니다."""
        async with _get_lock(self._session_id):
            await self._save_to_db()

    # --- Import / Export ---

    def export_json(self) -> str:
        return json.dumps(self._data, ensure_ascii=False, indent=2)

    async def import_json(self, raw: str) -> None:
        """JSON 문자열로부터 전체 데이터를 교체합니다.

        _load()와 동일한 관대 파싱을 사용하여 이전 버전 Export 파일도 수용합니다.
        """
        parsed = json.loads(raw)
        normalized = _normalize_loaded_data(parsed)
        validated = SessionDataModel.model_validate(normalized)
        async with _get_lock(self._session_id):
            self._data = validated.model_dump(mode="python")
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

    async def apply_battle_vote(self, payload: BattleVoteRequest) -> dict[str, Any]:
        from services import (
            bt_update,
            hierarchical_shrinkage,
            display_rating,
            display_uncertainty,
        )

        async with _get_lock(self._session_id):
            # 락 이전에 로드된 스냅샷은 동시 요청으로 이미 낡았을 수 있음 — 락 안에서 재로드
            await self._load_from_db()
            self._validate_active_round(payload)

            a1 = self.get_item(payload.item1_id)
            a2 = self.get_item(payload.item2_id)
            if not a1 or not a2:
                self._invalidate_active_round()
                raise BattleItemNotFoundError("대결 항목을 찾을 수 없습니다.")

            self._validate_vote_keys(payload.votes)

            criteria = self._data["criteria"]
            initial_sq = self._data["settings"]["initial_sigma"] ** 2
            results: list[dict[str, Any]] = []

            for criterion in criteria:
                key = criterion["key"]
                winner = payload.votes[key]

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
                hierarchical_shrinkage(self, a1)
                hierarchical_shrinkage(self, a2)

            a1["matches_played"] += 1
            a2["matches_played"] += 1
            self._invalidate_active_round()
            await self._save_to_db()

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
            sigmoid,
            _SIGMA_SQ_FLOOR,
            hierarchical_shrinkage,
            display_rating,
            display_uncertainty,
        )

        async with _get_lock(self._session_id):
            # 락 이전에 로드된 스냅샷은 동시 요청으로 이미 낡았을 수 있음 — 락 안에서 재로드
            await self._load_from_db()
            self._validate_active_round(payload, item3_id=payload.item3_id)

            item_ids = [payload.item1_id, payload.item2_id, payload.item3_id]
            items_3 = [self.get_item(iid) for iid in item_ids]
            if not all(items_3):
                self._invalidate_active_round()
                raise BattleItemNotFoundError("대결 항목을 찾을 수 없습니다.")

            self._validate_vote_keys(payload.votes)

            criteria = self._data["criteria"]
            initial_sq = self._data["settings"]["initial_sigma"] ** 2
            results: list[dict[str, Any]] = []
            id_str = {iid: str(iid) for iid in item_ids}

            for criterion in criteria:
                key = criterion["key"]
                vote = payload.votes[key]

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

                # 동시 업데이트: 원본 값에서 모든 그래디언트·정밀도를 계산 후 일괄 적용
                # 순차 적용 시 후속 쌍이 이미 변경된 값을 사용하는 편향을 제거합니다.
                orig_mu = {iid: item_by_id[iid]["mu"].get(key, 0.0) for iid in item_ids}
                orig_sq = {
                    iid: item_by_id[iid]["sigma_sq"].get(key, initial_sq)
                    for iid in item_ids
                }
                grad_accum: dict[int, float] = {iid: 0.0 for iid in item_ids}
                w_accum: dict[int, float] = {iid: 0.0 for iid in item_ids}

                for a_id, b_id, outcome in pairs:
                    p = sigmoid(orig_mu[a_id] - orig_mu[b_id])
                    w = p * (1.0 - p)
                    g = outcome - p
                    grad_accum[a_id] += g
                    grad_accum[b_id] -= g
                    w_accum[a_id] += w
                    w_accum[b_id] += w

                for iid in item_ids:
                    prec_new = 1.0 / orig_sq[iid] + w_accum[iid]
                    item_by_id[iid]["mu"][key] = (
                        orig_mu[iid] + grad_accum[iid] / prec_new
                    )
                    item_by_id[iid]["sigma_sq"][key] = max(
                        _SIGMA_SQ_FLOOR, 1.0 / prec_new
                    )

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
                    hierarchical_shrinkage(self, item)

            for item in items_3:
                item["matches_played"] += 1
            self._invalidate_active_round()
            await self._save_to_db()

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
    # last_accessed 갱신
    db = get_db()
    async with db_write_lock:
        await db.execute(
            "UPDATE sessions SET last_accessed = ? WHERE id = ?",
            (time.time(), session_id),
        )
        await db.commit()
    return store


async def session_exists(session_id: str) -> bool:
    """세션이 DB에 존재하는지 확인합니다."""
    db = get_db()
    async with db.execute(
        "SELECT 1 FROM sessions WHERE id = ?",
        (session_id,),
    ) as cursor:
        return await cursor.fetchone() is not None


async def delete_session(session_id: str) -> None:
    """세션 데이터를 DB에서 삭제하고 런타임 상태를 정리합니다."""
    db = get_db()
    async with db_write_lock:
        await db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        await db.commit()
    _locks.pop(session_id, None)


async def cleanup_expired_sessions() -> int:
    """만료된 세션과 그 런타임 락을 정리합니다. 삭제된 개수를 반환합니다."""
    cutoff = time.time() - SESSION_TTL_SECONDS
    db = get_db()
    async with db_write_lock:
        async with db.execute(
            "SELECT id FROM sessions WHERE last_accessed < ?", (cutoff,)
        ) as cursor:
            expired = [row["id"] for row in await cursor.fetchall()]
        if expired:
            await db.execute("DELETE FROM sessions WHERE last_accessed < ?", (cutoff,))
            await db.commit()
    for session_id in expired:
        _locks.pop(session_id, None)
    if expired:
        logger.info("cleanup_expired_sessions — removed %d sessions", len(expired))
    return len(expired)
