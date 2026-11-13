"""应用级单行状态表（key/value）：当前正式版（active）挖掘运行等指针。

版本切换只改这一行指针，模式库与推荐名单按 run_id 各自隔离，
失败/回滚不会覆盖正式版数据，复核意见随各 run 的推荐行永久保留。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base
from src.utils.timeutil import utcnow

ACTIVE_RUN_KEY = "active_mine_run_id"


class AppState(Base):
    __tablename__ = "app_state"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    state_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    state_value: Mapped[str | None] = mapped_column(String(120), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
