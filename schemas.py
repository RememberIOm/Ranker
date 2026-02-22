# schemas.py
from pydantic import BaseModel


class CriteriaResult(BaseModel):
    """개별 기준의 Elo 변동 결과"""
    key: str
    label: str
    color: str
    winner: str          # "1" | "2" | "draw"
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
