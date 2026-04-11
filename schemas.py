# schemas.py
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


VoteChoice = Literal["1", "2", "draw"]


class SettingsModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    elo_k_max: int = Field(default=100, ge=1, le=10_000)
    elo_k_min: int = Field(default=30, ge=1, le=10_000)
    elo_decay_factor: int = Field(default=50, ge=1, le=100_000)
    elo_draw_max: float = Field(default=0.33, ge=0.0, le=1.0)
    elo_draw_scale: float = Field(default=300.0, gt=0.0, le=10_000.0)
    initial_rating: float = Field(default=1200.0, ge=0.0, le=100_000.0)
    normalize_target: float = Field(default=1200.0, ge=0.0, le=100_000.0)
    normalize_threshold: float = Field(default=1.0, ge=0.0, le=100_000.0)
    result_auto_skip: bool = False
    result_skip_seconds: float = Field(default=3.0, ge=0.5, le=60.0)

    @model_validator(mode="after")
    def validate_bounds(self) -> "SettingsModel":
        if self.elo_k_min > self.elo_k_max:
            raise ValueError("elo_k_min은 elo_k_max보다 클 수 없습니다.")
        return self


class CriterionModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, pattern=r"^[a-z0-9_]+$")
    label: str = Field(min_length=1)
    color: str = Field(min_length=1)
    weight: float = Field(default=1.0, gt=0.0)
    battles: int = Field(default=0, ge=0)
    draws: int = Field(default=0, ge=0)

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
    model_config = ConfigDict(extra="forbid")

    id: int = Field(ge=1)
    name: str = Field(min_length=1)
    ratings: dict[str, float] = Field(default_factory=dict)
    matches_played: int = Field(default=0, ge=0)

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("item name은 비어 있을 수 없습니다.")
        return stripped


class ActiveRoundModel(BaseModel):
    """진행 중인 배틀 라운드 — 파일에 영속화하여 VM 재시작 후에도 투표 가능."""

    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=16, max_length=255)
    item1_id: int = Field(ge=1)
    item2_id: int = Field(ge=1)
    issued_at: float = Field(ge=0.0)

    @model_validator(mode="after")
    def validate_distinct_items(self) -> "ActiveRoundModel":
        if self.item1_id == self.item2_id:
            raise ValueError("active_round.item1_id와 item2_id는 달라야 합니다.")
        return self


class SessionDataModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    settings: SettingsModel = Field(default_factory=SettingsModel)
    criteria: list[CriterionModel] = Field(default_factory=_default_criteria)
    items: list[ItemModel] = Field(default_factory=list, max_length=10_000)
    active_round: ActiveRoundModel | None = None

    @model_validator(mode="after")
    def validate_consistency(self) -> "SessionDataModel":
        criterion_keys = [criterion.key for criterion in self.criteria]
        if len(set(criterion_keys)) != len(criterion_keys):
            raise ValueError("criteria.key는 중복될 수 없습니다.")

        allowed_rating_keys = set(criterion_keys)
        item_ids: set[int] = set()

        for item in self.items:
            if item.id in item_ids:
                raise ValueError(f"중복된 item id가 있습니다: {item.id}")
            item_ids.add(item.id)

            rating_keys = set(item.ratings)
            missing_keys = allowed_rating_keys - rating_keys
            unknown_keys = rating_keys - allowed_rating_keys
            if missing_keys:
                raise ValueError(
                    f"item {item.id}에 누락된 rating key가 있습니다: {sorted(missing_keys)}"
                )
            if unknown_keys:
                raise ValueError(
                    f"item {item.id}에 정의되지 않은 rating key가 있습니다: {sorted(unknown_keys)}"
                )

        return self


class BattleVoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item1_id: int = Field(ge=1)
    item2_id: int = Field(ge=1)
    round_token: str = Field(min_length=16, max_length=255)
    votes: dict[str, VoteChoice] = Field(min_length=1)
    redirect_to: str | None = None

    @field_validator("redirect_to")
    @classmethod
    def validate_redirect_to(cls, value: str | None) -> str | None:
        if value in (None, ""):
            return None
        if value.startswith("/") and not value.startswith("//"):
            return value
        raise ValueError("redirect_to는 안전한 상대 경로여야 합니다.")

    @model_validator(mode="after")
    def validate_item_pair(self) -> "BattleVoteRequest":
        if self.item1_id == self.item2_id:
            raise ValueError("같은 항목끼리는 대결할 수 없습니다.")
        return self


class CriteriaResult(BaseModel):
    """개별 기준의 Elo 변동 결과"""

    key: str
    label: str
    color: str
    winner: VoteChoice
    old_r1: int
    new_r1: int
    diff_r1: int
    old_r2: int
    new_r2: int
    diff_r2: int


class BattleVoteResponse(BaseModel):
    """전체 배틀 투표 응답 — 모든 criteria 결과를 한번에 반환"""

    a1_id: int
    a2_id: int
    a1_name: str
    a2_name: str
    results: list[CriteriaResult]
    total_items: int
    next_url: str
