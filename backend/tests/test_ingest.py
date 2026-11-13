"""采集服务单测：迟到事件按时间并入、同批重放去重、幂等屏障。"""
from __future__ import annotations

from datetime import datetime

from src.models.event_def import EventDef
from src.models.event_seq import EventSeq
from src.models.ingested_event import IngestedEvent
from src.services.ingest_service import ingest_events, merge_session_events, watermark

MODULE = {
    "acct.login": "account",
    "board.view": "board",
    "board.filter": "board",
    "report.build": "report",
}


def _seed_events(db):
    for code, module in MODULE.items():
        db.add(EventDef(code=code, module=module, label=code))
    db.flush()


def test_merge_late_event_sorted_by_time():
    existing = ["acct.login", "board.view"]
    existing_times = ["2026-11-13T08:00:00Z", "2026-11-13T09:00:00Z"]
    # 迟到事件发生在两者之间
    incoming = [("board.filter", datetime(2026, 11, 13, 8, 30), 0)]
    codes, times, late = merge_session_events(existing, existing_times, incoming)
    assert codes == ["acct.login", "board.filter", "board.view"]
    assert late is True
    assert len(times) == 3


def test_merge_adjacent_duplicates_collapse():
    incoming = [
        ("board.view", datetime(2026, 11, 13, 9, 0), 0),
        ("board.view", datetime(2026, 11, 13, 9, 5), 1),
        ("report.build", datetime(2026, 11, 13, 9, 10), 2),
    ]
    codes, _times, _late = merge_session_events([], None, incoming)
    assert codes == ["board.view", "report.build"]


def test_ingest_idempotent_same_batch_replay(db):
    _seed_events(db)
    payload = [
        {"event_id": "e1", "session_id": "s1", "user_id": "u1", "event": "acct.login",
         "occurred_at": "2026-11-13T08:00:00Z"},
        {"event_id": "e2", "session_id": "s1", "user_id": "u1", "event": "board.view",
         "occurred_at": "2026-11-13T09:00:00Z"},
    ]
    first = ingest_events(db, payload)
    db.commit()
    assert first["ingested"] == 2
    assert first["sessions_new"] == 1
    # 同批重放：全部命中 event_id 台账，不新增、不重复
    second = ingest_events(db, payload)
    db.commit()
    assert second["ingested"] == 0
    assert second["duplicate"] == 2
    assert db.query(EventSeq).count() == 1
    assert db.query(IngestedEvent).count() == 2


def test_late_event_merges_into_existing_session_no_new_session(db):
    _seed_events(db)
    ingest_events(db, [
        {"event_id": "e1", "session_id": "s1", "user_id": "u1", "event": "acct.login",
         "occurred_at": "2026-11-13T08:00:00Z"},
        {"event_id": "e2", "session_id": "s1", "user_id": "u1", "event": "board.view",
         "occurred_at": "2026-11-13T09:00:00Z"},
    ])
    db.commit()
    # 迟到补发：event_id 全新，但 session 已存在
    stats = ingest_events(db, [
        {"event_id": "e3", "session_id": "s1", "user_id": "u1", "event": "board.filter",
         "occurred_at": "2026-11-13T08:30:00Z"},
    ])
    db.commit()
    assert stats["sessions_new"] == 0
    assert stats["sessions_updated"] == 1
    assert stats["late"] == 1
    seq = db.query(EventSeq).filter_by(session_id="s1").one()
    assert seq.events == ["acct.login", "board.filter", "board.view"]
    assert db.query(EventSeq).count() == 1
    mark = watermark(db)["items"][0]
    assert mark["late_events"] == 1
    assert mark["ingested_events"] == 3


def test_duplicate_within_single_batch_counted_once(db):
    _seed_events(db)
    payload = [
        {"event_id": "e1", "session_id": "s1", "user_id": "u1", "event": "acct.login",
         "occurred_at": "2026-11-13T08:00:00Z"},
        {"event_id": "e1", "session_id": "s1", "user_id": "u1", "event": "acct.login",
         "occurred_at": "2026-11-13T08:00:00Z"},
    ]
    stats = ingest_events(db, payload)
    db.commit()
    assert stats["ingested"] == 1
    assert stats["duplicate"] == 1
    assert db.query(IngestedEvent).count() == 1
