from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from src.config.database import get_db
from src.constants.roles import MINING_CREATE_ROLES, READ_ROLES
from src.controllers import ingest_controller, sequence_controller
from src.middlewares.rbac import require_roles
from src.schemas.ingest import IngestIn
from src.utils.pagination import page_params

router = APIRouter(prefix="/sequences", tags=["sequences"])


@router.get("")
def list_sequences(
    q: str | None = None,
    user_id: str | None = None,
    converted: bool | None = None,
    min_len: int | None = None,
    limit: int = Query(20),
    offset: int = Query(0),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*READ_ROLES)),
) -> dict:
    page = page_params(limit, offset)
    return sequence_controller.list_sequences(
        db,
        principal,
        q=q,
        user_id=user_id,
        converted=converted,
        min_len=min_len,
        limit=page.limit,
        offset=page.offset,
    )


@router.get("/stats")
def stats(db: Session = Depends(get_db), principal: dict = Depends(require_roles(*READ_ROLES))) -> dict:
    return sequence_controller.stats(db)


@router.get("/watermark")
def watermark(db: Session = Depends(get_db), principal: dict = Depends(require_roles(*READ_ROLES))) -> dict:
    return ingest_controller.watermark(db)


@router.post("/ingest", status_code=202)
def ingest(
    payload: IngestIn,
    request: Request,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*MINING_CREATE_ROLES)),
) -> dict:
    return ingest_controller.ingest(db, payload.model_dump(), principal, request)


@router.get("/{sequence_id}")
def get_sequence(
    sequence_id: int, db: Session = Depends(get_db), principal: dict = Depends(require_roles(*READ_ROLES))
) -> dict:
    return sequence_controller.get_sequence(db, sequence_id, principal)
