"""测试夹具：临时文件 SQLite + 替换全局 SessionLocal/engine，覆盖 session_scope 路径。"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.config import database
from src.models import Base


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'test.db'}",
        future=True,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    testing_session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
    original_session = database.SessionLocal
    original_engine = database.engine
    database.SessionLocal = testing_session
    database.engine = engine
    session = testing_session()
    try:
        yield session
    finally:
        session.close()
        database.SessionLocal = original_session
        database.engine = original_engine
        engine.dispose()
