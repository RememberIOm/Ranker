# schemas.py
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


MAX_BACKUP_BYTES = 64 * 1024 * 1024


VoteChoice = Literal["1", "2", "draw", "skip"]
ThreeWayRole = Literal["best", "worst", "tied"]


def _safe_relative_path(value: str | None) -> str | None:
    """오픈 리다이렉트 방지 — 상대 경로만 허용합니다."""
    if value in (None, ""):
        return None
    if (
        value.startswith("/")
        and not value.startswith("//")
        and "\\" not in value
        and not any(ord(c) < 32 for c in value)
    ):
        return value
    raise ValueError("redirect_to는 안전한 상대 경로여야 합니다.")


SafeRedirect = Annotated[str | None, AfterValidator(_safe_relative_path)]


class SettingsModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    initial_sigma: float = Field(default=2.0, ge=0.1, le=10.0)
    display_center: float = Field(default=1200.0, ge=0.0, le=100_000.0)
    display_scale: float = Field(default=173.72, gt=0.0, le=10_000.0)
    battle_mode: Literal["2way", "3way"] = "2way"
    blind_mode: bool = True
    result_auto_skip: bool = False
    result_skip_seconds: float = Field(default=3.0, ge=0.5, le=60.0)


class CriterionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    key: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9_]+$")
    label: str = Field(min_length=1, max_length=200)
    color: Literal[
        "blue",
        "purple",
        "pink",
        "green",
        "indigo",
        "red",
        "yellow",
        "orange",
        "teal",
        "cyan",
        "gray",
    ] = "gray"
    weight: float = Field(default=1.0, gt=0.0, le=1_000_000)
    battles: int = Field(default=0, ge=0, le=2**63 - 1)
    draws: int = Field(default=0, ge=0, le=2**63 - 1)

    @model_validator(mode="after")
    def validate_counters(self) -> "CriterionModel":
        if self.draws > self.battles:
            raise ValueError("무승부 수는 비교 수를 넘을 수 없습니다.")
        return self

    @field_validator("key", "label", "color")
    @classmethod
    def strip_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("빈 문자열은 허용되지 않습니다.")
        return stripped


def _default_criteria() -> list[CriterionModel]:
    return [
        CriterionModel(key="story", label="스토리", color="blue", weight=1.2),
        CriterionModel(key="visual", label="작화", color="purple", weight=1.0),
        CriterionModel(key="ost", label="OST", color="pink", weight=0.8),
        CriterionModel(key="voice", label="성우", color="green", weight=0.8),
        CriterionModel(key="char", label="캐릭터", color="indigo", weight=1.0),
        CriterionModel(key="fun", label="재미", color="red", weight=1.2),
    ]


class ItemModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    id: int = Field(ge=1, le=2**63 - 1)
    name: str = Field(min_length=1, max_length=500)
    mu: dict[str, Annotated[float, Field(ge=-1_000_000, le=1_000_000)]] = Field(
        default_factory=dict
    )
    sigma_sq: dict[str, Annotated[float, Field(gt=0.0, le=1_000_000)]] = Field(
        default_factory=dict
    )
    matches_played: int = Field(default=0, ge=0, le=2**63 - 1)
    criterion_matches: dict[str, Annotated[int, Field(ge=0, le=2**63 - 1)]] = Field(
        default_factory=dict
    )

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("item name은 비어 있을 수 없습니다.")
        return stripped


class ActiveRoundModel(BaseModel):
    """진행 중인 배틀 라운드 — DB에 영속화하여 VM 재시작 후에도 투표 가능."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    token: str = Field(min_length=16, max_length=255)
    item1_id: int = Field(ge=1, le=2**63 - 1)
    item2_id: int = Field(ge=1, le=2**63 - 1)
    item3_id: int | None = Field(default=None, ge=1)
    issued_at: float = Field(ge=0.0)

    @model_validator(mode="after")
    def validate_distinct_items(self) -> "ActiveRoundModel":
        ids = [self.item1_id, self.item2_id]
        if self.item3_id is not None:
            ids.append(self.item3_id)
        if len(set(ids)) != len(ids):
            raise ValueError("active_round의 항목 ID는 모두 달라야 합니다.")
        return self


class ObservationModel(BaseModel):
    """An unordered count of one complete ranking response, including tied groups."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    groups: tuple[tuple[int, ...], ...]
    count: int = Field(ge=1, le=2**63 - 1)

    @field_validator("groups")
    @classmethod
    def validate_groups(cls, groups):
        from ranker.rating_engine import canonical_ranking

        return canonical_ranking(groups)


class PosteriorModel(BaseModel):
    """Database cache, rebuilt from observations on backup import."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    algorithm_version: Literal["gpl-map-v2"]
    item_ids: list[int]
    location: list[float]
    prior_variance: float = Field(ge=0.01, le=100)
    rows: list[int]
    cols: list[int]
    values: list[float]

    @model_validator(mode="after")
    def validate_shape(self):
        n = len(self.item_ids) + 2
        if (
            len(self.location) != n
            or len(set(self.item_ids)) != n - 2
            or not len(self.rows) == len(self.cols) == len(self.values)
            or any(i < 0 or i >= n for i in self.rows + self.cols)
        ):
            raise ValueError("평점 모형의 차원이 올바르지 않습니다.")
        return self


class SessionDataModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    settings: SettingsModel = Field(default_factory=SettingsModel)
    criteria: list[CriterionModel] = Field(
        default_factory=_default_criteria, max_length=64
    )
    items: list[ItemModel] = Field(default_factory=list, max_length=10_000)
    active_round: ActiveRoundModel | None = None
    observations: dict[str, list[ObservationModel]] = Field(default_factory=dict)
    exposures: dict[str, Annotated[int, Field(ge=0, le=2**63 - 1)]] = Field(
        default_factory=dict
    )
    posteriors: dict[str, PosteriorModel] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_consistency(self) -> "SessionDataModel":
        criterion_keys = [criterion.key for criterion in self.criteria]
        if len(set(criterion_keys)) != len(criterion_keys):
            raise ValueError("criteria.key는 중복될 수 없습니다.")

        allowed_keys = set(criterion_keys)
        for mapping in (self.observations, self.exposures, self.posteriors):
            if set(mapping) - allowed_keys:
                raise ValueError("모형 데이터에 없는 평가 기준이 포함되어 있습니다.")
        for key, observations in self.observations.items():
            groups = [observation.groups for observation in observations]
            if len(set(groups)) != len(groups):
                raise ValueError("같은 순위 응답은 하나의 관측 수로 합쳐야 합니다.")
            if sum(o.count for o in observations) > self.exposures.get(key, 0):
                raise ValueError("응답 수는 대결 노출 수를 넘을 수 없습니다.")
        item_ids: set[int] = set()

        for item in self.items:
            if item.id in item_ids:
                raise ValueError(f"중복된 item id가 있습니다: {item.id}")
            item_ids.add(item.id)

            for field_name in ("mu", "sigma_sq", "criterion_matches"):
                keys = set(getattr(item, field_name))
                missing = allowed_keys - keys
                unknown = keys - allowed_keys
                if missing:
                    raise ValueError(
                        f"item {item.id}에 누락된 {field_name} key가 있습니다: {sorted(missing)}"
                    )
                if unknown:
                    raise ValueError(
                        f"item {item.id}에 정의되지 않은 {field_name} key가 있습니다: {sorted(unknown)}"
                    )

        return self


class BattleVoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    item1_id: int = Field(ge=1, le=2**63 - 1)
    item2_id: int = Field(ge=1, le=2**63 - 1)
    round_token: str = Field(min_length=16, max_length=255)
    votes: dict[str, VoteChoice] = Field(min_length=1)
    redirect_to: SafeRedirect = None

    @model_validator(mode="after")
    def validate_item_pair(self) -> "BattleVoteRequest":
        if self.item1_id == self.item2_id:
            raise ValueError("같은 항목끼리는 대결할 수 없습니다.")
        return self


class CriteriaResult(BaseModel):
    """개별 기준의 정적 순위 모형 재적합 결과"""

    key: str
    label: str
    color: str
    winner: VoteChoice
    old_r1: float
    new_r1: float
    diff_r1: float
    old_r2: float
    new_r2: float
    diff_r2: float
    sigma1: float
    sigma2: float


class SkippedCriteriaResult(BaseModel):
    """건너뛴 기준에는 점수 변화가 없습니다."""

    key: str
    label: str
    color: str
    skipped: Literal[True] = True
    winner: Literal["skip"] | None = None


class BattleVoteResponse(BaseModel):
    """전체 배틀 투표 응답 — 모든 criteria 결과를 한번에 반환"""

    a1_id: int
    a2_id: int
    a1_name: str
    a2_name: str
    results: list[CriteriaResult | SkippedCriteriaResult]
    total_items: int
    next_url: str


# --- 3-way Battle ---


class ThreeWayBattleVoteRequest(BaseModel):
    """3-way 배틀 투표 요청 — 기준별 best/worst 선택."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    item1_id: int = Field(ge=1, le=2**63 - 1)
    item2_id: int = Field(ge=1, le=2**63 - 1)
    item3_id: int = Field(ge=1, le=2**63 - 1)
    round_token: str = Field(min_length=16, max_length=255)
    votes: dict[str, dict[str, ThreeWayRole] | Literal["skip"]] = Field(min_length=1)
    redirect_to: SafeRedirect = None

    @model_validator(mode="after")
    def validate_item_triple(self) -> "ThreeWayBattleVoteRequest":
        ids = {self.item1_id, self.item2_id, self.item3_id}
        if len(ids) != 3:
            raise ValueError("3-way 대결에는 서로 다른 3개 항목이 필요합니다.")
        return self


class ThreeWayCriteriaResult(BaseModel):
    """3-way 개별 기준 결과"""

    key: str
    label: str
    color: str
    best_id: int | None = None
    worst_id: int | None = None
    middle_id: int | None = None
    ratings: dict[str, float]  # {item_id_str: new_display_rating}
    diffs: dict[str, float]  # {item_id_str: rating_change}
    sigmas: dict[str, float]  # {item_id_str: display_uncertainty}


class ThreeWayBattleVoteResponse(BaseModel):
    """3-way 배틀 투표 응답"""

    a1_id: int
    a2_id: int
    a3_id: int
    a1_name: str
    a2_name: str
    a3_name: str
    results: list[ThreeWayCriteriaResult | SkippedCriteriaResult]
    total_items: int
    next_url: str
