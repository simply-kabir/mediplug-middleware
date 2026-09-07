"""
Graceful shutdown signalling — Phase 9.2.

One asyncio.Event shared by every long-running loop in a worker process
(the consumer read loop, the stalled-message reclaimer, the adjudication
poller). It lives in its own neutral module rather than in consumer.py
because worker/adjudication.py runs as a *separate* process — importing
consumer to reach the event would drag pipeline -> sentence-transformers
into the poller for nothing.

Usage:

    from ..shutdown import shutdown_event, install_handlers
    install_handlers()                       # once, inside the running loop
    while not shutdown_event.is_set():
        ...

`install_handlers()` wires SIGTERM + SIGINT to `request_shutdown()`. A
second signal escalates straight to `os._exit(1)`: installing a SIGINT
handler otherwise suppresses KeyboardInterrupt, so a second Ctrl-C during
a 90s dispatch would do nothing.

Honest exit latency: `block=5000` on XREADGROUP bounds only the idle wait.
A shutdown that lands during a slow dispatch is 5s + process_case (up to
~93s for 3 mock-payer attempts) + backoff (up to 30s) ~= 2 minutes. If the
worker is ever containerised it needs `stop_grace_period: 150s`, or SIGTERM
becomes SIGKILL and you are back to the crash path this module exists to
avoid.
"""

from __future__ import annotations

import asyncio
import os
import signal

import structlog

log = structlog.get_logger()

shutdown_event = asyncio.Event()


def request_shutdown() -> None:
    """Ask every loop watching `shutdown_event` to wind down. A second call
    (second signal) means the operator is done waiting — exit now."""
    if shutdown_event.is_set():
        log.warning("shutdown_forced", detail="second signal — exiting immediately")
        os._exit(1)
    shutdown_event.set()
    log.info("shutdown_requested")


def install_handlers() -> None:
    """Wire SIGTERM + SIGINT to `request_shutdown()`. Call from inside the
    running event loop (e.g. at the top of the worker's async entry point)."""
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, request_shutdown)
        except NotImplementedError:
            # Windows proactor loop has no add_signal_handler. A plain
            # signal.signal callback runs between bytecodes and will NOT
            # wake a loop blocked in select(), so it has to poke the loop.
            signal.signal(
                sig, lambda *_: loop.call_soon_threadsafe(request_shutdown)
            )
