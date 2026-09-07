"""
Phase 8 — adjudication trigger.

`finalize.py` takes a case to `submitted` and stores
`cases.payer_correlation_id`. This module handles the *other* half: the
async payer outcome. Once the payer adjudicates, something on our side has
to move the case to `payer_approved` / `payer_rejected` so the Aarogyamitra
UI (Supabase Realtime) shows the final result.

In `DISPATCH_MODE=mock` (the demo default) adjudication is a manual step
against the mock payer:

    POST http://localhost:8081/adjudicate/<payer_correlation_id>  {"outcome": "approved"}

The mock then reports that outcome on `GET /received`. This module polls
that endpoint and applies any outcome it finds to the matching case.

Run it like the worker, in its own process:

    uv run python -m mediplug.worker.adjudication

Zero lines in any file Kabir owns — self-contained, mirrors `finalize.py`.

For real NHCX later, `apply_adjudication(case_id, outcome)` is the same
core a `/v0.7/{flow}/on_submit` callback route would call. Out of scope
now (no sandbox credentials).

Retry / idempotency note: `_apply` re-reads `cases.status` and no-ops if
it is not `submitted`. The poller's own `SELECT` also filters
`status = 'submitted'`, so a transitioned case drops out of the next pass
naturally. Sync DB helpers called from an async entry point — the same
blocking-DB pattern `finalize.py` already carries as documented Phase 9
tech debt.
"""

from __future__ import annotations

import asyncio

import httpx
import psycopg
import structlog
from psycopg.types.json import Json

from ..config import settings

log = structlog.get_logger()

POLL_INTERVAL_SECS = 10

STATUS_BY_OUTCOME = {"approved": "payer_approved", "rejected": "payer_rejected"}


def _connect() -> psycopg.Connection:
    # Sync connect per call — same pattern (and same Phase 9 tech debt) as
    # finalize._connect / pipeline._set_status. prepare_threshold=None for
    # the Supabase pooler.
    return psycopg.connect(settings.database_url, prepare_threshold=None)


def _apply(case_id: str, new_status: str, detail: dict) -> bool:
    """Move one `submitted` case to `new_status`, writing a `case_events`
    row with `actor='payer'` and a `detail` jsonb payload.

    Returns False (and writes nothing) if the case is not currently
    `submitted` — natural idempotency, and a guard for a future endpoint
    path that would not pre-filter by status.
    """
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT status FROM cases WHERE id = %s", (case_id,))
        row = cur.fetchone()
        if row is None:
            log.warning("adjudication_skipped", case_id=case_id, reason="case not found")
            return False
        current = row[0]
        if current != "submitted":
            log.info(
                "adjudication_skipped",
                case_id=case_id,
                reason="not submitted",
                current_status=current,
            )
            return False

        cur.execute("UPDATE cases SET status = %s WHERE id = %s", (new_status, case_id))
        cur.execute(
            "INSERT INTO case_events (case_id, from_status, to_status, actor, detail) "
            "VALUES (%s, %s, %s, 'payer', %s)",
            (case_id, current, new_status, Json(detail)),
        )
        conn.commit()

    log.info(
        "status_transition",
        case_id=case_id,
        from_status="submitted",
        to_status=new_status,
        actor="payer",
    )
    return True


def apply_adjudication(
    case_id: str, outcome: str, *, detail: dict | None = None
) -> bool:
    """Apply a payer outcome to a case.

    `outcome` must be `"approved"` or `"rejected"` — anything else raises
    `ValueError`. Returns True if the case transitioned, False if it was
    already past `submitted` (idempotent no-op).
    """
    if outcome not in STATUS_BY_OUTCOME:
        raise ValueError(f"unknown adjudication outcome {outcome!r}")
    return _apply(
        case_id,
        STATUS_BY_OUTCOME[outcome],
        {"outcome": outcome, **(detail or {})},
    )


def _submitted_cases() -> list[tuple[str, str]]:
    """(case_id, payer_correlation_id) for every case awaiting adjudication."""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, payer_correlation_id FROM cases "
            "WHERE status = 'submitted' AND payer_correlation_id IS NOT NULL"
        )
        return [(str(cid), corr) for cid, corr in cur.fetchall()]


async def poll_once(*, client: httpx.AsyncClient | None = None) -> int:
    """One pass: read the mock payer's `/received`, apply any resolved
    outcome to the matching `submitted` case. Returns the number applied.
    """
    if settings.dispatch_mode != "mock":
        return 0

    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=15)
    try:
        resp = await client.get(f"{settings.mock_payer_url}/received")
        resp.raise_for_status()
        received = resp.json()
    finally:
        if owns_client:
            await client.aclose()

    applied = 0
    for case_id, corr in _submitted_cases():
        entry = received.get(corr)
        if (
            entry
            and entry.get("outcome") in STATUS_BY_OUTCOME
            and apply_adjudication(
                case_id, entry["outcome"], detail={"source": "mock-poller"}
            )
        ):
            applied += 1
    return applied


async def run_poller(interval: int = POLL_INTERVAL_SECS) -> None:
    if settings.dispatch_mode != "mock":
        log.info("adjudication_poller_disabled", dispatch_mode=settings.dispatch_mode)
        return

    log.info("adjudication_poller_started", interval=interval)
    while True:
        try:
            n = await poll_once()
            if n:
                log.info("adjudications_applied", count=n)
        except Exception as exc:
            log.error("adjudication_poll_error", error=str(exc))
        await asyncio.sleep(interval)


if __name__ == "__main__":
    from ..logging import configure

    configure()
    asyncio.run(run_poller())
