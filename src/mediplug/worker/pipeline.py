"""
Worker pipeline — Phase 5 version.

Handles semantic code mapping and confidence routing:
  - Re-reads case from Postgres
  - Marks case as 'analyzing' (without duplicate audit rows on retries)
  - If trigger == 'code_confirmed' and mapped_package_code is set, skips mapping
  - Otherwise runs map_notes in a thread (CPU-bound)
  - Confidence routing:
      >= 0.82: auto-accept -> 'ready_for_dispatch'
      0.45 - 0.82: human confirm needed -> 'needs_code_confirmation'
      < 0.45 or empty: unmapped -> 'action_required'
"""

from __future__ import annotations

import asyncio

import psycopg
from psycopg.types.json import Json
import structlog

from ..config import settings
from ..mapping.mapper import map_notes
from ..schemas import CodeCandidate

log = structlog.get_logger()


def _set_status(case_id: str, new_status: str, **extra_fields: object) -> None:
    """Transition a case to a new status in Postgres.

    Writes an audit event to case_events ONLY if status actually changed.
    Values passed via extra_fields are SET on the cases row.
    """
    if extra_fields:
        extra_sets = ", ".join(f"{col} = %s" for col in extra_fields)
        sql = f"UPDATE cases SET status = %s, {extra_sets} WHERE id = %s"
        params: tuple = (new_status, *extra_fields.values(), case_id)
    else:
        sql = "UPDATE cases SET status = %s WHERE id = %s"
        params = (new_status, case_id)

    with psycopg.connect(settings.database_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT status FROM cases WHERE id = %s", (case_id,))
        row = cur.fetchone()
        prev_status = row[0] if row else None

        cur.execute(sql, params)

        # Avoid duplicate audit events if status didn't change (e.g. retries)
        if prev_status != new_status:
            cur.execute(
                """INSERT INTO case_events (case_id, from_status, to_status, actor)
                   VALUES (%s, %s, %s, 'worker')""",
                (case_id, prev_status, new_status),
            )
        conn.commit()

    log.info("status_transition", case_id=case_id, from_status=prev_status, to_status=new_status)


async def process_case(job: dict) -> None:
    """Process a single case job via semantic mapping & confidence routing."""
    case_id = job["case_id"]
    stage = job.get("stage", "preauth")
    trigger = job.get("trigger", "ingest")
    structlog.contextvars.bind_contextvars(case_id=case_id, stage=stage)

    log.info("processing_case_start", trigger=trigger)

    # 1. Transition to analyzing
    _set_status(case_id, "analyzing")

    # 2. Re-read full case from DB for fresh state
    with psycopg.connect(settings.database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT raw_clinical_notes, mapped_package_code, code_confirmed_at
               FROM cases WHERE id = %s""",
            (case_id,),
        )
        row = cur.fetchone()
        if not row:
            log.error("case_not_found", case_id=case_id)
            return
        notes, existing_code, confirmed_at = row

    # 3. Check if already confirmed by human
    if trigger == "code_confirmed" and existing_code and confirmed_at:
        log.info("skipping_mapping_already_confirmed", code=existing_code)
        _set_status(case_id, "ready_for_dispatch")
        log.info("processing_case_done", final_status="ready_for_dispatch")
        structlog.contextvars.unbind_contextvars("case_id", "stage")
        return

    # 4. Run semantic code mapping in a separate thread (CPU-bound)
    candidates: list[CodeCandidate] = await asyncio.to_thread(map_notes, notes, 3)

    if not candidates:
        log.warning("no_code_candidates_found", case_id=case_id)
        _set_status(
            case_id,
            "action_required",
            confidence=None,
            alternate_codes=Json([]),
        )
        structlog.contextvars.unbind_contextvars("case_id", "stage")
        return

    best = candidates[0]
    alternate_json = Json([c.model_dump() for c in candidates])

    # 5. Confidence routing
    if best.confidence >= settings.confidence_auto_accept:
        # High confidence (>= 0.82): Auto-accept
        log.info(
            "auto_accept_package",
            code=best.code,
            name=best.name,
            confidence=best.confidence,
        )
        _set_status(
            case_id,
            "ready_for_dispatch",
            mapped_package_code=best.code,
            mapped_package_name=best.name,
            confidence=best.confidence,
            alternate_codes=alternate_json,
        )
    elif best.confidence >= settings.confidence_floor:
        # Borderline confidence (0.45 - 0.82): Human confirmation needed
        log.info(
            "needs_human_confirmation",
            top_code=best.code,
            confidence=best.confidence,
        )
        # Note: Do NOT set mapped_package_code yet!
        _set_status(
            case_id,
            "needs_code_confirmation",
            confidence=best.confidence,
            alternate_codes=alternate_json,
        )
    else:
        # Low confidence (< 0.45): Flag as action required
        log.warning(
            "confidence_below_floor",
            confidence=best.confidence,
        )
        _set_status(
            case_id,
            "action_required",
            confidence=best.confidence,
            alternate_codes=alternate_json,
        )

    log.info("processing_case_done")
    structlog.contextvars.unbind_contextvars("case_id", "stage")
