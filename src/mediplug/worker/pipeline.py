"""
Worker pipeline — the "dumb" Day-1 version.

This is a walking skeleton: it proves the pipes work (gateway → Redis →
worker → Postgres status transitions → Supabase Realtime → UI).  No
intelligence yet — that swaps in at Phase 5 (mapping) and Phase 6 (rules).

BUG AVOIDANCE (from the FK constraint discussion):
  The `cases.mapped_package_code` column has a foreign key to
  `packages(code)`.  The guide's example hardcodes "S4GS2.3" which may not
  exist in the real 1670-row package table loaded from the MJPJAY portal.
  That would cause a FK violation crash on every single job.

  For the Day-1 dumb pipeline we intentionally do NOT write
  mapped_package_code.  The exit criteria only needs the status transitions
  to prove the plumbing, not a real mapping (that's Phase 5's job).
"""

from __future__ import annotations

import asyncio

import psycopg
import structlog

from ..config import settings

log = structlog.get_logger()


def _set_status(case_id: str, new_status: str, **extra_fields: object) -> None:
    """Transition a case to a new status in Postgres.

    Also writes an audit event to case_events.
    Any additional keyword arguments are SET as column updates on the cases
    row (e.g. mapped_package_code="S4GS2.3", confidence=0.91).
    """
    # Build the extra SET clauses if any
    if extra_fields:
        extra_sets = ", ".join(f"{col} = %s" for col in extra_fields)
        sql = f"UPDATE cases SET status = %s, {extra_sets} WHERE id = %s"
        params: tuple = (new_status, *extra_fields.values(), case_id)
    else:
        sql = "UPDATE cases SET status = %s WHERE id = %s"
        params = (new_status, case_id)

    with psycopg.connect(settings.database_url) as conn, conn.cursor() as cur:
        # Read previous status for the audit trail
        cur.execute("SELECT status FROM cases WHERE id = %s", (case_id,))
        row = cur.fetchone()
        prev_status = row[0] if row else None

        cur.execute(sql, params)

        # Audit event
        cur.execute(
            """INSERT INTO case_events (case_id, from_status, to_status, actor)
               VALUES (%s, %s, %s, 'worker')""",
            (case_id, prev_status, new_status),
        )
        conn.commit()

    log.info("status_transition", case_id=case_id, from_status=prev_status, to_status=new_status)


async def process_case(job: dict) -> None:
    """Process a single case job.

    Day-1 "dumb" version: just moves the case through status transitions
    with fake delays so the UI shows live updates via Supabase Realtime.

    The status flow:  queued → analyzing → ready_for_dispatch

    No mapped_package_code is written (avoids FK constraint issues — see
    module docstring).  Phase 5 will replace this with real mapping +
    confidence routing, and Phase 6 will add the pre-flight rule check
    before ready_for_dispatch.
    """
    case_id = job["case_id"]
    structlog.contextvars.bind_contextvars(case_id=case_id, stage=job.get("stage"))

    log.info("processing_case_start")

    # --- Step 1: mark as analyzing ---
    _set_status(case_id, "analyzing")

    # Fake processing delay so the UI can show the "analyzing" state
    await asyncio.sleep(3)

    # --- Step 2: mark as ready_for_dispatch ---
    # No mapped_package_code, no confidence — those come in Phase 5.
    # The Day 1 exit criteria is: status transitions work end-to-end.
    _set_status(case_id, "ready_for_dispatch")

    log.info("processing_case_done")
    structlog.contextvars.unbind_contextvars("case_id", "stage")
