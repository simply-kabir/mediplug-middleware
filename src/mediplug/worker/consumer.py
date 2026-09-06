"""
Worker consumer — Redis Streams XREADGROUP loop.

Responsibilities:
  - Read new messages from the stream via XREADGROUP
  - Dispatch each to pipeline.process_case()
  - XACK on every exit path (success, failure, DLQ)
  - Reclaim stalled messages from dead consumers via XAUTOCLAIM
  - Exponential backoff + DLQ after max_delivery_attempts
"""

from __future__ import annotations

import asyncio
import json
import socket

import structlog
from redis.asyncio import Redis

from ..config import settings
from ..queue import ensure_group, get_redis
from .pipeline import process_case

log = structlog.get_logger()

# Unique consumer name — hostname + object id gives us uniqueness even when
# running multiple workers on the same machine.
CONSUMER_NAME = f"worker-{socket.gethostname()}-{id(object())}"


async def _handle(r: Redis, msg_id: str, raw: dict) -> None:
    """Process one stream message. XACK on every exit path."""
    job = json.loads(raw["payload"])
    attempt = job.get("attempt", 1)
    job_id = job.get("job_id", "unknown")

    try:
        await process_case(job)
        await r.xack(settings.stream_key, settings.consumer_group, msg_id)
        log.info("job_completed", job_id=job_id, case_id=job.get("case_id"))

    except Exception as exc:
        log.error("job_failed", job_id=job_id, attempt=attempt, error=str(exc))

        if attempt >= settings.max_delivery_attempts:
            # Dead-letter it, then ACK so it stops cycling.
            await r.xadd(
                settings.dlq_stream_key,
                {"payload": json.dumps({**job, "error": str(exc)})},
            )
            await r.xack(settings.stream_key, settings.consumer_group, msg_id)
            log.error("job_dead_lettered", job_id=job_id)
        else:
            # Re-enqueue with incremented attempt, ACK the old message.
            await asyncio.sleep(min(2**attempt, 30))  # backoff
            await r.xadd(
                settings.stream_key,
                {"payload": json.dumps({**job, "attempt": attempt + 1})},
            )
            await r.xack(settings.stream_key, settings.consumer_group, msg_id)
            log.warning("job_retried", job_id=job_id, next_attempt=attempt + 1)


async def reclaim_stalled(r: Redis) -> None:
    """Background task: recover messages from workers that died mid-job.

    XAUTOCLAIM grabs messages that have been pending longer than
    claim_stale_ms without an ACK — i.e. a consumer crashed.
    """
    while True:
        try:
            result = await r.xautoclaim(
                settings.stream_key,
                settings.consumer_group,
                CONSUMER_NAME,
                min_idle_time=settings.claim_stale_ms,
                count=10,
            )
            # xautoclaim returns (next_start_id, [(msg_id, data), ...], [deleted_ids])
            # but the exact shape varies across redis-py versions.
            msgs = result[1] if len(result) > 1 else []
            for msg_id, raw in msgs:
                log.warning("reclaimed_stalled_message", msg_id=msg_id)
                await _handle(r, msg_id, raw)
        except Exception as exc:
            log.error("reclaim_error", error=str(exc))
        await asyncio.sleep(15)


async def run() -> None:
    """Main entry point — start the consumer loop."""
    await ensure_group()
    r = get_redis()

    # Fire off the stale-message reclaimer as a background task.
    asyncio.create_task(reclaim_stalled(r))
    log.info("worker_started", consumer=CONSUMER_NAME)

    while True:
        resp = await r.xreadgroup(
            settings.consumer_group,
            CONSUMER_NAME,
            {settings.stream_key: ">"},  # ">" = new messages only
            count=1,
            block=5000,  # block up to 5s, then loop (keeps the process responsive)
        )
        if not resp:
            continue
        for _stream, messages in resp:
            for msg_id, raw in messages:
                await _handle(r, msg_id, raw)
