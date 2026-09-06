"""
Phase 0 contracts.

These are the shapes every other service (HMS mock, Aarogyamitra UI,
worker, gateway) agrees on. Get explicit sign-off from the team on this
file before writing pipeline code — changing it later means renegotiating
with everyone downstream.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# 2.1 Status state machine
# ---------------------------------------------------------------------------


class CaseStatus(StrEnum):
    QUEUED = "queued"
    ANALYZING = "analyzing"
    NEEDS_CODE_CONFIRMATION = "needs_code_confirmation"
    ACTION_REQUIRED = "action_required"
    READY_FOR_DISPATCH = "ready_for_dispatch"
    DISPATCHING = "dispatching"
    SUBMITTED = "submitted"
    PAYER_APPROVED = "payer_approved"
    PAYER_REJECTED = "payer_rejected"
    DISPATCH_FAILED = "dispatch_failed"
    FAILED = "failed"


# Copy of the enum as a plain list, for anywhere a DB CHECK constraint or
# frontend enum needs the raw strings instead of the Python type.
CASE_STATUSES: list[str] = [s.value for s in CaseStatus]

Stage = Literal["preauth", "claim"]
Trigger = Literal["ingest", "docs_updated", "code_confirmed", "manual_retry"]


# ---------------------------------------------------------------------------
# 2.2 Queue job envelope — what gets XADD'ed to the Redis stream
# ---------------------------------------------------------------------------


class JobEnvelope(BaseModel):
    """Deliberately thin. The worker re-reads the full case from Postgres,
    so a re-run always sees fresh data and the stream stays small."""

    job_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    case_id: str
    attempt: int = 1
    stage: Stage
    enqueued_at: datetime = Field(default_factory=datetime.utcnow)
    trigger: Trigger = "ingest"


# ---------------------------------------------------------------------------
# 2.3 Ingest API contract (for the HMS team)
#     POST /api/v1/cases/ingest
#     Header: Idempotency-Key: <client-generated uuid>   REQUIRED
# ---------------------------------------------------------------------------


class Patient(BaseModel):
    name: str
    gender: Literal["male", "female", "other"]
    birth_date: date
    abha_number: str | None = None
    ration_card_no: str | None = None


class Encounter(BaseModel):
    admission_date: date
    attending_doctor: str
    doctor_registration_no: str
    hospital_id: str


class Document(BaseModel):
    document_type: str
    file_url: str


class IngestRequest(BaseModel):
    hms_case_ref: str
    stage: Stage
    patient: Patient
    encounter: Encounter
    clinical_notes: str
    documents: list[Document] = Field(default_factory=list)


class IngestResponse(BaseModel):
    """Always 202, always fast (<50ms). The gateway does nothing more than
    validate, insert, XADD, and return this."""

    case_id: str
    status: CaseStatus = CaseStatus.QUEUED
    tracking_ref: str


# ---------------------------------------------------------------------------
# 2.4 missing_requirements shape — rendered directly by the frontend
# ---------------------------------------------------------------------------


class RequirementOption(BaseModel):
    code: str
    label: str
    unmapped: bool = False


class Requirement(BaseModel):
    requirement_id: str
    stage: Stage
    satisfied: bool
    any_of: list[RequirementOption]
    human_label: str


# ---------------------------------------------------------------------------
# 2.5 Semantic code mapping (Phase 5) — locked interface, see
#     sih/PHASE5_SPLIT.md §1. mapper.py produces these; worker/pipeline.py
#     consumes them for confidence routing.
# ---------------------------------------------------------------------------


class CodeCandidate(BaseModel):
    code: str          # packages.code — always sourced from the DB, so
                        # writing it to cases.mapped_package_code never
                        # violates the FK.
    name: str
    confidence: float  # 0.0 - 1.0
