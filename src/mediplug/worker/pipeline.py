"""
Worker pipeline — Phase 6 version.

Handles semantic code mapping, pre-flight rule checking, and confidence routing:
  - Marks case as 'analyzing'
  - If trigger in ('code_confirmed', 'docs_updated'):
      Skips mapping and re-evaluates pre-flight rules for the confirmed code
  - Otherwise runs map_notes in a worker thread (CPU-bound)
  - If package code resolved (auto-accept):
      Evaluates package requirements[stage] against uploaded case_documents
      -> If satisfied: 'ready_for_dispatch' (missing_requirements=[])
      -> If missing docs: 'action_required' (missing_requirements=[...])
  - If confidence 0.45 - 0.82: 'needs_code_confirmation'
  - If confidence < 0.45: 'action_required' (unmapped)

All Postgres access in this module uses plain blocking psycopg (not the
async pool from gateway/main.py — this is a separate worker process).
Every call is routed through asyncio.to_thread() so a DB round-trip
never stalls the worker's event loop, which would otherwise delay XACK/
XAUTOCLAIM/heartbeat handling for every OTHER job in flight, not just
this one. See sih/decisions.md for why this matters (same failure mode
almost shipped once already in gateway/main.py, Phase 3).
"""

from __future__ import annotations

import asyncio
import psycopg
from psycopg.types.json import Json
import structlog

from ..config import settings
from ..fraud import (
    IntegrityCheckResult,
    check_concurrent_admission,
    evaluate_clinical_integrity,
)
from ..mapping.mapper import map_notes
from ..rules.engine import evaluate, RuleEvaluation
from ..schemas import CodeCandidate
from .finalize import finalize_case

log = structlog.get_logger()


def _set_status(case_id: str, new_status: str, **extra_fields: object) -> None:
    """Transition a case to a new status in Postgres. Synchronous/blocking —
    callers MUST wrap this in asyncio.to_thread(), never call it directly
    from async code.

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


def _check_rules_for_code(case_id: str, stage: str, package_code: str) -> RuleEvaluation:
    """Fetch package requirements and uploaded docs, then evaluate.
    Synchronous/blocking — callers MUST wrap this in asyncio.to_thread()."""
    with psycopg.connect(settings.database_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT requirements FROM packages WHERE code = %s", (package_code,))
        pkg_row = cur.fetchone()
        stage_requirements = (pkg_row[0] or {}).get(stage, []) if pkg_row else []

        cur.execute("SELECT document_type FROM case_documents WHERE case_id = %s", (case_id,))
        doc_rows = cur.fetchall()
        uploaded_doc_types = [r[0] for r in doc_rows]

    return evaluate(stage_requirements, uploaded_doc_types)


def _check_integrity_and_rules(
    case_id: str,
    stage: str,
    package_code: str,
    patient: dict,
    encounter: dict,
) -> tuple[IntegrityCheckResult, RuleEvaluation]:
    """Check clinical integrity / anti-fraud rules first, then pre-flight document rules.
    Synchronous/blocking — callers MUST wrap this in asyncio.to_thread()."""
    with psycopg.connect(settings.database_url) as conn, conn.cursor() as cur:
        # 1. Anti-fraud & clinical integrity check
        integrity_res = evaluate_clinical_integrity(
            cur, case_id, patient, encounter, package_code
        )
        if not integrity_res.passed:
            cur.execute(
                """
                INSERT INTO case_events (case_id, from_status, to_status, actor, detail)
                VALUES (%s, 'analyzing', 'action_required', 'anti_fraud_engine', %s)
                """,
                (case_id, Json(integrity_res.detail or {})),
            )
            conn.commit()
            dummy_rule = RuleEvaluation(
                passed=False,
                missing_requirements=[
                    {
                        "human_label": f"FRAUD ALERT: {integrity_res.message}",
                        "satisfied": False,
                        "violation_type": integrity_res.violation_type,
                    }
                ],
            )
            return integrity_res, dummy_rule

        # 2. Pre-flight document check
        cur.execute("SELECT requirements FROM packages WHERE code = %s", (package_code,))
        pkg_row = cur.fetchone()
        stage_requirements = (pkg_row[0] or {}).get(stage, []) if pkg_row else []

        cur.execute("SELECT document_type FROM case_documents WHERE case_id = %s", (case_id,))
        doc_rows = cur.fetchall()
        uploaded_doc_types = [r[0] for r in doc_rows]

    rule_res = evaluate(stage_requirements, uploaded_doc_types)
    return integrity_res, rule_res


def _read_case_state(
    case_id: str,
) -> tuple[str, str | None, object, dict, dict] | None:
    """Fetch the fields process_case needs fresh from the DB. Synchronous/
    blocking — callers MUST wrap this in asyncio.to_thread(). Returns None
    if the case doesn't exist."""
    with psycopg.connect(settings.database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT raw_clinical_notes, mapped_package_code, code_confirmed_at, patient, encounter
               FROM cases WHERE id = %s""",
            (case_id,),
        )
        return cur.fetchone()


def _check_collision_db(cid: str, pat: dict, enc: dict) -> tuple[bool, dict | None]:
    if not pat and not enc:
        return True, None
    with psycopg.connect(settings.database_url) as conn, conn.cursor() as cur:
        passed, detail = check_concurrent_admission(cur, cid, pat, enc)
        if not passed and detail:
            cur.execute(
                """
                INSERT INTO case_events (case_id, from_status, to_status, actor, detail)
                VALUES (%s, 'analyzing', 'action_required', 'anti_fraud_engine', %s)
                """,
                (cid, Json(detail)),
            )
            conn.commit()
        return passed, detail


async def process_case(job: dict) -> None:
    """Process a single case job via semantic mapping, rule engine & confidence routing."""
    case_id = job["case_id"]
    stage = job.get("stage", "preauth")
    trigger = job.get("trigger", "ingest")

    # bound_contextvars (not bind_ + manual unbinds): it restores prior
    # values on exit AND covers the exception path, so job_failed /
    # job_dead_lettered lines no longer carry the *previous* case's id
    # (bug #5). Not clear_contextvars() — that would wipe anything a caller
    # bound above process_case.
    with structlog.contextvars.bound_contextvars(case_id=case_id, stage=stage):
        log.info("processing_case_start", trigger=trigger)

        # 1. Transition to analyzing
        await asyncio.to_thread(_set_status, case_id, "analyzing")

        # 2. Re-read full case from DB for fresh state
        row = await asyncio.to_thread(_read_case_state, case_id)
        if not row:
            log.error("case_not_found", case_id=case_id)
            return
        if len(row) == 5:
            notes, existing_code, confirmed_at, patient, encounter = row
        else:
            notes, existing_code, confirmed_at = row[:3]
            patient, encounter = {}, {}

        collision_pass, collision_detail = await asyncio.to_thread(
            _check_collision_db, case_id, patient, encounter
        )
        if not collision_pass and collision_detail:
            log.warning("ghost_admission_collision_detected", case_id=case_id, detail=collision_detail)
            fraud_missing = [
                {
                    "human_label": f"FRAUD ALERT: {collision_detail['message']}",
                    "satisfied": False,
                    "violation_type": collision_detail.get("violation_type", "concurrent_admission_collision"),
                }
            ]
            await asyncio.to_thread(
                _set_status,
                case_id,
                "action_required",
                missing_requirements=Json(fraud_missing),
            )
            log.info("processing_case_done", final_status="action_required")
            return

        # 3. Check if trigger is code_confirmed, docs_updated, or manual_dispatch (skip mapping, evaluate integrity & rules)
        if trigger in ("code_confirmed", "docs_updated", "manual_dispatch", "manual_retry") and existing_code:
            log.info("skipping_mapping_running_rules", trigger=trigger, code=existing_code)
            integrity_res, rule_res = await asyncio.to_thread(
                _check_integrity_and_rules, case_id, stage, existing_code, patient, encounter
            )
            if not integrity_res.passed:
                log.warning("anti_fraud_violation_blocked", case_id=case_id, violation=integrity_res.violation_type)
                await asyncio.to_thread(
                    _set_status,
                    case_id,
                    "action_required",
                    missing_requirements=Json(rule_res.missing_requirements),
                )
                log.info("processing_case_done", final_status="action_required")
                return

            if rule_res.passed:
                await asyncio.to_thread(
                    _set_status, case_id, "ready_for_dispatch", missing_requirements=Json([])
                )
                # Phase 7 + 8: build FHIR bundle, dispatch to payer, -> submitted.
                # Re-raises on failure so consumer.py runs its retry -> DLQ path.
                await finalize_case(case_id)
            else:
                await asyncio.to_thread(
                    _set_status,
                    case_id,
                    "action_required",
                    missing_requirements=Json(rule_res.missing_requirements),
                )
            log.info(
                "processing_case_done",
                final_status="ready_for_dispatch" if rule_res.passed else "action_required",
            )
            return

        # 4. Run semantic code mapping in a separate thread (CPU-bound)
        candidates: list[CodeCandidate] = await asyncio.to_thread(map_notes, notes, 3)

        if not candidates:
            log.warning("no_code_candidates_found", case_id=case_id)
            await asyncio.to_thread(
                _set_status,
                case_id,
                "action_required",
                confidence=None,
                alternate_codes=Json([]),
                missing_requirements=Json([]),
            )
            return

        best = candidates[0]
        alternate_json = Json([c.model_dump() for c in candidates])

        # 5. Confidence routing + Anti-fraud & Pre-flight rules
        if best.confidence >= settings.confidence_auto_accept:
            # High confidence (>= 0.82): Auto-accept code, then check integrity and rules
            log.info(
                "auto_accept_package",
                code=best.code,
                name=best.name,
                confidence=best.confidence,
            )
            integrity_res, rule_res = await asyncio.to_thread(
                _check_integrity_and_rules, case_id, stage, best.code, patient, encounter
            )
            if not integrity_res.passed:
                log.warning("anti_fraud_violation_blocked", case_id=case_id, violation=integrity_res.violation_type)
                await asyncio.to_thread(
                    _set_status,
                    case_id,
                    "action_required",
                    mapped_package_code=best.code,
                    mapped_package_name=best.name,
                    confidence=best.confidence,
                    alternate_codes=alternate_json,
                    missing_requirements=Json(rule_res.missing_requirements),
                )
                log.info("processing_case_done", final_status="action_required")
                return

            target_status = "ready_for_dispatch" if rule_res.passed else "action_required"
            await asyncio.to_thread(
                _set_status,
                case_id,
                target_status,
                mapped_package_code=best.code,
                mapped_package_name=best.name,
                confidence=best.confidence,
                alternate_codes=alternate_json,
                missing_requirements=Json(rule_res.missing_requirements),
            )
            if rule_res.passed:
                # Phase 7 + 8: build FHIR bundle, dispatch to payer, -> submitted.
                # Re-raises on failure so consumer.py runs its retry -> DLQ path.
                await finalize_case(case_id)
        elif best.confidence >= settings.confidence_floor:
            # Borderline confidence (0.45 - 0.82): Human confirmation needed
            log.info(
                "needs_human_confirmation",
                top_code=best.code,
                confidence=best.confidence,
            )
            # Note: Do NOT set mapped_package_code yet!
            await asyncio.to_thread(
                _set_status,
                case_id,
                "needs_code_confirmation",
                confidence=best.confidence,
                alternate_codes=alternate_json,
                missing_requirements=Json([]),
            )
        else:
            # Low confidence (< 0.45): Flag as action required
            log.warning(
                "confidence_below_floor",
                confidence=best.confidence,
            )
            await asyncio.to_thread(
                _set_status,
                case_id,
                "action_required",
                confidence=best.confidence,
                alternate_codes=alternate_json,
                missing_requirements=Json([]),
            )

        log.info("processing_case_done")