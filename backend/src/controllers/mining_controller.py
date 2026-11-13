"""挖掘运行控制器：发起候选运行（不自动切换）、切换正式版、回滚、版本差异。"""
from __future__ import annotations

from fastapi import Request
from sqlalchemy.orm import Session

from src.services import audit_service, mining_service, version_service


def create_run(db: Session, payload: dict, principal: dict, request: Request) -> dict:
    run = mining_service.create_and_execute(
        db,
        min_support_pct=payload["min_support_pct"],
        max_len=payload["max_len"],
        top_k=payload["top_k"],
        trigger=payload.get("trigger") or "manual",
        operator_email=principal["email"],
    )
    audit_service.record(
        db,
        actor_email=principal["email"],
        actor_role=principal["role"],
        action="mining.create",
        entity="mine_run",
        entity_id=run.id,
        detail={"run_no": run.run_no, "status": run.status, "stage": run.stage, "patterns": run.pattern_count},
        ip=request.client.host if request.client else None,
    )
    audit_service.record(
        db,
        actor_email=principal["email"],
        actor_role=principal["role"],
        action="mining.complete",
        entity="mine_run",
        entity_id=run.id,
        detail={
            "patterns": run.pattern_count,
            "recommendations": run.rec_count,
            "coverage": run.coverage_pct,
            "carried": run.carried_recs,
            "reset": run.reset_recs,
        },
        layer="domain",
        ip=request.client.host if request.client else None,
    )
    db.commit()
    return mining_service.serialize_run(run)


def resume_run(db: Session, run_id: int, principal: dict, request: Request) -> dict:
    run = mining_service.get_run(db, run_id)
    if run.stage == "done":
        return mining_service.serialize_run(run)
    resumed = mining_service.resume_run(db, run)
    audit_service.record(
        db,
        actor_email=principal["email"],
        actor_role=principal["role"],
        action="mining.create",
        entity="mine_run",
        entity_id=resumed.id,
        detail={"run_no": resumed.run_no, "resume": True, "stage": resumed.stage, "status": resumed.status},
        ip=request.client.host if request.client else None,
    )
    db.commit()
    return mining_service.serialize_run(resumed)


def promote_run(db: Session, run_id: int, principal: dict, request: Request) -> dict:
    run = mining_service.get_run(db, run_id)
    before = version_service.active_run(db)
    diff = version_service.run_diff(db, before.id if before else None, run.id)
    version_service.promote(db, run)
    audit_service.record(
        db,
        actor_email=principal["email"],
        actor_role=principal["role"],
        action="mining.promote",
        entity="mine_run",
        entity_id=run.id,
        detail={
            "run_no": run.run_no,
            "previous_run_no": before.run_no if before else None,
            "patterns": diff["patterns"],
            "recommendations": diff["recommendations"],
            "coverage_delta_pct": diff["coverage_delta_pct"],
        },
        ip=request.client.host if request.client else None,
    )
    db.commit()
    return mining_service.serialize_run(run)


def rollback_run(db: Session, run_id: int, principal: dict, request: Request) -> dict:
    target = mining_service.get_run(db, run_id)
    current = version_service.active_run(db)
    version_service.rollback_to(db, target)
    audit_service.record(
        db,
        actor_email=principal["email"],
        actor_role=principal["role"],
        action="mining.rollback",
        entity="mine_run",
        entity_id=target.id,
        detail={
            "target_run_no": target.run_no,
            "from_run_no": current.run_no if current else None,
        },
        ip=request.client.host if request.client else None,
    )
    db.commit()
    return mining_service.serialize_run(target)


def run_diff(db: Session, base_id: int | None, target_id: int) -> dict:
    return version_service.run_diff(db, base_id, target_id)


def list_runs(db: Session, **filters) -> dict:
    return mining_service.list_runs(db, **filters)


def get_run(db: Session, run_id: int) -> dict:
    return mining_service.serialize_run(mining_service.get_run(db, run_id))
