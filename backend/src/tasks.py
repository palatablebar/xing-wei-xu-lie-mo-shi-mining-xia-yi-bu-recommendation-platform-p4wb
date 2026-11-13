"""Celery 任务：worker 心跳 + 每日增量重挖（定时刷新模式库与推荐）。"""
from __future__ import annotations

import time

from src.config.celery_app import TASK_DAILY, TASK_HEARTBEAT, celery_app
from src.config.settings import settings
from src.utils.logger import get_logger

logger = get_logger("tasks")


def _redis():
    import redis

    return redis.Redis.from_url(settings.redis_url, decode_responses=True)


@celery_app.task(name=TASK_HEARTBEAT)
def heartbeat() -> str:
    try:
        _redis().set(f"{settings.app_name}:worker:heartbeat", time.time())
        return "ok"
    except Exception as exc:  # noqa: BLE001
        logger.warning("heartbeat failed: %s", exc)
        return "error"


@celery_app.task(name=TASK_DAILY)
def daily_remine() -> dict:
    """每天用最新会话做一次可恢复重挖：候选版分阶段跑完，不自动切换正式版。

    同批重跑（当天再次触发）命中同一 seed_key：未完成则从断点续跑，
    已完成则原样返回，不新增运行、不重复落库。切换由人工在推荐台/挖掘页确认。
    """
    from src.config.database import session_scope
    from src.services import audit_service, mining_service, version_service
    from src.utils.timeutil import local_now

    operator = "scheduler@" + settings.app_name + ".io"
    batch_key = f"daily-{local_now():%Y%m%d}"
    with session_scope() as db:
        run = mining_service.create_and_execute(
            db,
            min_support_pct=3.0,
            max_len=5,
            top_k=50,
            trigger="schedule",
            operator_email=operator,
            seed_key=batch_key,
        )
        active = version_service.active_run(db)
        audit_service.record(
            db,
            actor_email=operator,
            actor_role="admin",
            action="mining.create",
            entity="mine_run",
            entity_id=run.id,
            detail={
                "trigger": "schedule",
                "batch": batch_key,
                "run_no": run.run_no,
                "stage": run.stage,
                "status": run.status,
                "patterns": run.pattern_count,
                "active_run_no": active.run_no if active else None,
            },
            layer="worker",
        )
    logger.info("daily remine batch=%s stage=%s patterns=%s", batch_key, run.stage, run.pattern_count)
    return {
        "batch": batch_key,
        "run_no": run.run_no,
        "stage": run.stage,
        "patterns": run.pattern_count,
        "recommendations": run.rec_count,
    }
