from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from src.config.database import get_db
from src.constants.roles import MINING_CREATE_ROLES
from src.controllers import mining_controller
from src.middlewares.rbac import READ_ROLES, require_roles
from src.schemas.mine_run import MineRunIn
from src.utils.pagination import page_params

router = APIRouter(prefix="/mine-runs", tags=["mine-runs"])


@router.get("")
def list_runs(
    status: str | None = None,
    limit: int = Query(20),
    offset: int = Query(0),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*READ_ROLES)),
) -> dict:
    page = page_params(limit, offset)
    return mining_controller.list_runs(db, status=status, limit=page.limit, offset=page.offset)


@router.get("/active")
def active_run(db: Session = Depends(get_db), principal: dict = Depends(require_roles(*READ_ROLES))) -> dict:
    from src.services import mining_service, version_service

    run = version_service.active_run(db)
    return {"item": mining_service.serialize_run(run) if run else None}


@router.get("/{run_id}/diff")
def run_diff(
    run_id: int,
    base_id: int | None = None,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*READ_ROLES)),
) -> dict:
    from src.services import version_service

    if base_id is None:
        active = version_service.active_run(db)
        base_id = active.id if active else None
    return mining_controller.run_diff(db, base_id, run_id)


@router.get("/{run_id}")
def get_run(run_id: int, db: Session = Depends(get_db), principal: dict = Depends(require_roles(*READ_ROLES))) -> dict:
    return mining_controller.get_run(db, run_id)

@router.post("", status_code=201)
def create_run(
    payload: MineRunIn,
    request: Request,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*MINING_CREATE_ROLES)),
) -> dict:
    return mining_controller.create_run(db, payload.model_dump(), principal, request)


@router.post("/{run_id}/resume")
def resume_run(
    run_id: int,
    request: Request,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*MINING_CREATE_ROLES)),
) -> dict:
    return mining_controller.resume_run(db, run_id, principal, request)


@router.post("/{run_id}/promote")
def promote_run(
    run_id: int,
    request: Request,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*MINING_CREATE_ROLES)),
) -> dict:
    return mining_controller.promote_run(db, run_id, principal, request)


@router.post("/{run_id}/rollback")
def rollback_run(
    run_id: int,
    request: Request,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*MINING_CREATE_ROLES)),
) -> dict:
    return mining_controller.rollback_run(db, run_id, principal, request)
