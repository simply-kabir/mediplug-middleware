"""Ingest gateway — Phase 3 + Phase 5 confirm-code.

POST /api/v1/cases/ingest and POST /api/v1/cases/{case_id}/confirm-code are
the only things this file exposes. Both do the minimum: validate, write to
Postgres, XADD to Redis, respond. No mapping, no rule checking, no dispatch
— that's the worker's job.

Run it:
    uv run uvicorn mediplug.gateway.main:app --reload --port 8000
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


import secrets
import uuid
from contextlib import asynccontextmanager

import psycopg
import structlog
from fastapi import Depends, FastAPI, Header, HTTPException
from psycopg.types.json import Json
from psycopg_pool import AsyncConnectionPool
from redis.exceptions import RedisError

from ..config import settings
from ..logging import configure
from ..queue import close_redis, enqueue, ensure_group, queue_stats
from ..schemas import (
    CaseStatus,
    ConfirmCodeRequest,
    IngestRequest,
    IngestResponse,
    JobEnvelope,
    QueueStatsResponse,
    RequeueResponse,
    UploadDocumentsRequest,
)

configure()
log = structlog.get_logger()

# A real pool, not one connection per request. A single blocking
# psycopg.connect() call inside an async route stalls the whole event loop
# for the DB round-trip — under concurrent load (ingest AND confirm-code
# calls can both be in flight at once), requests queue up behind each other
# instead of running concurrently. min_size/max_size stay comfortably under
# Supabase's session-pooler client cap. Opened/closed via the lifespan
# below, not at import time, so tests can import this module without a
# live DB.
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
        await close_redis()  # lifespan previously closed only the DB pool, leaking Redis


from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="MediPlug Gateway", version="0.2.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _new_tracking_ref() -> str:
    """Human-readable-ish ref for the HMS side to display/search on. Not the
    primary key — cases.id is."""
    return f"MP-{secrets.token_hex(5).upper()}"


async def _get_by_idempotency_key(cur, idempotency_key: str):
    await cur.execute(
        "select id, tracking_ref, status, stage from cases where idempotency_key = %s",
        (idempotency_key,),
    )
    return await cur.fetchone()


async def _reenqueue_if_queued(case_id: str, stage: str, status: str) -> None:
    """Bug #4 (half 2): a client retrying after a 503-on-enqueue lands in the
    idempotent-replay branch, which used to return 202 without ever calling
    `enqueue` — leaving the case orphaned forever. Re-enqueue here, but only
    while the case is still `queued`: the worker's first act is
    `_set_status(..., "analyzing")`, so a genuinely in-flight case is no
    longer `queued` and this won't double-fire the pipeline."""
    if status != CaseStatus.QUEUED.value:
        return
    try:
        await enqueue(JobEnvelope(case_id=case_id, stage=stage, trigger="ingest"))
    except Exception as exc:
        log.error("redis_enqueue_failed_on_replay", case_id=case_id, error=str(exc))
        raise HTTPException(
            503, "Queue unavailable — case saved, retry to enqueue"
        ) from exc


def _require_admin(
    x_admin_token: str | None = Header(None, alias="X-Admin-Token"),
) -> None:
    """Gate for /admin/*. The app has no other auth and CORS is wide open,
    so an unauthenticated mutating requeue route would be drivable from any
    page on the internet."""
    if not x_admin_token or not secrets.compare_digest(
        x_admin_token, settings.admin_token
    ):
        raise HTTPException(401, "missing or invalid X-Admin-Token")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------

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
                    case_id, tracking_ref, status, existing_stage = existing
                    log.info(
                        "ingest.replay",
                        case_id=str(case_id),
                        idempotency_key=idempotency_key,
                    )
                    # If a prior request saved the row but its enqueue failed
                    # (503), the case is still `queued` — retry the enqueue now.
                    await _reenqueue_if_queued(str(case_id), existing_stage, status)
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
                    # caller. Requires idempotency_key UNIQUE in schema.sql.
                    await conn.rollback()
                    async with conn.cursor() as retry_cur:
                        existing = await _get_by_idempotency_key(
                            retry_cur, idempotency_key
                        )
                    if not existing:
                        raise
                    case_id, tracking_ref, status, existing_stage = existing
                    await _reenqueue_if_queued(str(case_id), existing_stage, status)
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

    envelope = JobEnvelope(case_id=str(case_id), stage=body.stage, trigger="ingest")
    try:
        job_id = await enqueue(envelope)
    except Exception as exc:
        # The row + its unique idempotency key are already committed, so
        # raising here is clean: the caller gets a truthful 5xx (not a
        # cheerful 202 for a case that will never be picked up), and a
        # retry with the same key hits the replay branch, which re-enqueues.
        log.error("redis_enqueue_failed", case_id=str(case_id), error=str(exc))
        raise HTTPException(
            503, "Queue unavailable — case saved, retry to enqueue"
        ) from exc

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


# ---------------------------------------------------------------------------
# Confirm code (human-in-the-loop, Phase 5)
# ---------------------------------------------------------------------------

@app.post("/api/v1/cases/{case_id}/confirm-code", status_code=200)
async def confirm_code(case_id: uuid.UUID, body: ConfirmCodeRequest) -> dict:
    """Aarogyamitra confirms or manually selects a package code. Validates
    the code exists in `packages` (prevents an FK violation), updates the
    case, writes an audit event, and re-enqueues with
    trigger='code_confirmed' so the worker promotes it without re-mapping."""
    case_str = str(case_id)

    try:
        async with db_pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "select name from packages where code = %s", (body.code,)
                )
                pkg_row = await cur.fetchone()
                if not pkg_row:
                    raise HTTPException(
                        400,
                        f"Package code '{body.code}' not found in packages master",
                    )
                package_name = pkg_row[0]

                await cur.execute(
                    "select status, stage, tracking_ref from cases where id = %s",
                    (case_str,),
                )
                case_row = await cur.fetchone()
                if not case_row:
                    raise HTTPException(404, "Case not found")
                prev_status, stage, tracking_ref = case_row

                await cur.execute(
                    """
                    update cases
                    set mapped_package_code = %s,
                        mapped_package_name = %s,
                        code_confirmed_by = %s,
                        code_confirmed_at = now(),
                        status = %s
                    where id = %s
                    """,
                    (
                        body.code,
                        package_name,
                        body.confirmed_by,
                        CaseStatus.QUEUED.value,
                        case_str,
                    ),
                )

                await cur.execute(
                    """
                    insert into case_events
                        (case_id, from_status, to_status, actor, detail)
                    values (%s, %s, %s, 'aarogyamitra', %s)
                    """,
                    (
                        case_str,
                        prev_status,
                        CaseStatus.QUEUED.value,
                        Json(
                            {
                                "confirmed_code": body.code,
                                "confirmed_by": body.confirmed_by,
                            }
                        ),
                    ),
                )
            await conn.commit()
    except HTTPException:
        raise
    except psycopg.Error as exc:
        log.error("db_error_on_confirm_code", case_id=case_str, error=str(exc))
        raise HTTPException(500, "Database error during code confirmation") from exc

    envelope = JobEnvelope(case_id=case_str, stage=stage, trigger="code_confirmed")
    try:
        job_id = await enqueue(envelope)
    except Exception as exc:
        # No idempotency key on this route — recovery is the caller
        # re-POSTing (which appends a duplicate case_events row each time,
        # documented in sih/DEMO_RUNBOOK.md). Still better than a false 200.
        log.error("redis_enqueue_failed_on_confirm", case_id=case_str, error=str(exc))
        raise HTTPException(
            503, "Queue unavailable — code saved, retry to re-enqueue"
        ) from exc

    log.info(
        "code_confirmed",
        case_id=case_str,
        code=body.code,
        confirmed_by=body.confirmed_by,
        job_id=job_id,
    )

    return {
        "case_id": case_str,
        "tracking_ref": tracking_ref,
        "status": CaseStatus.QUEUED.value,
        "mapped_package_code": body.code,
        "mapped_package_name": package_name,
    }


# ---------------------------------------------------------------------------
# Upload documents (human-in-the-loop, Phase 6)
# ---------------------------------------------------------------------------

@app.post("/api/v1/cases/{case_id}/documents", status_code=200)
async def upload_documents(
    case_id: uuid.UUID,
    body: UploadDocumentsRequest,
) -> dict:
    """Upload one or more documents to an existing case and re-enqueue for rule evaluation.

    1. Validates the case exists (404 if not found).
    2. Inserts document row(s) into case_documents.
    3. Transitions status back to 'queued'.
    4. Writes a case_events audit row.
    5. Re-enqueues a JobEnvelope with trigger='docs_updated'.
    """
    case_str = str(case_id)
    if not body.documents:
        raise HTTPException(400, "Must provide at least one document to upload")

    try:
        async with db_pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "select status, stage, tracking_ref from cases where id = %s",
                    (case_str,),
                )
                case_row = await cur.fetchone()
                if not case_row:
                    raise HTTPException(404, "Case not found")
                prev_status, stage, tracking_ref = case_row

                for doc in body.documents:
                    await cur.execute(
                        """
                        insert into case_documents (case_id, document_type, file_url)
                        values (%s, %s, %s)
                        """,
                        (case_str, doc.document_type, doc.file_url),
                    )

                await cur.execute(
                    "update cases set status = %s where id = %s",
                    (CaseStatus.QUEUED.value, case_str),
                )

                await cur.execute(
                    """
                    insert into case_events
                        (case_id, from_status, to_status, actor, detail)
                    values (%s, %s, %s, %s, %s)
                    """,
                    (
                        case_str,
                        prev_status,
                        CaseStatus.QUEUED.value,
                        body.uploaded_by,
                        Json(
                            {
                                "uploaded_documents": [
                                    {"document_type": d.document_type, "file_url": d.file_url}
                                    for d in body.documents
                                ],
                                "uploaded_by": body.uploaded_by,
                            }
                        ),
                    ),
                )
            await conn.commit()
    except HTTPException:
        raise
    except psycopg.Error as exc:
        log.error("db_error_on_upload_documents", case_id=case_str, error=str(exc))
        raise HTTPException(500, "Database error during document upload") from exc

    envelope = JobEnvelope(case_id=case_str, stage=stage, trigger="docs_updated")
    try:
        job_id = await enqueue(envelope)
    except Exception as exc:
        # No idempotency key here either — recovery is the caller re-POSTing.
        log.error("redis_enqueue_failed_on_upload_docs", case_id=case_str, error=str(exc))
        raise HTTPException(
            503, "Queue unavailable — documents saved, retry to re-enqueue"
        ) from exc

    log.info(
        "documents_uploaded",
        case_id=case_str,
        count=len(body.documents),
        uploaded_by=body.uploaded_by,
        job_id=job_id,
    )

    return {
        "case_id": case_str,
        "tracking_ref": tracking_ref,
        "status": CaseStatus.QUEUED.value,
        "documents_added": len(body.documents),
        "job_id": job_id,
    }


# ---------------------------------------------------------------------------
# Admin (Phase 9.3) — gated behind X-Admin-Token
# ---------------------------------------------------------------------------

@app.get("/admin/queue", response_model=QueueStatsResponse)
async def admin_queue(_: None = Depends(_require_admin)) -> QueueStatsResponse:
    """Live snapshot of the Redis Streams work queue + DLQ, for the ops
    console (guide §11.2). 503 if the Redis backend is unreachable."""
    try:
        stats = await queue_stats()
    except RedisError as exc:
        log.error("admin_queue_redis_error", error=str(exc))
        raise HTTPException(503, "Queue backend unavailable") from exc
    return QueueStatsResponse(**stats)


@app.post(
    "/admin/cases/{case_id}/requeue",
    response_model=RequeueResponse,
    status_code=202,
)
async def admin_requeue(
    case_id: uuid.UUID, _: None = Depends(_require_admin)
) -> RequeueResponse:
    """On-stage escape hatch for a stuck case: re-enqueue a JobEnvelope with
    the (previously defined but unused) trigger='manual_retry', and record
    an admin-actor case_events row. Does not mutate cases.status — the
    worker re-drives from whatever state the row is in."""
    case_str = str(case_id)
    try:
        async with db_pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "select status, stage from cases where id = %s", (case_str,)
                )
                row = await cur.fetchone()
                if not row:
                    raise HTTPException(404, "Case not found")
                status, stage = row

                await cur.execute(
                    """
                    insert into case_events (case_id, from_status, to_status, actor, detail)
                    values (%s, %s, %s, 'admin', %s)
                    """,
                    (case_str, status, status, Json({"trigger": "manual_retry"})),
                )
            await conn.commit()
    except HTTPException:
        raise
    except psycopg.Error as exc:
        log.error("db_error_on_requeue", case_id=case_str, error=str(exc))
        raise HTTPException(500, "Database error during requeue") from exc

    envelope = JobEnvelope(case_id=case_str, stage=stage, trigger="manual_retry")
    try:
        job_id = await enqueue(envelope)
    except Exception as exc:
        log.error("redis_enqueue_failed_on_requeue", case_id=case_str, error=str(exc))
        raise HTTPException(503, "Queue unavailable — case not requeued") from exc

    log.info("admin_requeue", case_id=case_str, job_id=job_id, status=status)
    return RequeueResponse(case_id=case_str, status=CaseStatus(status), job_id=job_id)