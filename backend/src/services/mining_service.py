"""挖掘服务：可恢复的分阶段重挖 → 模式库 → 下一步推荐 → 覆盖率，全部完成后才允许切换。

版本与可恢复口径：
- 每次重挖产出一个候选运行（candidate, is_active=False），与正式版按 run_id 隔离；
- 四个阶段 patterns → recommend → coverage → done，每阶段成功即落库；
  中断/失败后同批重跑（同 seed_key 或对同一未完成 run 重跑）从断点续跑，不新增、不重复；
- 推荐复核意见继承：同 (user_id, suffix, next_event) 沿用正式版结论（same），
  口径任一变化回待复核（changed），全新推荐为 new；
- 仅 stage=done 的运行才能 promote 为正式版；失败不影响正式版，可回滚。
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.constants.enums import REVIEW_STATUS_LABELS
from src.models.event_def import EventDef
from src.models.event_seq import EventSeq
from src.models.ingest_cursor import IngestCursor
from src.models.mine_run import MineRun
from src.models.pattern import Pattern
from src.models.recommendation import Recommendation
from src.services import recommender, sequence_service, version_service
from src.services.prefixspan import MinedPattern, mine_patterns, min_support_count
from src.utils.errors import AppError
from src.utils.timeutil import iso_utc, local_now, utcnow

PATTERN_MIN_LEN = 2
DEFAULT_TOP_K = 50


def serialize_run(run: MineRun) -> dict:
    return {
        "id": run.id,
        "run_no": run.run_no,
        "min_support_pct": run.min_support_pct,
        "max_len": run.max_len,
        "top_k": run.top_k,
        "trigger": run.trigger,
        "status": run.status,
        "stage": run.stage,
        "is_active": run.is_active,
        "edition": "active" if run.is_active else ("archived" if run.promoted_at else "candidate"),
        "operator_email": run.operator_email,
        "sequence_count": run.sequence_count,
        "pattern_count": run.pattern_count,
        "rec_count": run.rec_count,
        "covered_users": run.covered_users,
        "active_users": run.active_users,
        "coverage_pct": run.coverage_pct,
        "carried_recs": run.carried_recs,
        "reset_recs": run.reset_recs,
        "watermark_at": iso_utc(run.watermark_at),
        "ingested_events": run.ingested_events,
        "promoted_at": iso_utc(run.promoted_at),
        "metrics": run.metrics or {},
        "log_text": run.log_text,
        "started_at": iso_utc(run.started_at),
        "finished_at": iso_utc(run.finished_at),
    }


def next_run_no(db: Session) -> str:
    prefix = f"M{local_now():%Y%m%d}"
    count = db.scalar(select(func.count()).select_from(MineRun).where(MineRun.run_no.like(f"{prefix}%"))) or 0
    return f"{prefix}-{count + 1:04d}"


def get_run(db: Session, run_id: int) -> MineRun:
    row = db.get(MineRun, run_id)
    if row is None:
        raise AppError("NOT_FOUND", 404, {"entity": "mine_run", "id": run_id})
    return row


def latest_run(db: Session) -> MineRun | None:
    return db.scalar(select(MineRun).order_by(MineRun.id.desc()).limit(1))


def list_runs(db: Session, status: str | None, limit: int, offset: int) -> dict:
    stmt = select(MineRun)
    count_stmt = select(func.count()).select_from(MineRun)
    if status:
        stmt = stmt.where(MineRun.status == status)
        count_stmt = count_stmt.where(MineRun.status == status)
    total = db.scalar(count_stmt) or 0
    rows = db.scalars(stmt.order_by(MineRun.id.desc()).limit(limit).offset(offset)).all()
    return {"items": [serialize_run(row) for row in rows], "total": total, "limit": limit, "offset": offset}


def _watermark_snapshot(db: Session) -> tuple:
    """本批水位：所有采集源中最大已并入事件时间 + 已并入事件总数。"""
    rows = db.scalars(select(IngestCursor)).all()
    watermark_at = max((row.last_event_at for row in rows if row.last_event_at), default=None)
    ingested = sum(row.ingested_events for row in rows)
    return watermark_at, ingested


def _persist_patterns(db: Session, run: MineRun, mined: list[MinedPattern], seq_count: int) -> dict[tuple[str, ...], Pattern]:
    stored: dict[tuple[str, ...], Pattern] = {}
    for rank, item in enumerate(mined, start=1):
        data = item.as_dict(seq_count)
        row = db.scalar(select(Pattern).where(Pattern.run_id == run.id, Pattern.seq_key == data["seq_key"]))
        if row is None:
            row = Pattern(run_id=run.id, seq_key=data["seq_key"])
            db.add(row)
        row.items = data["items"]
        row.length = data["length"]
        row.support_count = data["support_count"]
        row.support_pct = data["support_pct"]
        row.confidence = data["confidence"]
        row.lift = data["lift"]
        row.rank = rank
        db.flush()
        stored[tuple(data["items"])] = row
    return stored


def _active_review_index(db: Session, active_id: int | None) -> dict[tuple, Recommendation]:
    """正式版推荐按 (user_id, suffix, next_event) 建索引，用于复核意见继承。"""
    if not active_id:
        return {}
    rows = db.scalars(select(Recommendation).where(Recommendation.run_id == active_id)).all()
    return {(row.user_id, tuple(row.suffix or []), row.next_event): row for row in rows}


def _persist_recommendations(
    db: Session, run: MineRun, result: recommender.RecommendResult, review_index: dict[tuple, Recommendation]
) -> tuple[int, int, int]:
    by_pattern = {row.id: row for row in db.scalars(select(Pattern).where(Pattern.run_id == run.id)).all()}
    carried = reset = 0
    for item in result.recommendations:
        pattern = by_pattern.get(item.pattern_id)
        if pattern is None:
            continue
        key = (item.user_id, tuple(item.suffix), item.next_event)
        exists = db.scalar(
            select(Recommendation).where(
                Recommendation.run_id == run.id,
                Recommendation.user_id == item.user_id,
                Recommendation.pattern_id == item.pattern_id,
                Recommendation.next_event == item.next_event,
            )
        )
        if exists is None:
            exists = Recommendation(
                run_id=run.id,
                pattern_id=item.pattern_id,
                user_id=item.user_id,
                suffix=[],
                next_event=item.next_event,
            )
            db.add(exists)
        exists.suffix = list(item.suffix)
        exists.next_module = item.module
        exists.confidence = item.confidence
        exists.lift = item.lift
        exists.score = item.score
        exists.priority = item.priority
        prior = review_index.get(key)
        if prior is not None and prior.status != "pending":
            # 同用户 + 同后缀 + 同下一步：沿用原复核结论与备注，不回待复核
            exists.status = prior.status
            exists.reviewer = prior.reviewer
            exists.review_note = prior.review_note
            exists.carry_state = "same"
            carried += 1
        else:
            # 口径变化（后缀/下一步不同）或正式版本就待复核：回待复核
            exists.status = "pending"
            exists.reviewer = None
            exists.review_note = None
            exists.carry_state = "changed" if prior is not None else "new"
            if prior is not None:
                reset += 1
        db.flush()
    rec_count = (
        db.scalar(select(func.count()).select_from(Recommendation).where(Recommendation.run_id == run.id)) or 0
    )
    return rec_count, carried, reset


def _stage_patterns(db: Session, run: MineRun) -> None:
    rows = db.scalars(select(EventSeq).order_by(EventSeq.started_at, EventSeq.id)).all()
    sequences = [list(row.events or []) for row in rows]
    run.sequence_count = len(sequences)
    mined = mine_patterns(sequences, run.min_support_pct, run.max_len, run.top_k, min_len=PATTERN_MIN_LEN)
    stored = _persist_patterns(db, run, mined, len(sequences))
    support_top = max((item.support_count for item in mined), default=0)
    module_of = {row.code: row.module for row in db.scalars(select(EventDef)).all()}
    run.pattern_count = len(stored)
    run.metrics = {
        **(run.metrics or {}),
        "min_support_count": min_support_count(len(sequences), run.min_support_pct),
        "frequent_total": len(mined),
        "top_support_count": support_top,
        "top_support_pct": round(support_top / len(sequences) * 100, 1) if sequences else 0.0,
        "top_pattern": mined[0].seq_key if mined else None,
        "avg_sequence_len": round(sum(len(s) for s in sequences) / len(sequences), 2) if sequences else 0.0,
        "modules_total": len({module for module in module_of.values()}),
        "convert_base_pct": round(
            (db.scalar(select(func.count()).select_from(EventSeq).where(EventSeq.converted.is_(True))) or 0)
            / len(sequences) * 100,
            1,
        )
        if sequences
        else 0.0,
    }
    run.stage = "patterns"
    db.flush()


def _stage_recommend(db: Session, run: MineRun) -> None:
    stored = {
        tuple(row.items): row
        for row in db.scalars(select(Pattern).where(Pattern.run_id == run.id)).all()
    }
    module_of = {row.code: row.module for row in db.scalars(select(EventDef)).all()}
    suffixes = sequence_service.latest_suffixes(db)
    refs = [
        recommender.PatternRef(pattern_id=row.id, items=tuple(row.items), confidence=row.confidence, lift=row.lift)
        for row in stored.values()
    ]
    result = recommender.recommend(suffixes, refs, module_of)
    active = version_service.active_run(db)
    review_index = _active_review_index(db, active.id if active and active.id != run.id else None)
    rec_count, carried, reset = _persist_recommendations(db, run, result, review_index)
    run.rec_count = rec_count
    run.carried_recs = carried
    run.reset_recs = reset
    run.active_users = result.active_users
    run.stage = "recommend"
    db.flush()


def _stage_coverage(db: Session, run: MineRun) -> None:
    # 覆盖率由推荐结果直接统计，独立成阶段以便断点续跑
    covered = (
        db.scalar(
            select(func.count(func.distinct(Recommendation.user_id))).where(Recommendation.run_id == run.id)
        )
        or 0
    )
    run.covered_users = covered
    run.coverage_pct = round(covered / run.active_users * 100, 1) if run.active_users else 0.0
    run.stage = "coverage"
    db.flush()


def _finalize(run: MineRun) -> None:
    run.stage = "done"
    run.status = "success" if run.pattern_count else "partial"
    run.finished_at = utcnow()
    run.log_text = (
        f"本批水位 {iso_utc(run.watermark_at) or '—'}（已并入事件 {run.ingested_events}）；"
        f"读取 {run.sequence_count} 条会话（平均 {(run.metrics or {}).get('avg_sequence_len', 0)} 事件）；"
        f"最小支持度 {run.min_support_pct}% => {(run.metrics or {}).get('min_support_count', 0)} 条会话；"
        f"模式库 {run.pattern_count} 条（top_k={run.top_k}，长度≥{PATTERN_MIN_LEN}）；"
        f"推荐 {run.rec_count} 条（沿用复核 {run.carried_recs}、变化回待复核 {run.reset_recs}），"
        f"覆盖 {run.covered_users}/{run.active_users}（{run.coverage_pct}%）。候选版待切换正式版。"
    )


def execute_run(db: Session, run: MineRun) -> MineRun:
    """推进一个阶段（每阶段独立事务，断点可恢复）。

    阶段成功 → flush，由外层提交；阶段抛错 → 回滚本阶段，另起事务把运行
    标记 failed 并停在进入时的阶段（last_good_stage），正式版与已完成阶段不受影响。
    """
    if run.stage == "done":
        return run
    run.status = "running"
    actions = {
        "queued": _stage_patterns,
        "patterns": _stage_recommend,
        "recommend": _stage_coverage,
        "coverage": _finalize,
    }
    action = actions.get(run.stage)
    if action is None:
        return run
    entry_stage = run.stage
    run.metrics = {**(run.metrics or {}), "last_good_stage": entry_stage}
    try:
        action(db, run) if action is not _finalize else action(run)
        db.flush()
    except Exception as exc:  # noqa: BLE001 - 单阶段失败不影响其它版本，留痕后可续跑
        db.rollback()
        _mark_failed(run.id, entry_stage, exc)
        return db.get(MineRun, run.id)
    return run


def _mark_failed(run_id: int, last_good_stage: str, exc: Exception) -> None:
    """用独立事务记录失败断点，确保失败状态本身持久化。"""
    from src.config.database import session_scope

    with session_scope() as fail_db:
        failed = fail_db.get(MineRun, run_id)
        if failed is None or failed.stage == "done":
            return
        failed.status = "failed"
        failed.stage = last_good_stage
        failed.finished_at = utcnow()
        failed.log_text = f"挖掘失败（断点 {last_good_stage}）：{exc}"
        failed.metrics = {**(failed.metrics or {}), "error": str(exc)[:300]}


def run_to_done(db: Session, run: MineRun) -> MineRun:
    """在多个事务里把候选运行一路推进到 done（或失败），每阶段提交一次。"""
    guard = 0
    while run.stage not in ("done",) and run.status != "failed" and guard < 10:
        run = execute_run(db, run)
        db.commit()
        run = db.get(MineRun, run.id)
        guard += 1
    return run


def resume_run(db: Session, run: MineRun) -> MineRun:
    """对未完成/失败的同一运行从断点续跑，不新建运行、不重复落库。"""
    if run.stage == "done":
        return run
    run.status = "running"
    db.flush()
    db.commit()
    return run_to_done(db, run)


def create_and_execute(
    db: Session,
    *,
    min_support_pct: float,
    max_len: int,
    top_k: int,
    trigger: str,
    operator_email: str,
    seed_key: str | None = None,
) -> MineRun:
    # 同批重跑：seed_key 命中且未完成 → 续跑；已完成 → 原样返回，不新增
    if seed_key:
        existing = db.scalar(select(MineRun).where(MineRun.seed_key == seed_key))
        if existing is not None:
            if existing.stage != "done":
                return resume_run(db, existing)
            return existing
    # 本批水位在运行创建时固定：续跑/重放都以创建时刻的采集水位为准
    watermark_at, ingested = _watermark_snapshot(db)
    run = MineRun(
        run_no=next_run_no(db),
        min_support_pct=float(min_support_pct),
        max_len=int(max_len),
        top_k=int(top_k),
        trigger=trigger,
        operator_email=operator_email,
        status="running",
        stage="queued",
        is_active=False,
        seed_key=seed_key,
        started_at=utcnow(),
        watermark_at=watermark_at,
        ingested_events=ingested,
    )
    db.add(run)
    db.flush()
    db.commit()
    return run_to_done(db, run)


def review_stats(db: Session, run_id: int | None = None) -> list[dict]:
    stmt = select(Recommendation.status, func.count()).group_by(Recommendation.status)
    if run_id:
        stmt = stmt.where(Recommendation.run_id == run_id)
    rows = db.execute(stmt).all()
    return [
        {"key": key, "label": REVIEW_STATUS_LABELS.get(key, key), "count": count}
        for key, count in sorted(rows, key=lambda item: item[0])
    ]
