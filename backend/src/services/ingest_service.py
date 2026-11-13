"""采集服务：会话事件幂等并入。

解决三个问题：
1. 采集端会补发同一会话的迟到事件 —— 按事件时间并入原会话，不新增会话；
2. 同批事件会重复到达 —— 以 ingested_events 台账的 event_id 去重，同批重跑不新增、不重复；
3. 维护每路采集的水位（ingest_cursors），记录已确认并入的时间水位与计数。

合并规则（merge_session_events，纯函数可单测）：
- 带 occurred_at 的事件按 (时间, 到达序) 排序，迟到事件自然落到正确位置；
- 不带时间的事件码追加到尾部；
- 相邻完全相同的事件码合并为一次点击（与 demo_data 口径一致），非相邻重复保留。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.models.event_def import EventDef
from src.models.event_seq import EventSeq
from src.models.ingest_cursor import IngestCursor
from src.models.ingested_event import IngestedEvent
from src.utils.timeutil import iso_utc, utcnow

DEFAULT_SOURCE = "collector"


def _parse_time(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed


def merge_session_events(
    existing_codes: list[str],
    existing_times: list[str] | None,
    incoming: list[tuple[str, datetime | None, int]],
) -> tuple[list[str], list[str | None], bool]:
    """把一批 (事件码, 发生时间, 到达序) 并入已有会话。

    返回 (合并后的事件码, 对齐的 ISO 时间, 是否有迟到插入)。
    调用方保证 incoming 已按 event_id 去重。
    """
    timed: list[tuple[datetime, int, str]] = []
    untimed: list[tuple[int, str]] = []
    seq = 0
    for code in existing_codes:
        at = _parse_time(existing_times[seq]) if existing_times and seq < len(existing_times) else None
        if at is not None:
            timed.append((at, seq, code))
        else:
            untimed.append((seq, code))
        seq += 1
    existing_at = [t for t in (_parse_time(v) for v in (existing_times or [])) if t]
    latest_existing = max(existing_at, default=None)
    late_inserted = False
    for code, at, order in incoming:
        if at is None:
            untimed.append((order, code))
            continue
        if latest_existing and at < latest_existing:
            late_inserted = True
        timed.append((at, order, code))
    timed.sort(key=lambda item: (item[0], item[1]))
    untimed.sort(key=lambda item: item[0])
    ordered = [code for _at, _order, code in timed] + [code for _order, code in untimed]
    times = [iso_utc(at) for at, _order, _code in timed] + [None] * len(untimed)
    # 合并相邻重复事件：连续两次同事件码视为一次点击
    merged_codes: list[str] = []
    merged_times: list[str | None] = []
    for code, at in zip(ordered, times):
        if merged_codes and merged_codes[-1] == code:
            continue
        merged_codes.append(code)
        merged_times.append(at)
    return merged_codes, merged_times, late_inserted


def _modules_path(events: list[str], module_of: dict[str, str]) -> str:
    return ">".join(module_of.get(code, "board") for code in events)


def get_cursor(db: Session, source: str) -> IngestCursor:
    row = db.scalar(select(IngestCursor).where(IngestCursor.source == source))
    if row is None:
        row = IngestCursor(source=source, ingested_events=0, duplicate_events=0, late_events=0)
        db.add(row)
        db.flush()
    return row


def ingest_events(db: Session, payloads: list[dict], source: str = DEFAULT_SOURCE) -> dict:
    """幂等并入一批采集事件。

    每个 payload：{event_id, session_id, user_id, event, occurred_at?}
    - event_id 已在台账（历史批次）或本批重复 → 计 duplicate，跳过；
    - 同 session_id 已存在 → 迟到事件按时间并入；否则新建会话；
    - 全程更新 source 水位。返回本批处理统计。
    """
    cursor = get_cursor(db, source)
    module_of = {row.code: row.module for row in db.scalars(select(EventDef)).all()}
    sessions: dict[str, EventSeq] = {row.session_id: row for row in db.scalars(select(EventSeq)).all()}

    # 一次性取本批涉及 event_id 的台账记录，构成幂等屏障
    candidate_ids = {str(p.get("event_id") or "").strip() for p in payloads}
    candidate_ids.discard("")
    known: set[str] = set(
        db.scalars(select(IngestedEvent.event_id).where(IngestedEvent.event_id.in_(candidate_ids))).all()
    ) if candidate_ids else set()

    seen_batch: set[str] = set()
    per_session: dict[str, list[tuple[str, datetime | None, int]]] = {}
    meta: dict[str, dict] = {}
    stats = {
        "received": len(payloads),
        "ingested": 0,
        "duplicate": 0,
        "late": 0,
        "sessions_new": 0,
        "sessions_updated": 0,
    }

    for order, payload in enumerate(payloads):
        event_id = str(payload.get("event_id") or "").strip()
        session_id = str(payload.get("session_id") or "").strip()
        event_code = str(payload.get("event") or payload.get("event_code") or "").strip()
        if not event_id or not session_id or not event_code:
            stats["duplicate"] += 1
            continue
        if event_id in seen_batch or event_id in known:
            stats["duplicate"] += 1
            cursor.duplicate_events += 1
            continue
        seen_batch.add(event_id)
        at = _parse_time(payload.get("occurred_at"))
        per_session.setdefault(session_id, []).append((event_code, at, order))
        meta[event_id] = {"session_id": session_id, "event_code": event_code, "at": at}
        if at and (cursor.last_event_at is None or at > cursor.last_event_at):
            cursor.last_event_at = at
        cursor.last_event_id = event_id

    for session_id, incoming in per_session.items():
        row = sessions.get(session_id)
        if row is None:
            first_payload = next(
                (p for p in payloads if str(p.get("session_id") or "").strip() == session_id), {}
            )
            merged, merged_times, _late = merge_session_events([], None, incoming)
            started = min((at for _code, at, _o in incoming if at), default=None) or utcnow()
            row = EventSeq(
                session_id=session_id,
                user_id=str(first_payload.get("user_id") or "unknown"),
                events=merged,
                event_times=merged_times,
                event_count=len(merged),
                modules_path=_modules_path(merged, module_of),
                started_at=started,
                converted=False,
            )
            db.add(row)
            stats["sessions_new"] += 1
        else:
            merged, merged_times, late = merge_session_events(
                list(row.events or []), list(row.event_times or []), incoming
            )
            if late:
                stats["late"] += sum(1 for _c, at, _o in incoming if at is not None)
                cursor.late_events += 1
            row.events = merged
            row.event_times = merged_times
            row.event_count = len(merged)
            row.modules_path = _modules_path(merged, module_of)
            stats["sessions_updated"] += 1
        stats["ingested"] += len(incoming)
        cursor.ingested_events += len(incoming)

    for event_id in seen_batch:
        info = meta[event_id]
        db.add(
            IngestedEvent(
                event_id=event_id,
                source=source,
                session_id=info["session_id"],
                event_code=info["event_code"],
                event_at=info["at"],
            )
        )
    db.flush()
    return stats


def watermark(db: Session) -> dict:
    rows = db.scalars(select(IngestCursor).order_by(IngestCursor.source)).all()
    return {
        "items": [
            {
                "source": row.source,
                "last_event_id": row.last_event_id,
                "last_event_at": iso_utc(row.last_event_at),
                "ingested_events": row.ingested_events,
                "duplicate_events": row.duplicate_events,
                "late_events": row.late_events,
                "updated_at": iso_utc(row.updated_at),
            }
            for row in rows
        ]
    }
