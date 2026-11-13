"""端到端冒烟：建表 → seed 演示数据并切正式版 → 迟到事件并入 → 可恢复重挖 → 差异 → 回滚。"""
from __future__ import annotations

import tempfile
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.config import database
from src.models import Base
from src.models.recommendation import Recommendation
from src.services import ingest_service, mining_service, overview_service, version_service

tmpdir = Path(tempfile.mkdtemp())
engine = create_engine(f"sqlite:///{tmpdir / 'smoke.db'}", future=True)
Base.metadata.create_all(engine)
factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
database.SessionLocal = factory
database.engine = engine

# 1) seed（确定性数据，内部会把首个 run 提升为正式版）
from scripts.seed_demo import seed_all  # noqa: E402

summary = seed_all()
print("seed:", summary)

with factory() as db:
    active = version_service.active_run(db)
    print("active v1:", active.run_no, active.stage, "recs", active.rec_count, "cover", active.coverage_pct)

    # 在正式版上先采纳一条推荐，验证后续继承
    sample = db.query(Recommendation).filter_by(run_id=active.id).first()
    sample.status = "confirmed"
    sample.reviewer = "analyst@nextbest.io"
    sample.review_note = "沿用我"
    sample_key = (sample.user_id, tuple(sample.suffix), sample.next_event)
    db.commit()

    # 2) 迟到事件并入：给已存在会话 s0001 补发一条更早的事件
    before = db.query(__import__("src.models.event_seq", fromlist=["EventSeq"]).EventSeq).filter_by(session_id="s0001").one()
    print("s0001 before:", before.events[:4])
    stats = ingest_service.ingest_events(db, [
        {"event_id": "late-1", "session_id": "s0001", "user_id": before.user_id,
         "event": "acct.login", "occurred_at": "2026-09-30T08:00:00Z"},
        {"event_id": "late-1", "session_id": "s0001", "user_id": before.user_id,
         "event": "acct.login", "occurred_at": "2026-09-30T08:00:00Z"},  # 重复
    ])
    db.commit()
    print("ingest stats:", stats)
    after = db.query(__import__("src.models.event_seq", fromlist=["EventSeq"]).EventSeq).filter_by(session_id="s0001").one()
    print("s0001 after :", after.events[:4], "| times:", (after.event_times or [])[:2])

    # 3) 可恢复重挖（候选版，同批 key）— 在采集之后，水位快照非空
    run = mining_service.create_and_execute(
        db, min_support_pct=3.0, max_len=5, top_k=50, trigger="schedule",
        operator_email="scheduler@nextbest.io", seed_key="daily-20261113",
    )
    rerun = mining_service.create_and_execute(
        db, min_support_pct=3.0, max_len=5, top_k=50, trigger="schedule",
        operator_email="scheduler@nextbest.io", seed_key="daily-20261113",
    )
    print("candidate:", run.run_no, run.stage, "same run on rerun:", run.id == rerun.id)
    print("watermark snapshot:", run.watermark_at, "ingested:", run.ingested_events,
          "carried:", run.carried_recs, "reset:", run.reset_recs)

    # 复核继承断言：同 (用户,后缀,下一步) 的已采纳推荐沿用结论
    inherited = db.query(Recommendation).filter_by(run_id=run.id, carry_state="same").all()
    hit = next((r for r in inherited if (r.user_id, tuple(r.suffix), r.next_event) == sample_key), None)
    print("inherited same-state rec:", bool(hit), "| status kept:", hit.status if hit else None,
          "| note kept:", hit.review_note if hit else None)

    diff = version_service.run_diff(db, active.id, run.id)
    print("diff patterns:", diff["patterns"], "recs:", diff["recommendations"], "Δcov:", diff["coverage_delta_pct"])

    # 4) 切换正式版
    version_service.promote(db, run)
    db.commit()
    print("active v2:", version_service.active_run(db).run_no)

    # 5) 回滚到 v1
    version_service.rollback_to(db, active)
    db.commit()
    print("rolled back active:", version_service.active_run(db).run_no)

    # 6) 总览口径
    ov = overview_service.overview(db)
    print("overview keys:", sorted(k for k in ov if k in ("active_run", "candidate_run", "watermark", "switch_diff")))
    print("watermark:", ov["watermark"]["watermark_at"], "dups:", ov["watermark"]["duplicate_events"],
          "late:", ov["watermark"]["late_events"])
    print("active_run run_no:", ov["active_run"]["run_no"], "| candidate stage:", ov["candidate_run"]["stage"])

print("SMOKE OK")
