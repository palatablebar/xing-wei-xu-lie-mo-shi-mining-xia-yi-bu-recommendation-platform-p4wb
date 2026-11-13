"""版本服务：正式版指针（app_state + mine_runs.is_active 双写一致）与版本间差异。

- 候选运行（candidate）阶段跑完前不影响正式版；
- promote 一次性切换指针，旧正式版转历史版，模式/推荐数据按 run_id 隔离不被覆盖；
- rollback 把指针切回任一已完成的历史版，复核意见保留在各自 run 的推荐行上。
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.models.app_state import AppState, ACTIVE_RUN_KEY
from src.models.mine_run import MineRun
from src.models.pattern import Pattern
from src.models.recommendation import Recommendation
from src.utils.errors import AppError
from src.utils.timeutil import iso_utc, utcnow


def _pointer_row(db: Session) -> AppState:
    row = db.scalar(select(AppState).where(AppState.state_key == ACTIVE_RUN_KEY))
    if row is None:
        row = AppState(state_key=ACTIVE_RUN_KEY, state_value=None)
        db.add(row)
        db.flush()
    return row


def active_run(db: Session) -> MineRun | None:
    """正式版运行：优先 is_active 标志，回退到 app_state 指针。"""
    run = db.scalar(select(MineRun).where(MineRun.is_active.is_(True)).order_by(MineRun.id.desc()).limit(1))
    if run is not None:
        return run
    pointer = _pointer_row(db)
    if pointer.state_value:
        return db.get(MineRun, int(pointer.state_value))
    return None


def active_run_id(db: Session) -> int | None:
    run = active_run(db)
    return run.id if run else None


def _set_pointer(db: Session, run_id: int | None) -> None:
    _pointer_row(db).state_value = str(run_id) if run_id is not None else None


def promote(db: Session, run: MineRun) -> MineRun:
    """把候选版一次性切换为正式版；同事务内旧正式版转历史版。"""
    if run.stage != "done":
        raise AppError("CONFLICT", 409, {"reason": "run_not_ready", "stage": run.stage})
    previous = active_run(db)
    if previous is not None and previous.id == run.id:
        return run
    if previous is not None:
        previous.is_active = False
    run.is_active = True
    run.promoted_at = utcnow()
    _set_pointer(db, run.id)
    db.flush()
    return run


def rollback_to(db: Session, target: MineRun) -> MineRun:
    """回滚到指定的已完成历史版；当前正式版转历史版，复核意见不丢失。"""
    if target.stage != "done":
        raise AppError("CONFLICT", 409, {"reason": "run_not_ready", "stage": target.stage})
    current = active_run(db)
    if current is not None and current.id != target.id:
        current.is_active = False
    target.is_active = True
    target.promoted_at = utcnow()
    _set_pointer(db, target.id)
    db.flush()
    return target


def _rec_key(rec: Recommendation) -> tuple[str, tuple[str, ...], str]:
    return rec.user_id, tuple(rec.suffix or []), rec.next_event


def run_diff(db: Session, base_id: int | None, target_id: int) -> dict:
    """两个运行之间的切换前后差异：模式 / 推荐 / 覆盖率 / 复核继承。"""
    target = db.get(MineRun, target_id)
    if target is None:
        raise AppError("NOT_FOUND", 404, {"entity": "mine_run", "id": target_id})
    base = db.get(MineRun, base_id) if base_id else None

    base_patterns = {row.seq_key for row in db.scalars(select(Pattern).where(Pattern.run_id == base_id)).all()} if base else set()
    target_patterns = {row.seq_key for row in db.scalars(select(Pattern).where(Pattern.run_id == target_id)).all()}
    base_recs = (
        {_rec_key(row) for row in db.scalars(select(Recommendation).where(Recommendation.run_id == base_id)).all()}
        if base
        else set()
    )
    target_recs = {_rec_key(row) for row in db.scalars(select(Recommendation).where(Recommendation.run_id == target_id)).all()}

    def block(run: MineRun | None) -> dict | None:
        if run is None:
            return None
        return {
            "id": run.id,
            "run_no": run.run_no,
            "is_active": run.is_active,
            "stage": run.stage,
            "pattern_count": run.pattern_count,
            "rec_count": run.rec_count,
            "coverage_pct": run.coverage_pct,
            "covered_users": run.covered_users,
            "active_users": run.active_users,
            "watermark_at": iso_utc(run.watermark_at),
        }

    return {
        "base": block(base),
        "target": block(target),
        "patterns": {
            "added": len(target_patterns - base_patterns),
            "removed": len(base_patterns - target_patterns),
            "kept": len(target_patterns & base_patterns),
        },
        "recommendations": {
            "added": len(target_recs - base_recs),
            "removed": len(base_recs - target_recs),
            "kept": len(target_recs & base_recs),
        },
        "coverage_delta_pct": round(target.coverage_pct - (base.coverage_pct if base else 0.0), 1),
        "carried_recs": target.carried_recs,
        "reset_recs": target.reset_recs,
    }
