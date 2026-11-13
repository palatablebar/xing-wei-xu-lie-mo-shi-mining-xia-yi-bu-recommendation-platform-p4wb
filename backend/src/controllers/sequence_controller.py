"""会话序列控制器：只读脱敏（viewer 看脱敏 ID）。"""
from __future__ import annotations

from sqlalchemy.orm import Session

from src.services import sequence_service


def _masked(principal: dict) -> bool:
    return principal["role"] == "viewer"


def list_sequences(db: Session, principal: dict, **filters) -> dict:
    return sequence_service.list_sequences(db, masked=_masked(principal), **filters)


def get_sequence(db: Session, sequence_id: int, principal: dict) -> dict:
    return sequence_service.get_sequence(db, sequence_id, masked=_masked(principal))


def stats(db: Session) -> dict:
    return sequence_service.sequence_stats(db)
