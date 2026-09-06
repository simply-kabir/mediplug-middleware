"""Ingest gateway — Phase 3.

POST /api/v1/cases/ingest is the only thing the HMS talks to, and it does
exactly four things: validate, insert into Postgres, XADD to Redis, respond.
No code mapping, no rule checking, no dispatch — that's the worker's job
(Phase 4+). Keep this file thin — IngestResponse promises callers <50ms,
which only holds if nothing "smart" happens in here.

Run it:
    uv run uvicorn mediplug.gateway.main:app --reload --port 8000
"""

from __future__ import annotations

import secrets
import uuid
from contextlib import asynccontextmanager

import psycopg
import structlog
from fastapi import FastAPI, Header, HTTPException
from psycopg.types.json import Json
from psycopg_pool import AsyncConnectionPool

from ..config import settings
from ..logging import configure
from ..queue import enqueue, ensure_group
from ..schemas import CaseStatus, IngestRequest, IngestResponse, JobEnvelope

configure()
log = structlog.get_logger()

# A real pool, not one connection per request. A single blocking
# psycopg.connect() call inside an async route stalls the whole event loop
# for the DB round-trip — under concurrent load, requests queue up behind
# each other instead of running concurrently, which breaks the <50ms
# promise in IngestResponse's docstring. min_size/max_size stay comfortably
# under Supabase's session-pooler client cap. Opened/closed via the
# lifespan below, not at import time, so tests can import this module
# without a live DB.
db_pool = AsyncConnectionPool(
    settings.database_url,
    min_size=2,
    max_size=8,
    kwargs={"prepare_threshold": None},
    open=False,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db_pool.open()
    await ensure_group()
    log.info("gateway_started")
    try:
        yield
    finally:
        await db_pool.close()


app = FastAPI(title="MediPlug Ingest Gateway", lifespan=lifespan)


def _new_tracking_ref() -> str:
    """Human-readable-ish ref for the HMS side to display/search on. Not the
    primary key — cases.id is. A collision is astronomically unlikely at
    hackathon scale, and the unique constraint on the column would surface
    one anyway."""
    return f"MP-{secrets.token_hex(5).upper()}"


async def _get_by_idempotency_key(cur, idempotency_key: str):
    await cur.execute(
        "select id, tracking_ref, status from cases where idempotency_key = %s",
        (idempotency_key,),
    )
    return await cur.fetchone()


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/api/v1/cases/ingest", response_model=IngestResponse, status_code=202)
async def ingest(
    body: IngestRequest,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
) -> IngestResponse:
    if not idempotency_key.strip():
        raise HTTPException(400, "Idempotency-Key header must not be blank")

    try:
        async with db_pool.connection() as conn:
            async with conn.cursor() as cur:
                # Idempotent replay: same key -> same answer, no new row, no
                # second XADD. This is what makes HMS-side retries safe.
                existing = await _get_by_idempotency_key(cur, idempotency_key)
                if existing:
                    case_id, tracking_ref, status = existing
                    log.info(
                        "ingest.replay",
                        case_id=str(case_id),
                        idempotency_key=idempotency_key,
                    )
                    return IngestResponse(
                        case_id=str(case_id),
                        status=CaseStatus(status),
                        tracking_ref=tracking_ref,
                    )

                case_id = uuid.uuid4()
                tracking_ref = _new_tracking_ref()

                try:
                    await cur.execute(
                        """
                        insert into cases
                            (id, tracking_ref, idempotency_key, hms_case_ref, stage,
                             status, patient, encounter, raw_clinical_notes)
                        values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            case_id,
                            tracking_ref,
                            idempotency_key,
                            body.hms_case_ref,
                            body.stage,
                            CaseStatus.QUEUED.value,
                            Json(body.patient.model_dump(mode="json")),
                            Json(body.encounter.model_dump(mode="json")),
                            body.clinical_notes,
                        ),
                    )
                except psycopg.errors.UniqueViolation:
                    # Two requests with the same Idempotency-Key raced past
                    # the check above — whoever lost the insert just reads
                    # back what the winner wrote, instead of erroring the
                    # caller. This ONLY works because idempotency_key has a
                    # UNIQUE constraint in schema.sql — confirm that's there.
                    await conn.rollback()
                    async with conn.cursor() as retry_cur:
                        existing = await _get_by_idempotency_key(
                            retry_cur, idempotency_key
                        )
                    if not existing:
                        raise
                    case_id, tracking_ref, status = existing
                    return IngestResponse(
                        case_id=str(case_id),
                        status=CaseStatus(status),
                        tracking_ref=tracking_ref,
                    )

                if body.documents:
                    await cur.executemany(
                        """
                        insert into case_documents
                            (case_id, document_type, file_url)
                        values (%s, %s, %s)
                        """,
                        [
                            (case_id, d.document_type, d.file_url)
                            for d in body.documents
                        ],
                    )

                await cur.execute(
                    """
                    insert into case_events
                        (case_id, to_status, actor, detail)
                    values (%s, %s, 'gateway', %s)
                    """,
                    (
                        case_id,
                        CaseStatus.QUEUED.value,
                        Json({"trigger": "ingest"}),
                    ),
                )
            await conn.commit()
    except psycopg.Error as exc:
        log.error("db_error_on_ingest", error=str(exc))
        raise HTTPException(500, "Database error during ingest") from exc

    # Deliberately thin payload — the worker re-reads the full case from
    # Postgres, so this call never blocks on anything but the DB write above.
    envelope = JobEnvelope(case_id=str(case_id), stage=body.stage, trigger="ingest")
    try:
        job_id = await enqueue(envelope)
    except Exception as exc:
        # The case row is already committed — it'll sit as 'queued' until
        # someone re-enqueues it. Log loudly but don't fail the HTTP
        # response, because the data is safe in Postgres.
        log.error("redis_enqueue_failed", case_id=str(case_id), error=str(exc))
        job_id = None

    log.info(
        "ingest.accepted",
        case_id=str(case_id),
        tracking_ref=tracking_ref,
        stage=body.stage,
        job_id=job_id,
    )
    return IngestResponse(
        case_id=str(case_id), status=CaseStatus.QUEUED, tracking_ref=tracking_ref
    )