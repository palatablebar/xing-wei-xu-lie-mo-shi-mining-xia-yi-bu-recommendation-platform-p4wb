"""采集水位：按 source 记录「已确认并入」到哪一条事件，支持同批重放去重与迟到事件并入。

- last_event_id：最近一条已并入事件的事件 ID（幂等水位，重放不新增）；
- last_event_at：该事件的发生时间，即本批已并入的时间水位；
- 同一会话的迟到事件按事件时间并入原会话，不产生重复会话。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base
from src.utils.timeutil import utcnow


class IngestCursor(Base):
    __tablename__ = "ingest_cursors"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    last_event_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    last_event_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    ingested_events: Mapped[int] = mapped_column(Integer, default=0)
    duplicate_events: Mapped[int] = mapped_column(Integer, default=0)
    late_events: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
