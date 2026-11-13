"""采集控制器：事件幂等并入与水位查询。"""
from __future__ import annotations

from fastapi import Request
from sqlalchemy.orm import Session

from src.services import audit_service, ingest_service


def ingest(db: Session, payload: dict, principal: dict, request: Request) -> dict:
    source = payload.get("source") or ingest_service.DEFAULT_SOURCE
    stats = ingest_service.ingest_events(db, payload.get("events") or [], source=source)
    audit_service.record(
        db,
        actor_email=principal["email"],
        actor_role=principal["role"],
        action="sequence.ingest",
        entity="event_seq",
        entity_id=source,
        detail=stats,
        ip=request.client.host if request.client else None,
    )
    db.commit()
    return {"source": source, **stats, "watermark": ingest_service.watermark(db)}


def watermark(db: Session) -> dict:
    return ingest_service.watermark(db)
