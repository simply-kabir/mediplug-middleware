"""
Worker consumer — Redis Streams XREADGROUP loop.

Responsibilities:
  - Read new messages from the stream via XREADGROUP
  - Dispatch each to pipeline.process_case()
  - XACK on every exit path (success, failure, DLQ, poison)
  - Reclaim stalled messages from dead consumers via XAUTOCLAIM
  - Exponential backoff + DLQ after max_delivery_attempts
  - Graceful SIGTERM/SIGINT shutdown: finish the current job, then exit
"""

from __future__ import annotations

import asyncio
import sys

# psycopg3's async pool refuses to run under Windows' default
# ProactorEventLoop — it needs a selector-based loop to manage sockets.
# This MUST run before uvicorn (or anything else) creates the event loop,
# which is why it's the very first thing in this module, above every
# other import. uvicorn imports this module before it creates its loop,
# so setting the policy here — not in the CLI command, not in lifespan —
# is what actually takes effect in time.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


import asyncio
import json
import socket

import structlog
from redis.asyncio import Redis

from ..config import settings
from ..queue import close_redis, ensure_group, get_redis
from ..shutdown import install_handlers, shutdown_event
from .pipeline import process_case

log = structlog.get_logger()

# Unique consumer name — hostname + object id gives us uniqueness even when
# running multiple workers on the same machine.
CONSUMER_NAME = f"worker-{socket.gethostname()}-{id(object())}"

# msg_ids currently being processed by _handle in THIS process. The read
# loop and reclaim_stalled share CONSUMER_NAME, and XAUTOCLAIM will happily
# claim a message to the consumer that already holds it — so without this
# guard a _handle slower than claim_stale_ms gets a concurrent second
# process_case in the same process (two payer POSTs, a silently no-op
# second xack). reclaim_stalled skips anything in here.
_inflight: set[str] = set()

# How long, on shutdown, to let the reclaimer's in-flight _handle finish
# before force-cancelling it. Cancelling mid-_handle is data-loss-suspect
# (CancelledError is a BaseException on 3.11 — no XACK, no retry, no DLQ,
# case stranded at `dispatching` with the payer already POSTed), so this is
# generous: worst-case process_case (~93s) + backoff (~30s) + slack.
_SHUTDOWN_GRACE_S = 150


async def _sleep_or_shutdown(seconds: float) -> None:
    """Sleep for `seconds`, but return immediately if shutdown is requested."""
    try:
        await asyncio.wait_for(shutdown_event.wait(), timeout=seconds)
    except TimeoutError:  # 3.11: asyncio.TimeoutError is an alias of TimeoutError
        pass


async def _handle(r: Redis, msg_id: str, raw: dict | None) -> None:
    """Process one stream message. XACK on every exit path."""
    _inflight.add(msg_id)
    try:
        # Parse INSIDE the try: a malformed message must not propagate out
        # of _handle, out of run()'s loop, and kill the process — with the
        # message still in the PEL so every restart dies on it again. DLQ
        # it and ACK it instead.
        try:
            job = json.loads(raw["payload"])
        except (KeyError, TypeError, ValueError) as exc:
            log.error("job_poisoned", msg_id=msg_id, error=str(exc))
            await r.xadd(
                settings.dlq_stream_key,
                {"payload": json.dumps({"raw": str(raw), "error": f"unparseable: {exc}"})},
            )
            await r.xack(settings.stream_key, settings.consumer_group, msg_id)
            return

        attempt = job.get("attempt", 1)
        job_id = job.get("job_id", "unknown")

        try:
            await process_case(job)
            await r.xack(settings.stream_key, settings.consumer_group, msg_id)
            log.info("job_completed", job_id=job_id, case_id=job.get("case_id"))

        except Exception as exc:
            log.error("job_failed", job_id=job_id, attempt=attempt, error=str(exc))

            if attempt >= settings.max_delivery_attempts:
                # Dead-letter it, then ACK so it stops cycling. No maxlen on
                # the DLQ — it is the only forensic record a job existed.
                await r.xadd(
                    settings.dlq_stream_key,
                    {"payload": json.dumps({**job, "error": str(exc)})},
                )
                await r.xack(settings.stream_key, settings.consumer_group, msg_id)
                log.error("job_dead_lettered", job_id=job_id)
            else:
                # Re-enqueue with incremented attempt, ACK the old message.
                # A shutdown during the backoff short-circuits the wait; the
                # re-XADD + XACK below still run, so the job goes back on the
                # stream rather than being stranded.
                await _sleep_or_shutdown(min(2**attempt, 30))
                await r.xadd(
                    settings.stream_key,
                    {"payload": json.dumps({**job, "attempt": attempt + 1})},
                    maxlen=settings.stream_maxlen,
                    approximate=True,
                )
                await r.xack(settings.stream_key, settings.consumer_group, msg_id)
                log.warning("job_retried", job_id=job_id, next_attempt=attempt + 1)
    finally:
        _inflight.discard(msg_id)


async def reclaim_stalled(r: Redis) -> None:
    """Background task: recover messages from workers that died mid-job.

    XAUTOCLAIM grabs messages that have been pending longer than
    claim_stale_ms without an ACK — i.e. a consumer crashed.
    """
    while not shutdown_event.is_set():
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

            # 3rd reply element: PEL entries whose stream entry was trimmed
            # away (XTRIM / MAXLEN). Redis drops them from the PEL and only
            # reports the ids here — those jobs never run, never retry,
            # never reach the DLQ. At minimum, log it loudly.
            vanished = result[2] if len(result) > 2 else []
            if vanished:
                log.error("reclaim_entries_vanished", ids=list(vanished), count=len(vanished))

            for msg_id, raw in msgs:
                if shutdown_event.is_set():
                    break
                if msg_id in _inflight:
                    continue  # this process is already handling it
                if not raw or not raw.get("payload"):
                    # redis-py's parser can emit (None, None) against older
                    # servers — skip defensively rather than crash the loop.
                    log.warning("reclaim_skipped_empty", msg_id=msg_id)
                    continue
                log.warning("reclaimed_stalled_message", msg_id=msg_id)
                await _handle(r, msg_id, raw)
        except Exception as exc:
            log.error("reclaim_error", error=str(exc))
        await _sleep_or_shutdown(settings.reclaim_interval_s)


async def run() -> None:
    """Main entry point — start the consumer loop."""
    install_handlers()
    await ensure_group()
    r = get_redis()

    # Keep the reclaim task handle — it must be awaited on shutdown, not
    # discarded (see teardown below).
    reclaim_task = asyncio.create_task(reclaim_stalled(r))
    log.info("worker_started", consumer=CONSUMER_NAME)

    try:
        while not shutdown_event.is_set():
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
                    # A message already read is run to completion even if
                    # shutdown fired mid-block — "finish current job, then exit".
                    await _handle(r, msg_id, raw)
    finally:
        log.info("worker_shutdown_requested")
        shutdown_event.set()  # in case we exited the loop for another reason

        # Teardown ordering matters. Await the reclaim task first so its
        # own in-flight _handle finishes; only cancel if it overruns the
        # grace window (and flag that as data-loss-suspect). Closing Redis
        # before the task exits would blow up its next command.
        try:
            await asyncio.wait_for(reclaim_task, timeout=_SHUTDOWN_GRACE_S)
        except TimeoutError:
            log.error("reclaim_task_cancelled_on_shutdown", detail="data-loss-suspect")
            reclaim_task.cancel()
            try:
                await reclaim_task
            except asyncio.CancelledError:
                pass

        await close_redis()
        log.info("worker_stopped")
