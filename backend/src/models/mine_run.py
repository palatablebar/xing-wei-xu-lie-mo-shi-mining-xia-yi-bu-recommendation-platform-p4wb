"""一次序列模式挖掘运行（含推荐生成）。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base
from src.utils.timeutil import utcnow


class MineRun(Base):
    __tablename__ = "mine_runs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    run_no: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    min_support_pct: Mapped[float] = mapped_column(Float, default=3.0)
    max_len: Mapped[int] = mapped_column(Integer, default=5)
    top_k: Mapped[int] = mapped_column(Integer, default=50)
    trigger: Mapped[str] = mapped_column(String(20), default="manual")
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    # 版本编排：candidate 候选（暂不对外）→ active 正式版（切换后）→ archived 归档；失败保持 failed
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    stage: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    # 本批采集水位快照：挖掘时已确认并入的最大事件时间 / 已并入事件总数
    watermark_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    ingested_events: Mapped[int] = mapped_column(Integer, default=0)
    # 复核意见继承统计（来自上一正式版）
    carried_recs: Mapped[int] = mapped_column(Integer, default=0)
    reset_recs: Mapped[int] = mapped_column(Integer, default=0)
    promoted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    operator_email: Mapped[str] = mapped_column(String(160), default="system")
    sequence_count: Mapped[int] = mapped_column(Integer, default=0)
    pattern_count: Mapped[int] = mapped_column(Integer, default=0)
    rec_count: Mapped[int] = mapped_column(Integer, default=0)
    covered_users: Mapped[int] = mapped_column(Integer, default=0)
    active_users: Mapped[int] = mapped_column(Integer, default=0)
    coverage_pct: Mapped[float] = mapped_column(Float, default=0.0)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    log_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    seed_key: Mapped[str | None] = mapped_column(String(120), unique=True, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
