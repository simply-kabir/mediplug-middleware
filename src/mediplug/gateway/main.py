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

from pathlib import Path

import psycopg
import structlog
from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.staticfiles import StaticFiles
from psycopg.types.json import Json
from psycopg_pool import AsyncConnectionPool
from redis.exceptions import RedisError

from ..config import settings
from ..fraud import validate_abha
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
    check=AsyncConnectionPool.check_connection,
    max_idle=45.0,
    max_lifetime=300.0,
    kwargs={
        "prepare_threshold": None,
        "keepalives": 1,
        "keepalives_idle": 30,
        "keepalives_interval": 10,
        "keepalives_count": 3,
    },
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

UPLOAD_DIR = Path(__file__).resolve().parents[3] / "data" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=str(UPLOAD_DIR)), name="uploads")


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

    # Anti-Fraud Pillar 1: Validate ABHA format, dummy patterns & Verhoeff checksum
    if body.patient.abha_number:
        is_valid, err_msg = validate_abha(body.patient.abha_number)
        if not is_valid:
            log.warning("anti_fraud_abha_rejected", abha=body.patient.abha_number, error=err_msg)
            raise HTTPException(400, f"Anti-Fraud Gatekeeper: {err_msg}")

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


@app.post("/api/v1/cases/{case_id}/upload-document", status_code=200)
async def upload_case_file(
    request: Request,
    case_id: uuid.UUID,
    file: UploadFile = File(...),
    document_type: str = Form(...),
    uploaded_by: str = Form("aarogyamitra"),
    re_enqueue: bool = Form(True),
) -> dict:
    """Upload a real binary document (PDF/image) to a case and optionally re-enqueue for pre-flight rule evaluation."""
    case_str = str(case_id)
    doc_type = document_type.strip()
    if not doc_type:
        raise HTTPException(400, "document_type is required")
    if not file.filename:
        raise HTTPException(400, "Valid file must be provided")

    case_upload_dir = UPLOAD_DIR / case_str
    case_upload_dir.mkdir(parents=True, exist_ok=True)

    safe_filename = Path(file.filename).name
    file_path = case_upload_dir / safe_filename

    contents = await file.read()
    with open(file_path, "wb") as f:
        f.write(contents)

    base = str(request.base_url).rstrip("/")
    file_url = f"{base}/uploads/{case_str}/{safe_filename}"

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

                await cur.execute(
                    """
                    insert into case_documents (case_id, document_type, file_url, file_name)
                    values (%s, %s, %s, %s)
                    """,
                    (case_str, doc_type, file_url, safe_filename),
                )

                if re_enqueue:
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
                        CaseStatus.QUEUED.value if re_enqueue else prev_status,
                        uploaded_by,
                        Json(
                            {
                                "uploaded_documents": [
                                    {
                                        "document_type": doc_type,
                                        "file_url": file_url,
                                        "file_name": safe_filename,
                                        "file_size": len(contents),
                                    }
                                ],
                                "uploaded_by": uploaded_by,
                                "re_enqueue": re_enqueue,
                            }
                        ),
                    ),
                )
            await conn.commit()
    except HTTPException:
        raise
    except psycopg.Error as exc:
        log.error("db_error_on_upload_case_file", case_id=case_str, error=str(exc))
        raise HTTPException(500, "Database error during document upload") from exc

    job_id = None
    if re_enqueue:
        envelope = JobEnvelope(case_id=case_str, stage=stage, trigger="docs_updated")
        try:
            job_id = await enqueue(envelope)
        except Exception as exc:
            log.error("redis_enqueue_failed_on_upload_case_file", case_id=case_str, error=str(exc))
            raise HTTPException(
                503, "Queue unavailable — document saved, retry to re-enqueue"
            ) from exc

    log.info(
        "document_file_uploaded",
        case_id=case_str,
        document_type=doc_type,
        file_name=safe_filename,
        file_url=file_url,
        job_id=job_id,
        re_enqueued=re_enqueue,
    )

    return {
        "case_id": case_str,
        "tracking_ref": tracking_ref,
        "status": CaseStatus.QUEUED.value if re_enqueue else prev_status,
        "document": {
            "document_type": doc_type,
            "file_name": safe_filename,
            "file_url": file_url,
        },
        "job_id": job_id,
    }


@app.post("/api/v1/cases/{case_id}/upload-documents-batch", status_code=200)
async def upload_documents_batch(
    request: Request,
    case_id: uuid.UUID,
    files: list[UploadFile] = File(...),
    document_types: list[str] = Form(...),
    uploaded_by: str = Form("claim_officer"),
) -> dict:
    """Upload multiple documents at once in a single batch, and only trigger rule re-evaluation ONCE."""
    case_str = str(case_id)
    if not files:
        raise HTTPException(400, "At least one file must be provided")

    case_upload_dir = UPLOAD_DIR / case_str
    case_upload_dir.mkdir(parents=True, exist_ok=True)
    base = str(request.base_url).rstrip("/")

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

                uploaded_docs = []
                for idx, file in enumerate(files):
                    safe_filename = Path(file.filename).name if file.filename else f"doc_{idx}_{uuid.uuid4().hex[:6]}.pdf"
                    file_path = case_upload_dir / safe_filename
                    contents = await file.read()
                    with open(file_path, "wb") as f:
                        f.write(contents)

                    doc_type = (document_types[idx] if idx < len(document_types) else document_types[-1]).strip()
                    file_url = f"{base}/uploads/{case_str}/{safe_filename}"

                    await cur.execute(
                        """
                        insert into case_documents (case_id, document_type, file_url, file_name)
                        values (%s, %s, %s, %s)
                        """,
                        (case_str, doc_type, file_url, safe_filename),
                    )
                    uploaded_docs.append({
                        "document_type": doc_type,
                        "file_name": safe_filename,
                        "file_url": file_url,
                        "file_size": len(contents),
                    })

                # Transition to queued for re-evaluation once
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
                        uploaded_by,
                        Json({
                            "uploaded_documents": uploaded_docs,
                            "uploaded_by": uploaded_by,
                            "batch": True,
                        }),
                    ),
                )
            await conn.commit()
    except HTTPException:
        raise
    except psycopg.Error as exc:
        log.error("db_error_on_batch_upload", case_id=case_str, error=str(exc))
        raise HTTPException(500, "Database error during batch document upload") from exc

    envelope = JobEnvelope(case_id=case_str, stage=stage, trigger="docs_updated")
    try:
        job_id = await enqueue(envelope)
    except Exception as exc:
        log.error("redis_enqueue_failed_on_batch_upload", case_id=case_str, error=str(exc))
        raise HTTPException(
            503, "Queue unavailable — documents saved, retry to re-enqueue"
        ) from exc

    log.info("documents_batch_uploaded", case_id=case_str, count=len(uploaded_docs), job_id=job_id)
    return {
        "case_id": case_str,
        "tracking_ref": tracking_ref,
        "status": CaseStatus.QUEUED.value,
        "documents_added": len(uploaded_docs),
        "documents": uploaded_docs,
        "job_id": job_id,
    }


@app.get("/api/v1/cases/{case_id}/documents", status_code=200)
async def list_case_documents(case_id: uuid.UUID) -> list[dict]:
    """List all documents attached to a case."""
    case_str = str(case_id)
    try:
        async with db_pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    select id, document_type, file_url, file_name, uploaded_at
                    from case_documents
                    where case_id = %s
                    order by uploaded_at asc
                    """,
                    (case_str,),
                )
                rows = await cur.fetchall()
                return [
                    {
                        "id": str(r[0]),
                        "document_type": r[1],
                        "file_url": r[2],
                        "file_name": r[3],
                        "uploaded_at": r[4].isoformat() if r[4] else None,
                    }
                    for r in rows
                ]
    except psycopg.Error as exc:
        log.error("db_error_on_list_case_documents", case_id=case_str, error=str(exc))
        raise HTTPException(500, "Database error listing documents") from exc


@app.get("/api/v1/cases/{case_id}", status_code=200)
async def get_case(case_id: str) -> dict:
    """Retrieve full case details by UUID, tracking_ref, or hms_case_ref."""
    for attempt in range(2):
        try:
            async with db_pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        select id, tracking_ref, hms_case_ref, stage, status,
                               patient, encounter, raw_clinical_notes,
                               missing_requirements, mapped_package_code, mapped_package_name,
                               confidence, alternate_codes, payer_correlation_id, error_message,
                               created_at
                        from cases
                        where id::text = %s or tracking_ref = %s or hms_case_ref = %s
                        order by created_at desc
                        limit 1
                        """,
                        (case_id, case_id, case_id),
                    )
                    row = await cur.fetchone()
                    if not row:
                        raise HTTPException(404, "Case not found")

                    (
                        c_id, tracking_ref, hms_case_ref, stage, status,
                        patient, encounter, notes,
                        missing_reqs, pkg_code, pkg_name,
                        confidence, alt_codes, corr_id, err_msg,
                        created_at
                    ) = row

                    await cur.execute(
                        """
                        select id, document_type, file_url, file_name, uploaded_at
                        from case_documents
                        where case_id = %s
                        order by uploaded_at asc
                        """,
                        (c_id,),
                    )
                    doc_rows = await cur.fetchall()
                    documents = [
                        {
                            "id": str(r[0]),
                            "document_type": r[1],
                            "file_url": r[2],
                            "file_name": r[3],
                            "uploaded_at": r[4].isoformat() if r[4] else None,
                        }
                        for r in doc_rows
                    ]

                    return {
                        "id": str(c_id),
                        "tracking_ref": tracking_ref,
                        "hms_case_ref": hms_case_ref,
                        "stage": stage,
                        "status": status,
                        "patient": patient or {},
                        "encounter": encounter or {},
                        "raw_clinical_notes": notes or "",
                        "missing_requirements": missing_reqs or [],
                        "mapped_package_code": pkg_code,
                        "mapped_package_name": pkg_name,
                        "confidence": confidence,
                        "alternate_codes": alt_codes or [],
                        "payer_correlation_id": corr_id,
                        "error_message": err_msg,
                        "created_at": created_at.isoformat() if created_at else None,
                        "documents": documents,
                    }
        except HTTPException:
            raise
        except psycopg.Error as exc:
            if attempt == 0:
                log.warning("db_retry_on_get_case", case_id=case_id, error=str(exc))
                await asyncio.sleep(0.1)
                continue
            log.error("db_error_on_get_case", case_id=case_id, error=str(exc))
            raise HTTPException(500, "Database error retrieving case") from exc


@app.post("/api/v1/cases/{case_id}/manual-dispatch", status_code=200)
async def manual_dispatch(case_id: str, dispatched_by: str = Form("claim_officer")) -> dict:
    """Manually dispatch a case. Re-evaluates rules and dispatches to payer (NHCX).
    Designed for when automatic dispatch failed or worker was shut down and restarted."""
    try:
        async with db_pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    select id, status, stage, tracking_ref, mapped_package_code
                    from cases
                    where id::text = %s or tracking_ref = %s or hms_case_ref = %s
                    limit 1
                    """,
                    (case_id, case_id, case_id),
                )
                case_row = await cur.fetchone()
                if not case_row:
                    raise HTTPException(404, "Case not found")

                c_id, prev_status, stage, tracking_ref, pkg_code = case_row
                case_str = str(c_id)

                if not pkg_code:
                    raise HTTPException(
                        400,
                        "Cannot dispatch case without confirmed package code. Please select and confirm a package code first.",
                    )

                # Set status to queued so worker evaluates pre-flight rules and dispatches
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
                        dispatched_by,
                        Json({"trigger": "manual_dispatch", "action": "manual_dispatch_initiated"}),
                    ),
                )
            await conn.commit()
    except HTTPException:
        raise
    except psycopg.Error as exc:
        log.error("db_error_on_manual_dispatch", case_id=case_id, error=str(exc))
        raise HTTPException(500, "Database error initiating manual dispatch") from exc

    envelope = JobEnvelope(case_id=case_str, stage=stage, trigger="manual_dispatch")
    try:
        job_id = await enqueue(envelope)
    except Exception as exc:
        log.error("redis_enqueue_failed_on_manual_dispatch", case_id=case_str, error=str(exc))
        raise HTTPException(
            503, "Queue unavailable — case queued in DB, retry or start worker"
        ) from exc

    log.info("manual_dispatch_queued", case_id=case_str, job_id=job_id)
    return {
        "case_id": case_str,
        "tracking_ref": tracking_ref,
        "status": CaseStatus.QUEUED.value,
        "message": "Manual dispatch queued. Worker will evaluate pre-flight rules and dispatch to payer.",
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