"""
Tests for Phase 0 schema contracts.
"""

import sys
from pathlib import Path
from datetime import date
import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mediplug.schemas import (
    CaseStatus,
    CASE_STATUSES,
    JobEnvelope,
    Patient,
    Encounter,
    Document,
    IngestRequest,
    ConfirmCodeRequest,
    CodeCandidate,
)


def test_case_statuses():
    assert len(CaseStatus) == 11
    assert "queued" in CASE_STATUSES
    assert "analyzing" in CASE_STATUSES
    assert "needs_code_confirmation" in CASE_STATUSES
    assert "action_required" in CASE_STATUSES
    assert "ready_for_dispatch" in CASE_STATUSES
    assert "submitted" in CASE_STATUSES


def test_job_envelope_valid():
    envelope = JobEnvelope(case_id="case-123", stage="preauth", trigger="ingest")
    assert envelope.attempt == 1
    assert envelope.stage == "preauth"
    assert envelope.trigger == "ingest"


def test_ingest_request_validation():
    req = IngestRequest(
        hms_case_ref="ENC-1001",
        stage="preauth",
        patient=Patient(name="John Doe", gender="male", birth_date=date(1980, 1, 1)),
        encounter=Encounter(
            admission_date=date(2026, 9, 1),
            attending_doctor="Dr. House",
            doctor_registration_no="REG123",
            hospital_id="HOSP01",
        ),
        clinical_notes="Patient complaints of severe abdominal pain",
        documents=[Document(document_type="usg_abdomen", file_url="https://example.com/usg.pdf")],
    )
    assert req.hms_case_ref == "ENC-1001"
    assert len(req.documents) == 1


def test_confirm_code_request():
    req = ConfirmCodeRequest(code="S1A11.2", confirmed_by="aarogyamitra_user")
    assert req.code == "S1A11.2"
    assert req.confirmed_by == "aarogyamitra_user"
