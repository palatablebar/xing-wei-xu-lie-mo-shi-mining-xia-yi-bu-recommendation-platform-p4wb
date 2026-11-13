from __future__ import annotations

from pydantic import BaseModel, Field


class IngestEventIn(BaseModel):
    event_id: str = Field(min_length=1, max_length=120)
    session_id: str = Field(min_length=1, max_length=40)
    user_id: str = Field(min_length=1, max_length=24)
    event: str = Field(min_length=1, max_length=40)
    occurred_at: str | None = Field(default=None, max_length=40)


class IngestIn(BaseModel):
    source: str | None = Field(default=None, max_length=64)
    events: list[IngestEventIn] = Field(default_factory=list, max_length=5000)
