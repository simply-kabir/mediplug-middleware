"""Redis Streams wrapper. One module, both sides of the stream — gateway
enqueues, worker (Phase 4) consumes off the same key with XREADGROUP/XACK —
so nothing has to be re-derived or guessed between them.

IMPORTANT: the payload lives under the "payload" field. worker/consumer.py
reads raw["payload"] — if this ever changes, the worker must change with it.
"""

from __future__ import annotations

import redis.asyncio as redis

from .config import settings
from .schemas import JobEnvelope

_pool: redis.Redis | None = None


def get_redis() -> redis.Redis:
    """Lazily create one shared async Redis client per process."""
    global _pool
    if _pool is None:
        _pool = redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_timeout=10.0,  # must exceed the worker's XREADGROUP block time (5s)
            socket_connect_timeout=5.0,
        )
    return _pool


async def ensure_group() -> None:
    """Idempotently create the consumer group. Safe to call on every boot —
    BUSYGROUP on a repeat call means the group already exists, not an error."""
    r = get_redis()
    try:
        await r.xgroup_create(
            settings.stream_key, settings.consumer_group, id="0", mkstream=True
        )
    except redis.ResponseError as e:
        if "BUSYGROUP" not in str(e):
            raise


async def enqueue(envelope: JobEnvelope) -> str:
    """XADD one job envelope onto the shared stream. The whole envelope goes
    in as a single JSON field so the stream schema stays stable even if
    JobEnvelope grows fields later — the worker just does
    JobEnvelope.model_validate_json() on read.

    Returns the Redis-assigned message id (useful for logging, not stored).
    """
    r = get_redis()
    return await r.xadd(settings.stream_key, {"payload": envelope.model_dump_json()})