# store.py
# 세션 기반 JSON 데이터 저장소 — 각 사용자가 독립된 데이터를 운용합니다.
# UUID 세션 ID를 키로 사용하며, 세션별 JSON 파일을 /data/sessions/ 에 저장합니다.

import asyncio
import json
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Any

import aiofiles
from pydantic import ValidationError

logger = logging.getLogger("ranker.store")

from schemas import BattleVoteRequest, SessionDataModel

# 환경 변수 SESSION_DIR이 설정되어 있으면 해당 경로를 사용하고,
# 로컬 개발 환경(uvicorn 실행)에서는 권한 오류를 피하기 위해 './data/sessions'를 사용합니다.
SESSION_DIR = Path(os.getenv("SESSION_DIR", "./data/sessions"))
SESSION_DIR.mkdir(parents=True, exist_ok=True)

# 세션 만료 시간 (7일)
SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", str(7 * 24 * 60 * 60)))

# asyncio.Lock은 실행 중인 이벤트 루프 안에서 생성해야 하므로 lazy init
# cooperative scheduling 덕분에 await 없는 구간은 원자적 — dict 가드 불필요
# ⚠️ 단일 uvicorn 워커 전제 — 멀티 워커(Gunicorn) 환경에서는 프로세스 간 Lock을
#    공유할 수 없으므로 filelock 패키지로 교체 필요. fly.toml 참고.
_locks: dict[str, asyncio.Lock] = {}
_session_cache: dict[str, tuple["DataStore", float]] = {}


class InvalidBattleVoteError(ValueError):
    """투표 페이로드가 현재 세션 상태와 맞지 않을 때 발생합니다."""


class StaleBattleRoundError(RuntimeError):
    """이미 처리되었거나 만료된 대결 라운드일 때 발생합니다."""


class BattleItemNotFoundError(LookupError):
    """대결 중인 항목을 찾을 수 없을 때 발생합니다."""


class SessionSaveError(RuntimeError):
    """세션 파일 저장에 실패했을 때 발생합니다 (디스크 풀, 권한 거부 등)."""


class InvalidSessionDataError(ValueError):
    """세션 파일이 손상되었거나 현재 스키마로 복구할 수 없을 때 발생합니다."""


def _get_lock(session_id: str) -> asyncio.Lock:
    """세션별 asyncio.Lock을 반환합니다 (lazy init)."""
    if session_id not in _locks:
        _locks[session_id] = asyncio.Lock()
    return _locks[session_id]


def _default_data() -> dict[str, Any]:
    """초기 JSON 스키마 — 새 세션 또는 파일이 없을 때 생성됩니다."""
    return SessionDataModel().model_dump(mode="python")


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
            draw_scale = settings_raw.get("elo_draw_scale", 300.0)
            try:
                settings["draw_bandwidth"] = float(draw_scale) / 173.72
            except (TypeError, ValueError):
                settings["draw_bandwidth"] = 1.5
            settings["initial_sigma"] = 2.0
            settings["hierarchical_strength"] = 5.0
            old_initial = settings_raw.get("initial_rating", 1200.0)
            try:
                settings["display_center"] = float(old_initial)
            except (TypeError, ValueError):
                settings["display_center"] = 1200.0
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

            weight = raw_criterion.get("weight", 1.0)
            try:
                normalized_weight = float(weight)
            except (TypeError, ValueError):
                normalized_weight = 1.0
            if normalized_weight <= 0:
                normalized_weight = 1.0

            battles_raw = raw_criterion.get("battles", 0)
            draws_raw = raw_criterion.get("draws", 0)
            try:
                normalized_battles = max(0, int(battles_raw))
            except (TypeError, ValueError):
                normalized_battles = 0
            try:
                normalized_draws = max(0, int(draws_raw))
            except (TypeError, ValueError):
                normalized_draws = 0

            criteria.append({
                "key": normalized_key,
                "label": label.strip(),
                "color": color.strip(),
                "weight": normalized_weight,
                "battles": normalized_battles,
                "draws": normalized_draws,
            })

    if not criteria:
        criteria = defaults["criteria"]

    initial_sigma = settings.get("initial_sigma", defaults["settings"]["initial_sigma"])
    try:
        initial_sigma = float(initial_sigma)
    except (TypeError, ValueError):
        initial_sigma = float(defaults["settings"]["initial_sigma"])
    initial_sigma_sq = initial_sigma ** 2

    display_center = settings.get("display_center", 1200.0)
    display_scale = settings.get("display_scale", 173.72)

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

            matches_played = raw_item.get("matches_played", 0)
            if not isinstance(matches_played, int) or matches_played < 0:
                matches_played = 0

            criterion_matches_raw = raw_item.get("criterion_matches")
            criterion_matches: dict[str, int] = {}
            if isinstance(criterion_matches_raw, dict):
                for key in allowed_keys:
                    val = criterion_matches_raw.get(key, 0)
                    try:
                        criterion_matches[key] = max(0, int(val))
                    except (TypeError, ValueError):
                        criterion_matches[key] = 0

            # Elo→BT 마이그레이션: "ratings" 존재 + "mu" 부재 시 변환
            mu_raw = raw_item.get("mu")
            ratings_raw = raw_item.get("ratings")
            is_legacy = (isinstance(ratings_raw, dict) and not isinstance(mu_raw, dict))

            if is_legacy:
                mu: dict[str, float] = {}
                sigma_sq: dict[str, float] = {}
                for key in allowed_keys:
                    old_r = ratings_raw.get(key, display_center)
                    try:
                        old_r = float(old_r)
                    except (TypeError, ValueError):
                        old_r = display_center
                    mu[key] = (old_r - display_center) / display_scale
                    cm = criterion_matches.get(key, 0)
                    sigma_sq[key] = max(0.1, initial_sigma_sq / (1.0 + cm * 0.25))
            else:
                if not isinstance(mu_raw, dict):
                    mu_raw = {}
                sigma_sq_raw = raw_item.get("sigma_sq")
                if not isinstance(sigma_sq_raw, dict):
                    sigma_sq_raw = {}
                mu = {}
                sigma_sq = {}
                for key in allowed_keys:
                    val = mu_raw.get(key, 0.0)
                    try:
                        mu[key] = float(val)
                    except (TypeError, ValueError):
                        mu[key] = 0.0
                    sq_val = sigma_sq_raw.get(key, initial_sigma_sq)
                    try:
                        sigma_sq[key] = max(0.01, float(sq_val))
                    except (TypeError, ValueError):
                        sigma_sq[key] = initial_sigma_sq

            items.append({
                "id": item_id,
                "name": name.strip(),
                "mu": mu,
                "sigma_sq": sigma_sq,
                "matches_played": matches_played,
                "criterion_matches": criterion_matches,
            })

    # active_round (진행 중인 배틀 라운드) 복원 — 파일에 영속화되어 VM 재시작 후에도 투표 가능.
    # 검증 실패(같은 ID, 잘못된 토큰 등) 시 None으로 관대 복원 — 전체 파일 로드 실패를 피함.
    active_round_raw = data.get("active_round")
    active_round: dict[str, Any] | None = None
    if isinstance(active_round_raw, dict):
        token = active_round_raw.get("token")
        try:
            ar_item1_id = int(active_round_raw.get("item1_id", 0))
            ar_item2_id = int(active_round_raw.get("item2_id", 0))
            ar_issued_at = float(active_round_raw.get("issued_at", 0.0))
        except (TypeError, ValueError):
            ar_item1_id = ar_item2_id = 0
            ar_issued_at = 0.0
        if (
            isinstance(token, str)
            and 16 <= len(token) <= 255
            and ar_item1_id >= 1
            and ar_item2_id >= 1
            and ar_item1_id != ar_item2_id
        ):
            active_round = {
                "token": token,
                "item1_id": ar_item1_id,
                "item2_id": ar_item2_id,
                "issued_at": ar_issued_at,
            }

    return {
        "settings": settings,
        "criteria": criteria,
        "items": items,
        "active_round": active_round,
    }


class DataStore:
    """
    세션별 JSON 데이터 저장소.
    메모리에 데이터를 캐싱하고, 변경 시 세션 파일에 비동기적으로 기록합니다.
    직접 생성하지 말고 DataStore.create(session_id)를 사용하세요.
    """

    def __init__(self, session_id: str) -> None:
        self._session_id = session_id
        self._path = SESSION_DIR / f"{session_id}.json"
        self._data: dict[str, Any] = {}  # create()에서 채워짐 (active_round 포함)

    @classmethod
    async def create(cls, session_id: str) -> "DataStore":
        """비동기 팩토리 — 파일에서 데이터를 로드한 DataStore를 반환합니다."""
        instance = cls(session_id)
        instance._data = await instance._load()
        return instance

    async def _load(self) -> dict[str, Any]:
        if self._path.exists():
            async with aiofiles.open(self._path, "r", encoding="utf-8") as f:
                raw = await f.read()
            try:
                parsed = await asyncio.to_thread(json.loads, raw)
                normalized = _normalize_loaded_data(parsed)
                validated = await asyncio.to_thread(SessionDataModel.model_validate, normalized)
            except (json.JSONDecodeError, ValidationError, InvalidSessionDataError) as exc:
                raise InvalidSessionDataError(f"세션 파일을 읽을 수 없습니다: {self._path.name}") from exc
            return validated.model_dump(mode="python")
        return _default_data()

    async def _save_locked(self) -> None:
        # CPU-bound 직렬화를 스레드풀에 위임하여 이벤트 루프 블로킹 방지 (_load와 일관성 유지)
        try:
            serialized = await asyncio.to_thread(
                json.dumps, self._data, ensure_ascii=False, indent=2
            )
            # 임시 파일에 먼저 쓰고 os.replace로 원자적 교체 — 충돌 시 파일 손상 방지
            tmp_path = self._path.with_suffix(".tmp")
            async with aiofiles.open(tmp_path, "w", encoding="utf-8") as f:
                await f.write(serialized)
            os.replace(tmp_path, self._path)
        except OSError as exc:
            logger.error("session_save_failed — session_id=%s: %s", self._session_id, exc)
            raise SessionSaveError(f"세션 저장에 실패했습니다: {self._session_id}") from exc

    async def _save(self) -> None:
        lock = _get_lock(self._session_id)
        async with lock:
            await self._save_locked()

    def _invalidate_active_round(self) -> None:
        """진행 중인 라운드를 무효화합니다. 호출자가 _save_locked()로 디스크 반영을 책임집니다."""
        self._data["active_round"] = None

    def _get_item_from_data(self, item_id: int) -> dict[str, Any] | None:
        for item in self._data["items"]:
            if item["id"] == item_id:
                return item
        return None

    # --- Settings ---

    @property
    def settings(self) -> dict[str, Any]:
        return self._data["settings"]

    async def update_settings(self, patch: dict[str, Any]) -> None:
        lock = _get_lock(self._session_id)
        async with lock:
            self._data["settings"].update(patch)
            await self._save_locked()

    # --- Criteria ---

    @property
    def criteria(self) -> list[dict[str, Any]]:
        return self._data["criteria"]

    async def set_criteria(self, criteria: list[dict[str, Any]]) -> None:
        """평가 기준 전체 교체 — 기존 아이템의 mu/sigma_sq도 동기화합니다."""
        lock = _get_lock(self._session_id)
        async with lock:
            old_keys = {c["key"] for c in self._data["criteria"]}
            new_keys = {c["key"] for c in criteria}
            added = new_keys - old_keys
            removed = old_keys - new_keys

            initial_sq = self._data["settings"]["initial_sigma"] ** 2

            for item in self._data["items"]:
                for key in added:
                    item["mu"].setdefault(key, 0.0)
                    item["sigma_sq"].setdefault(key, initial_sq)
                for key in removed:
                    item["mu"].pop(key, None)
                    item["sigma_sq"].pop(key, None)

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
            await self._save_locked()

    # --- Items ---

    @property
    def items(self) -> list[dict[str, Any]]:
        return self._data["items"]

    def _next_id(self) -> int:
        if not self._data["items"]:
            return 1
        return max(item["id"] for item in self._data["items"]) + 1

    async def add_item(self, name: str) -> dict[str, Any]:
        lock = _get_lock(self._session_id)
        async with lock:
            initial_sq = self._data["settings"]["initial_sigma"] ** 2
            item = {
                "id": self._next_id(),
                "name": name.strip(),
                "mu": {c["key"]: 0.0 for c in self._data["criteria"]},
                "sigma_sq": {c["key"]: initial_sq for c in self._data["criteria"]},
                "matches_played": 0,
                "criterion_matches": {},
            }
            self._data["items"].append(item)
            self._invalidate_active_round()
            await self._save_locked()
            return item

    async def add_items_bulk(self, names: list[str]) -> int:
        """여러 항목을 한번에 추가합니다. 추가된 개수를 반환합니다."""
        lock = _get_lock(self._session_id)
        async with lock:
            count = 0
            initial_sq = self._data["settings"]["initial_sigma"] ** 2
            next_id = self._next_id()
            for name in names:
                stripped = name.strip()
                if not stripped:
                    continue
                item = {
                    "id": next_id,
                    "name": stripped,
                    "mu": {c["key"]: 0.0 for c in self._data["criteria"]},
                    "sigma_sq": {c["key"]: initial_sq for c in self._data["criteria"]},
                    "matches_played": 0,
                    "criterion_matches": {},
                }
                self._data["items"].append(item)
                next_id += 1
                count += 1
            if count:
                self._invalidate_active_round()
                await self._save_locked()
            return count

    def get_item(self, item_id: int) -> dict[str, Any] | None:
        return self._get_item_from_data(item_id)

    async def update_item(self, item_id: int, **fields: Any) -> bool:
        lock = _get_lock(self._session_id)
        async with lock:
            item = self._get_item_from_data(item_id)
            if not item:
                return False
            item.update(fields)
            await self._save_locked()
            return True

    async def delete_item(self, item_id: int) -> bool:
        lock = _get_lock(self._session_id)
        async with lock:
            before = len(self._data["items"])
            self._data["items"] = [i for i in self._data["items"] if i["id"] != item_id]
            if len(self._data["items"]) < before:
                self._invalidate_active_round()
                await self._save_locked()
                return True
            return False

    async def save(self) -> None:
        """외부에서 메모리 데이터 변경 후 명시적으로 저장할 때 사용합니다."""
        await self._save()

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
        lock = _get_lock(self._session_id)
        async with lock:
            self._data = validated.model_dump(mode="python")
            self._invalidate_active_round()
            await self._save_locked()

    async def issue_battle_round(self, item1_id: int, item2_id: int) -> str:
        """배틀 라운드 토큰을 발급하고 파일에 영속화합니다.

        파일 저장으로 VM 재시작/Fly.io 자동 스케일다운 후에도 사용자가 이어서 투표 가능.
        """
        lock = _get_lock(self._session_id)
        async with lock:
            token = secrets.token_urlsafe(24)
            self._data["active_round"] = {
                "token": token,
                "item1_id": item1_id,
                "item2_id": item2_id,
                "issued_at": time.time(),
            }
            await self._save_locked()
            return token

    async def apply_battle_vote(self, payload: BattleVoteRequest) -> tuple[dict[str, Any], bool]:
        from services import bt_update, hierarchical_shrinkage, display_rating, display_uncertainty

        lock = _get_lock(self._session_id)
        async with lock:
            active_round = self._data.get("active_round")
            if (
                not active_round
                or active_round["token"] != payload.round_token
                or active_round["item1_id"] != payload.item1_id
                or active_round["item2_id"] != payload.item2_id
            ):
                raise StaleBattleRoundError("이 대결은 만료되었거나 이미 처리되었습니다. 새로고침 후 다시 시도해주세요.")

            a1 = self._get_item_from_data(payload.item1_id)
            a2 = self._get_item_from_data(payload.item2_id)
            if not a1 or not a2:
                self._invalidate_active_round()
                raise BattleItemNotFoundError("대결 항목을 찾을 수 없습니다.")

            criteria = self._data["criteria"]
            allowed_keys = {criterion["key"] for criterion in criteria}
            submitted_keys = set(payload.votes)
            unknown_keys = submitted_keys - allowed_keys
            missing_keys = allowed_keys - submitted_keys

            if unknown_keys:
                raise InvalidBattleVoteError(
                    f"알 수 없는 투표 기준이 포함되어 있습니다: {sorted(unknown_keys)}"
                )
            if missing_keys:
                raise InvalidBattleVoteError(
                    f"투표가 누락된 기준이 있습니다: {sorted(missing_keys)}"
                )

            initial_sq = self._data["settings"]["initial_sigma"] ** 2
            results: list[dict[str, Any]] = []

            for criterion in criteria:
                key = criterion["key"]
                winner = payload.votes[key]

                old_mu1 = a1["mu"].get(key, 0.0)
                old_sq1 = a1["sigma_sq"].get(key, initial_sq)
                old_mu2 = a2["mu"].get(key, 0.0)
                old_sq2 = a2["sigma_sq"].get(key, initial_sq)

                match winner:
                    case "1":
                        outcome = 1.0
                    case "2":
                        outcome = 0.0
                    case _:
                        outcome = 0.5

                new_mu1, new_sq1, new_mu2, new_sq2 = bt_update(
                    old_mu1, old_sq1, old_mu2, old_sq2, outcome,
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

                results.append({
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
                })

            # 모든 기준 업데이트 후 계층적 축소
            if self._data["settings"]["hierarchical_strength"] > 0:
                hierarchical_shrinkage(self, a1)
                hierarchical_shrinkage(self, a2)

            a1["matches_played"] += 1
            a2["matches_played"] += 1
            self._invalidate_active_round()
            await self._save_locked()

            return (
                {
                    "a1_id": a1["id"],
                    "a2_id": a2["id"],
                    "a1_name": a1["name"],
                    "a2_name": a2["name"],
                    "results": results,
                    "total_items": len(self._data["items"]),
                    "next_url": payload.redirect_to or "/battle",
                },
                False,  # 정규화 불필요 — Bayesian prior가 대체
            )

    def delete_session(self) -> None:
        """세션 데이터 파일을 삭제합니다."""
        self._invalidate_active_round()
        delete_session(self._session_id)


# --- 세션 관리자 ---

# 메모리 캐시: session_id → (DataStore, last_access_timestamp)
# asyncio cooperative scheduling으로 await 없는 구간은 원자적 — 별도 lock 불필요


def _utime_if_exists(path: Path) -> None:
    """파일이 존재하면 mtime을 현재 시각으로 갱신합니다. 존재하지 않으면 무시."""
    try:
        os.utime(path, None)
    except FileNotFoundError:
        pass


async def get_store(session_id: str) -> DataStore:
    """세션 ID에 해당하는 DataStore를 반환합니다 (캐시 활용)."""
    if session_id in _session_cache:
        store, _ = _session_cache[session_id]
        _session_cache[session_id] = (store, time.time())
        return store

    store = await DataStore.create(session_id)
    # 파일 mtime 갱신 — 서버 재시작 후에도 cleanup이 활성 세션을 삭제하지 않도록 방지.
    # os.utime은 파일이 있을 때만 mtime을 갱신 — Path.touch(exist_ok=True)의 "빈 파일 생성" 경주 회피.
    await asyncio.to_thread(_utime_if_exists, SESSION_DIR / f"{session_id}.json")
    _session_cache[session_id] = (store, time.time())
    return store


def session_exists(session_id: str) -> bool:
    """세션 파일이 존재하는지 확인합니다."""
    return (SESSION_DIR / f"{session_id}.json").exists()


def _purge_runtime_state(session_id: str) -> None:
    _session_cache.pop(session_id, None)
    _locks.pop(session_id, None)


def delete_session(session_id: str) -> None:
    """세션 파일과 메모리 캐시/락을 함께 정리합니다."""
    (SESSION_DIR / f"{session_id}.json").unlink(missing_ok=True)
    _purge_runtime_state(session_id)


async def cleanup_expired_sessions() -> int:
    """만료된 세션 파일을 정리합니다. 삭제된 개수를 반환합니다.

    warm cache(최근 접근된 세션)는 파일 mtime이 오래됐어도 보존 — 투표 없이
    /battle·/ranking만 조회하는 활성 사용자의 파일이 실수로 삭제되지 않도록 보호.
    """
    now = time.time()
    removed = 0
    for f in SESSION_DIR.glob("*.json"):
        session_id = f.stem
        # 캐시에 있고 최근 접근된 세션은 mtime 무관하게 보존
        cache_entry = _session_cache.get(session_id)
        if cache_entry is not None:
            _, last_access = cache_entry
            if (now - last_access) <= SESSION_TTL_SECONDS:
                continue
        if (now - f.stat().st_mtime) > SESSION_TTL_SECONDS:
            delete_session(session_id)
            removed += 1

    for session_id, (_, last_access) in list(_session_cache.items()):
        if (now - last_access) > SESSION_TTL_SECONDS:
            _purge_runtime_state(session_id)

    if removed:
        logger.info("cleanup_expired_sessions — removed %d sessions", removed)
    return removed
