"""已并入事件台账：event_id 唯一，作为采集幂等屏障。

同批重跑 / 采集端重复投递时，event_id 已在此表即跳过，保证不新增、不重复；
迟到事件凭 event_id 去重后再按事件时间并入对应会话。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base
from src.utils.timeutil import utcnow


class IngestedEvent(Base):
    __tablename__ = "ingested_events"

    event_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    source: Mapped[str] = mapped_column(String(64), index=True)
    session_id: Mapped[str] = mapped_column(String(40), index=True)
    event_code: Mapped[str] = mapped_column(String(40))
    event_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
