"""Redis Streams wrapper. One module, both sides of the stream — gateway
enqueues, worker (Phase 4) consumes off the same key with XREADGROUP/XACK —
so nothing has to be re-derived or guessed between them.

IMPORTANT: the payload lives under the "payload" field. worker/consumer.py
reads raw["payload"] — if this ever changes, the worker must change with it.
"""

from __future__ import annotations

import json

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


async def close_redis() -> None:
    """Release the shared client. `from_url` owns the underlying pool, so
    `aclose()` (redis-py 8.x) disconnects it. Called on graceful shutdown —
    worker `run()` on exit, gateway `lifespan` finally — so a container stop
    doesn't leak connections. Resets the global so a later `get_redis()`
    rebuilds cleanly (matters for tests)."""
    global _pool
    if _pool is not None:
        await _pool.aclose()
        _pool = None


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

    `maxlen`/`approximate` bound the stream — ACK removes a message from the
    consumer group's pending list, not from the stream itself, so without
    this XLEN grows without limit. `approximate=True` lets Redis trim on
    whole-macronode boundaries (cheap). The retry re-add in
    `worker/consumer.py` carries the same bound; the DLQ deliberately does
    not (it is the only forensic record a job ever existed).
    """
    r = get_redis()
    return await r.xadd(
        settings.stream_key,
        {"payload": envelope.model_dump_json()},
        maxlen=settings.stream_maxlen,
        approximate=True,
    )


async def queue_stats() -> dict:
    """Snapshot of both streams for `GET /admin/queue` (Phase 9.3).

    XLEN of the work stream and the DLQ, the group's pending count, the
    per-group XINFO, and the 10 most recent DLQ payloads (JSON-parsed so the
    admin UI can render the error string without a second round-trip).
    """
    r = get_redis()
    stream_length = await r.xlen(settings.stream_key)

    try:
        dlq_length = await r.xlen(settings.dlq_stream_key)
    except redis.ResponseError:
        dlq_length = 0

    try:
        pending_summary = await r.xpending(settings.stream_key, settings.consumer_group)
        pending = pending_summary.get("pending", 0) if pending_summary else 0
    except redis.ResponseError:
        pending = 0

    try:
        groups = await r.xinfo_groups(settings.stream_key)
    except redis.ResponseError:
        groups = []

    dlq_recent: list[dict] = []
    try:
        for _msg_id, fields in await r.xrange(settings.dlq_stream_key, "-", "+", count=10):
            raw = fields.get("payload")
            try:
                dlq_recent.append(json.loads(raw) if raw else {})
            except (TypeError, ValueError):
                dlq_recent.append({"payload": raw})
    except redis.ResponseError:
        pass

    return {
        "stream_length": stream_length,
        "dlq_length": dlq_length,
        "pending": pending,
        "groups": groups,
        "dlq_recent": dlq_recent,
    }