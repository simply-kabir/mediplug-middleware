"""
Phase 7 + 8 Part B — case finalization.

The whole of "Part B" for both phases, in one Shubh-owned module so that
pipeline.py (Kabir-owned) needs exactly one line:

    from .finalize import finalize_case
    await finalize_case(case_id)      # after Phase 6 rules pass

Flow (guide §8.2):

    ready_for_dispatch
      → build_claim_bundle + validate        (Phase 7)  → cases.fhir_bundle
      → dispatching
      → get_dispatcher().submit(bundle, stage) (Phase 8)
      → submitted                (payer accepted)
        / dispatch_failed        (payer rejected or transport error)

Failure model matches the rest of the pipeline: record `error_message`
(and the dispatch response, if we got one) FIRST, then re-raise so
`worker/consumer.py` runs its retry → DLQ path. Nothing is ever swallowed.

Duplicate-dispatch guard (Phase 9.4): a re-delivered job used to re-POST to
the payer and let `_persist_dispatch` overwrite `payer_correlation_id`,
orphaning the first submission so it could never be adjudicated back.
`finalize_case` now short-circuits when `cases.payer_correlation_id` is
already set. If the row is still `dispatching` (the crash window between
`_persist_dispatch` and the `submitted` transition), it heals forward to
`submitted` before returning.

Retry note: a retried job re-runs `finalize_case` from the top. Rebuilding
the bundle is pure and cheap; the `UPDATE`s are idempotent. Full
transactional rework is still deferred Phase 9 tech debt.
"""

from __future__ import annotations

import asyncio

import psycopg
import structlog
from psycopg.types.json import Json
from pydantic import ValidationError

from ..config import settings
from ..dispatch import Dispatcher, get_dispatcher
from ..fhir import build_claim_bundle, validate

log = structlog.get_logger()


def _connect() -> psycopg.Connection:
    # Sync connect per call — same pattern (and same Phase 9 tech debt) as
    # pipeline._set_status. prepare_threshold=None for the Supabase pooler.
    return psycopg.connect(settings.database_url, prepare_threshold=None)


def _load_inputs(case_id: str) -> tuple[dict, dict, list[dict], str]:
    """Every DB read Phase 7/8 needs. Raises LookupError if the case or its
    mapped package is missing (→ job fails loudly, not silently)."""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT patient, encounter, stage, mapped_package_code "
            "FROM cases WHERE id = %s",
            (case_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise LookupError(f"case {case_id} not found")
        patient, encounter, stage, package_code = row
        if not package_code:
            raise LookupError(f"case {case_id} has no mapped_package_code")

        cur.execute(
            "SELECT code, name, amount, icd_code FROM packages WHERE code = %s",
            (package_code,),
        )
        prow = cur.fetchone()
        if prow is None:
            raise LookupError(f"mapped package {package_code} not in packages table")
        pcode, pname, pamount, picd = prow

        cur.execute(
            "SELECT document_type, file_url, file_name FROM case_documents "
            "WHERE case_id = %s ORDER BY uploaded_at",
            (case_id,),
        )
        documents = [
            {"document_type": dt, "file_url": fu, "file_name": fn}
            for dt, fu, fn in cur.fetchall()
        ]

    case = {"patient": patient, "encounter": encounter}
    package = {"code": pcode, "name": pname, "amount": pamount, "icd_code": picd}
    return case, package, documents, (stage or "preauth")


def _dispatch_state(case_id: str) -> tuple[str | None, str]:
    """(payer_correlation_id, status) for the Phase 9.4 duplicate-dispatch
    guard. A dedicated seam, NOT a widening of `_load_inputs`: that returns
    a fixed 4-tuple which `tests/test_finalize.py`'s `wired` fixture
    replaces wholesale, so changing its arity breaks all six existing tests.
    """
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT payer_correlation_id, status FROM cases WHERE id = %s", (case_id,)
        )
        row = cur.fetchone()
        if row is None:
            raise LookupError(f"case {case_id} not found")
        return row[0], row[1]


def _transition(case_id: str, new_status: str, **fields: object) -> None:
    """Local mirror of pipeline._set_status — kept here so this module does
    not import a private helper from Kabir's file. Writes a case_events row
    only when the status actually changes (retry-safe)."""
    sets = "status = %s"
    params: list = [new_status]
    for col, val in fields.items():
        sets += f", {col} = %s"
        params.append(val)
    params.append(case_id)

    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT status FROM cases WHERE id = %s", (case_id,))
        prev = cur.fetchone()
        prev_status = prev[0] if prev else None

        cur.execute(f"UPDATE cases SET {sets} WHERE id = %s", params)
        if prev_status != new_status:
            cur.execute(
                "INSERT INTO case_events (case_id, from_status, to_status, actor) "
                "VALUES (%s, %s, %s, 'worker')",
                (case_id, prev_status, new_status),
            )
        conn.commit()

    log.info(
        "status_transition", case_id=case_id, from_status=prev_status, to_status=new_status
    )


def _record_error(case_id: str, message: str) -> None:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE cases SET error_message = %s WHERE id = %s", (message, case_id)
        )
        conn.commit()


def _persist_bundle(case_id: str, bundle: dict) -> None:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE cases SET fhir_bundle = %s WHERE id = %s", (Json(bundle), case_id)
        )
        conn.commit()


def _persist_dispatch(case_id: str, bundle: dict, result) -> None:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE cases SET dispatch_request = %s, dispatch_response = %s, "
            "payer_correlation_id = %s WHERE id = %s",
            (Json(bundle), Json(result.raw_response), result.correlation_id, case_id),
        )
        conn.commit()


async def finalize_case(case_id: str, *, dispatcher: Dispatcher | None = None) -> None:
    """Phase 7 + 8 Part B. Call once, after Phase 6 rules pass, with the
    case at (or entering) `ready_for_dispatch`.

    `dispatcher` is an injection seam for tests; production leaves it None
    and `get_dispatcher()` resolves it from `settings.dispatch_mode`.

    Every sync `psycopg` helper below is routed through `asyncio.to_thread`
    so a blocking DB round-trip does not stall the worker's event loop for
    the whole ~93s dispatch — the same treatment `pipeline.py` gives its
    equivalents. (Test fixtures patch these seams with sync lambdas, which
    run fine under `to_thread`.)
    """
    # ---- Phase 9.4: duplicate-dispatch guard ---------------------------
    # The predicate is `payer_correlation_id IS NOT NULL` — nothing more. A
    # status allow-list would miss the exact crash window drill 1 tests:
    # between _persist_dispatch and _transition(..., "submitted") the row is
    # `dispatching` WITH the correlation id already set. Guard on the id
    # alone; when the row is stuck at `dispatching`, heal it forward.
    corr_id, current_status = await asyncio.to_thread(_dispatch_state, case_id)
    if corr_id is not None:
        log.info(
            "finalize_skipped",
            case_id=case_id,
            reason="already_dispatched",
            status=current_status,
        )
        if current_status == "dispatching":
            await asyncio.to_thread(_transition, case_id, "submitted")
        return

    case, package, documents, stage = await asyncio.to_thread(_load_inputs, case_id)

    # ---- Phase 7: build + validate + persist -----------------------------
    bundle = build_claim_bundle(case, package, documents, stage)
    try:
        validate(bundle)
    except ValidationError as exc:
        log.error("fhir_validation_failed", case_id=case_id, error=str(exc))
        await asyncio.to_thread(_record_error, case_id, f"FHIR validation failed: {exc}")
        raise
    await asyncio.to_thread(_persist_bundle, case_id, bundle)
    log.info("fhir_bundle_built", case_id=case_id, entries=len(bundle["entry"]))

    # ---- Phase 8: dispatch ----------------------------------------------
    await asyncio.to_thread(_transition, case_id, "dispatching")
    try:
        result = await (dispatcher or get_dispatcher()).submit(bundle, stage)
    except Exception as exc:
        log.error("dispatch_error", case_id=case_id, error=str(exc))
        await asyncio.to_thread(_record_error, case_id, f"dispatch error: {exc}")
        await asyncio.to_thread(_transition, case_id, "dispatch_failed")
        raise

    await asyncio.to_thread(_persist_dispatch, case_id, bundle, result)

    if not result.accepted:
        log.warning("dispatch_rejected", case_id=case_id, error=result.error)
        await asyncio.to_thread(_record_error, case_id, f"dispatch rejected: {result.error}")
        await asyncio.to_thread(_transition, case_id, "dispatch_failed")
        raise RuntimeError(f"dispatch rejected: {result.error}")

    await asyncio.to_thread(_transition, case_id, "submitted")
    log.info("dispatch_submitted", case_id=case_id, correlation_id=result.correlation_id)
