"""可恢复重挖与版本切换集成测试：分阶段、复核继承、promote/rollback、断点恢复。"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from src.models.event_def import EventDef
from src.models.event_seq import EventSeq
from src.models.mine_run import MineRun
from src.models.recommendation import Recommendation
from src.services import mining_service, version_service

CATALOG = {
    "acct.login": "account",
    "board.view": "board",
    "board.filter": "board",
    "report.build": "report",
    "report.export": "report",
    "alert.create": "alert",
}
BASE = datetime(2026, 10, 1, 8, 0, 0)


def _seed_world(db, users=20):
    for code, module in CATALOG.items():
        db.add(EventDef(code=code, module=module, label=code))
    db.flush()
    seq_no = 0
    for idx in range(users):
        user = f"u{idx + 1:03d}"
        long_seq = (
            ["acct.login", "board.view", "report.build", "report.export"]
            if idx % 2 == 0
            else ["acct.login", "board.view", "report.build", "alert.create"]
        )
        seq_no += 1
        db.add(EventSeq(session_id=f"s{seq_no:04d}", user_id=user, events=long_seq,
                        event_count=len(long_seq), started_at=BASE + timedelta(hours=seq_no)))
        # 每位用户再给一条更晚的短会话，使后缀稳定为 [login, view, build]
        seq_no += 1
        short = ["acct.login", "board.view", "report.build"]
        db.add(EventSeq(session_id=f"s{seq_no:04d}", user_id=user, events=short,
                        event_count=len(short), started_at=BASE + timedelta(days=2, hours=seq_no)))
    db.commit()


def _run(db, seed=None):
    return mining_service.create_and_execute(
        db, min_support_pct=5.0, max_len=4, top_k=50, trigger="manual",
        operator_email="analyst@nextbest.io", seed_key=seed,
    )


def test_staged_run_completes_and_promote_switches_active(db):
    _seed_world(db)
    run_a = _run(db, seed="batch-a")
    assert run_a.stage == "done"
    assert run_a.pattern_count > 0 and run_a.rec_count > 0
    # 候选版默认不影响正式版
    assert version_service.active_run(db) is None
    version_service.promote(db, run_a)
    db.commit()
    assert version_service.active_run(db).id == run_a.id
    assert run_a.is_active is True


def test_review_carry_same_changed_new_and_switch_diff(db):
    _seed_world(db)
    run_a = _run(db, seed="batch-a")
    version_service.promote(db, run_a)
    db.commit()
    # 在正式版上复核两条：一条采纳、一条驳回
    recs_a = db.query(Recommendation).filter_by(run_id=run_a.id).order_by(Recommendation.id).all()
    confirmed = next(r for r in recs_a if r.status == "pending")
    mining_status_note = "沿用我"
    confirmed.status = "confirmed"
    confirmed.reviewer = "analyst@nextbest.io"
    confirmed.review_note = mining_status_note
    dismissed = next(r for r in recs_a if r.id != confirmed.id)
    dismissed.status = "dismissed"
    dismissed.reviewer = "analyst@nextbest.io"
    db.commit()
    confirm_key = (confirmed.user_id, tuple(confirmed.suffix), confirmed.next_event)

    run_b = _run(db, seed="batch-b")
    assert run_b.is_active is False  # 切换前正式版仍是 A
    assert version_service.active_run(db).id == run_a.id
    assert run_b.carried_recs >= 1  # 已复核且同键的推荐沿用结论
    assert run_b.reset_recs >= 1    # 待复核同键推荐回 pending

    carried = db.query(Recommendation).filter_by(run_id=run_b.id, carry_state="same").all()
    matched = next(r for r in carried if (r.user_id, tuple(r.suffix), r.next_event) == confirm_key)
    assert matched.status == "confirmed"
    assert matched.reviewer == "analyst@nextbest.io"
    assert matched.review_note == mining_status_note
    # 正式版 A 的复核意见原样保留
    assert db.get(Recommendation, confirmed.id).status == "confirmed"

    diff = version_service.run_diff(db, run_a.id, run_b.id)
    assert diff["base"]["id"] == run_a.id and diff["target"]["id"] == run_b.id
    assert diff["recommendations"]["kept"] >= 1

    version_service.promote(db, run_b)
    db.commit()
    assert version_service.active_run(db).id == run_b.id
    assert db.get(MineRun, run_a.id).is_active is False


def test_rollback_restores_previous_edition_without_losing_review(db):
    _seed_world(db)
    run_a = _run(db, seed="batch-a")
    version_service.promote(db, run_a)
    db.commit()
    rec = db.query(Recommendation).filter_by(run_id=run_a.id).first()
    rec.status = "confirmed"
    rec.reviewer = "boss@nextbest.io"
    db.commit()

    run_b = _run(db, seed="batch-b")
    version_service.promote(db, run_b)
    db.commit()
    assert version_service.active_run(db).id == run_b.id

    target = db.get(MineRun, run_a.id)
    version_service.rollback_to(db, target)
    db.commit()
    assert version_service.active_run(db).id == run_a.id
    assert db.get(Recommendation, rec.id).status == "confirmed"
    assert db.get(Recommendation, rec.id).reviewer == "boss@nextbest.io"


def test_failure_resumes_from_breakpoint_without_duplicate_rows(db, monkeypatch):
    _seed_world(db)
    original = mining_service._stage_recommend
    calls = {"n": 0}

    def broken(session, run):
        calls["n"] += 1
        raise RuntimeError("boom at recommend")

    monkeypatch.setattr(mining_service, "_stage_recommend", broken)
    run = _run(db, seed="batch-recover")
    assert run.status == "failed"
    assert run.stage == "patterns"  # 模式阶段已落库，断点停在 patterns
    pattern_rows = db.query(Recommendation).filter_by(run_id=run.id).count()
    assert pattern_rows == 0

    monkeypatch.setattr(mining_service, "_stage_recommend", original)
    # 同批重跑：不新建运行，从断点续跑到 done
    resumed = _run(db, seed="batch-recover")
    assert resumed.id == run.id
    assert resumed.stage == "done"
    assert resumed.rec_count > 0
    # 模式未重复落库
    from src.models.pattern import Pattern

    assert db.query(Pattern).filter_by(run_id=run.id).count() == run.pattern_count


def test_same_batch_rerun_does_not_create_new_run(db):
    _seed_world(db)
    first = _run(db, seed="daily-20261113")
    second = _run(db, seed="daily-20261113")
    assert first.id == second.id
    assert db.query(MineRun).count() == 1


def test_promote_rejects_unfinished_run(db):
    _seed_world(db)
    run = MineRun(run_no="M0", min_support_pct=5.0, max_len=4, top_k=50, trigger="manual",
                  operator_email="x@y.io", status="running", stage="patterns", is_active=False)
    db.add(run)
    db.commit()
    with pytest.raises(Exception):
        version_service.promote(db, run)
    db.rollback()
