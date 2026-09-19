"""
Comprehensive Unit Tests for Goal 0: Anti-Fraud & Clinical Integrity Engine.

Pillar 1: ABHA Identity & Verhoeff Checksum Validation
Pillar 2: Concurrent Active Inpatient Admission Check (Ghost Hospital Collision)
Pillar 3: Anatomical Impossibility & Procedure Cooldown Registry
FastAPI Gateway Gatekeeper: Rejection on POST /api/v1/cases/ingest
"""

from __future__ import annotations

import sys
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mediplug.fraud.abha import (
    compute_verhoeff_checksum,
    is_dummy_sequence,
    validate_abha,
    validate_phr_address,
    validate_verhoeff,
)
from mediplug.fraud.collision import check_concurrent_admission
from mediplug.fraud.engine import IntegrityCheckResult, evaluate_clinical_integrity
from mediplug.fraud.restrictions import (
    SINGLE_EXCISION_ORGAN_BY_CODE,
    PROCEDURE_COOLDOWNS_DAYS,
    check_procedure_restrictions,
)
from mediplug.gateway import main

client = TestClient(main.app)


# ---------------------------------------------------------------------------
# Mock DB Cursor Helpers
# ---------------------------------------------------------------------------

class _FakeCursor:
    def __init__(self, fetchone_val=None, fetchall_val=None):
        self._fetchone_val = fetchone_val
        self._fetchall_val = fetchall_val or []
        self.executed: list[tuple] = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return self._fetchone_val

    def fetchall(self):
        return self._fetchall_val


# ===========================================================================
# Pillar 1: ABHA Identity, Dummy Pattern & Verhoeff Tests
# ===========================================================================

def test_compute_and_validate_verhoeff():
    # 13 digits + calculated check digit
    base = "1448923057193"
    check_digit = compute_verhoeff_checksum(base)
    full_number = base + check_digit
    assert validate_verhoeff(full_number) is True

    # Tampering any single digit must invalidate Verhoeff
    corrupted = full_number[:-1] + ("0" if full_number[-1] != "0" else "1")
    assert validate_verhoeff(corrupted) is False


def test_validate_abha_success():
    # Calculate a valid 14-digit ABHA
    base = "9182736450192"
    check_digit = compute_verhoeff_checksum(base)
    valid_raw = base + check_digit

    # Raw 14 digits
    ok, err = validate_abha(valid_raw)
    assert ok is True
    assert err is None

    # Hyphenated format XX-XXXX-XXXX-XXXX
    formatted = f"{valid_raw[0:2]}-{valid_raw[2:6]}-{valid_raw[6:10]}-{valid_raw[10:14]}"
    ok2, err2 = validate_abha(formatted)
    assert ok2 is True
    assert err2 is None


def test_validate_abha_none_or_empty_is_optional():
    ok, err = validate_abha(None)
    assert ok is True
    assert err is None

    ok, err = validate_abha("")
    assert ok is True
    assert err is None


def test_validate_abha_invalid_format_and_length():
    # Less than 14 digits
    ok, err = validate_abha("12345678")
    assert ok is False
    assert "14 numeric digits" in err

    # Non-numeric
    ok, err = validate_abha("14-ABCD-EFGH-1234")
    assert ok is False

    # Bad hyphen placement
    ok, err = validate_abha("123-4567-8901-23")
    assert ok is False


def test_validate_abha_repetitive_and_dummy_patterns():
    # All zeros
    ok, err = validate_abha("00-0000-0000-0000")
    assert ok is False
    assert "invalid dummy sequence" in err

    # All ones
    ok, err = validate_abha("11111111111111")
    assert ok is False
    assert "invalid dummy sequence" in err

    # Sequential dummy
    ok, err = validate_abha("12345678901234")
    assert ok is False
    assert "invalid dummy sequence" in err


def test_validate_abha_verhoeff_checksum_failure():
    # 14 digits with incorrect check digit
    invalid_checksum_abha = "14489230571930"  # Check digit is actually 6, not 0
    ok, err = validate_abha(invalid_checksum_abha)
    assert ok is False
    assert "checksum validation failed" in err


def test_validate_phr_address():
    assert validate_phr_address("rohit.kumar@abdm")[0] is True
    assert validate_phr_address("user_123@sbx")[0] is True
    assert validate_phr_address("invalid-phr-no-domain")[0] is False
    assert validate_phr_address("")[0] is True  # Optional


# ===========================================================================
# Pillar 2: Ghost Hospital / Concurrent Inpatient Admission Tests
# ===========================================================================

def test_concurrent_admission_no_collision_when_no_active_records():
    cur = _FakeCursor(fetchone_val=None)
    patient = {"abha_number": "14-4892-3057-1936", "name": "Aarav Sharma", "birth_date": "1985-04-12"}
    encounter = {"hospital_id": "HOSP-DELHI-01", "admission_date": "2026-09-08"}

    passed, detail = check_concurrent_admission(cur, str(uuid4()), patient, encounter)
    assert passed is True
    assert detail is None


def test_concurrent_admission_detects_ghost_hospital_collision():
    colliding_row = (
        uuid4(),
        "TRK-COLLIDE-99",
        "HOSP-MUMBAI-02",  # Different hospital!
        "2026-09-05",       # Admitted earlier, still active
    )
    cur = _FakeCursor(fetchone_val=colliding_row)

    patient = {"abha_number": "14-4892-3057-1936", "name": "Aarav Sharma", "birth_date": "1985-04-12"}
    encounter = {"hospital_id": "HOSP-DELHI-01", "admission_date": "2026-09-08"}

    passed, detail = check_concurrent_admission(cur, str(uuid4()), patient, encounter)
    assert passed is False
    assert detail is not None
    assert detail["violation_type"] == "concurrent_admission_collision"
    assert detail["colliding_hospital_id"] == "HOSP-MUMBAI-02"
    assert "Ghost admission conflict" in detail["message"]


# ===========================================================================
# Pillar 3: Single-Excision Impossibility & Cooldown Tests
# ===========================================================================

def test_anatomical_impossibility_repeat_cholecystectomy():
    # Patient previously had Gallbladder excised under S8G5.11
    prior_case_id = uuid4()
    prior_rows = [
        (prior_case_id, "TRK-PRIOR-01", "S8G5.11", "2025-01-15", "payer_approved"),
    ]
    cur = _FakeCursor(fetchall_val=prior_rows)

    patient = {"abha_number": "14-4892-3057-1936", "name": "Sunita Verma", "birth_date": "1978-10-20"}
    encounter = {"hospital_id": "HOSP-01", "admission_date": "2026-09-08"}

    # Attempting another Gallbladder procedure (S1A11.2 - Open Cholecystectomy)
    passed, detail = check_procedure_restrictions(
        cur, str(uuid4()), patient, encounter, "S1A11.2"
    )

    assert passed is False
    assert detail is not None
    assert detail["violation_type"] == "anatomical_impossibility"
    assert detail["organ"] == "Gallbladder"
    assert "The Gallbladder cannot be excised twice" in detail["message"]


def test_anatomical_impossibility_repeat_appendectomy():
    # Patient previously had Appendix excised under S1A5.1 (Lap Appendectomy)
    prior_rows = [
        (uuid4(), "TRK-PRIOR-02", "S1A5.1", "2024-06-10", "payer_approved"),
    ]
    cur = _FakeCursor(fetchall_val=prior_rows)

    patient = {"abha_number": "14-4892-3057-1936"}
    encounter = {"hospital_id": "HOSP-01", "admission_date": "2026-09-08"}

    # Attempting S8G4.1 (Open Appendectomy)
    passed, detail = check_procedure_restrictions(
        cur, str(uuid4()), patient, encounter, "S8G4.1"
    )

    assert passed is False
    assert detail["organ"] == "Appendix"
    assert detail["violation_type"] == "anatomical_impossibility"


def test_procedure_cooldown_violation_cabg():
    # CABG (S7F7.2) requires 180 days cooldown
    prior_admit = "2026-08-01"  # Only ~38 days before current admission
    current_admit = "2026-09-08"

    prior_rows = [
        (uuid4(), "TRK-CABG-01", "S7F7.2", prior_admit, "payer_approved"),
    ]
    cur = _FakeCursor(fetchall_val=prior_rows)

    patient = {"abha_number": "14-4892-3057-1936"}
    encounter = {"hospital_id": "HOSP-01", "admission_date": current_admit}

    passed, detail = check_procedure_restrictions(
        cur, str(uuid4()), patient, encounter, "S7F7.2"
    )

    assert passed is False
    assert detail is not None
    assert detail["violation_type"] == "cooldown_violation"
    assert detail["cooldown_days"] == 180
    assert detail["days_elapsed"] == 38
    assert "Procedure Cooldown Violation" in detail["message"]


def test_procedure_cooldown_satisfied_after_mandatory_interval():
    # CABG performed 200 days ago (mandatory cooldown is 180 days)
    prior_admit = "2026-01-01"
    current_admit = "2026-09-08"

    prior_rows = [
        (uuid4(), "TRK-CABG-01", "S7F7.2", prior_admit, "payer_approved"),
    ]
    cur = _FakeCursor(fetchall_val=prior_rows)

    patient = {"abha_number": "14-4892-3057-1936"}
    encounter = {"hospital_id": "HOSP-01", "admission_date": current_admit}

    passed, detail = check_procedure_restrictions(
        cur, str(uuid4()), patient, encounter, "S7F7.2"
    )

    assert passed is True
    assert detail is None


# ===========================================================================
# Unified Evaluator Tests
# ===========================================================================

def test_evaluate_clinical_integrity_passes_when_clean():
    cur = _FakeCursor(fetchone_val=None, fetchall_val=[])
    patient = {"abha_number": "14-4892-3057-1936", "name": "Kavita Nair", "birth_date": "1990-01-01"}
    encounter = {"hospital_id": "HOSP-BLR-01", "admission_date": "2026-09-08"}

    res = evaluate_clinical_integrity(cur, str(uuid4()), patient, encounter, "S1A5.1")
    assert res.passed is True
    assert res.violation_type is None


# ===========================================================================
# Ingest API Gatekeeper Test (FastAPI Endpoint)
# ===========================================================================

def test_gateway_ingest_rejects_dummy_abha():
    payload = {
        "hms_case_ref": "HMS-FRAUD-01",
        "patient": {
            "name": "Test Fraud Patient",
            "gender": "male",
            "birth_date": "1990-01-01",
            "abha_number": "00-0000-0000-0000",  # Dummy repetitive pattern!
        },
        "encounter": {
            "hospital_id": "HOSP-01",
            "admission_date": "2026-09-08",
            "attending_doctor": "Dr. Smith",
            "doctor_registration_no": "MCI-12345",
        },
        "stage": "preauth",
        "clinical_notes": "Patient presents with acute abdominal pain.",
    }

    resp = client.post(
        "/api/v1/cases/ingest",
        json=payload,
        headers={"Idempotency-Key": str(uuid4())},
    )

    assert resp.status_code == 400
    assert "Anti-Fraud Gatekeeper" in resp.json()["detail"]
    assert "invalid dummy sequence" in resp.json()["detail"]


def test_gateway_ingest_rejects_verhoeff_corrupted_abha():
    payload = {
        "hms_case_ref": "HMS-FRAUD-02",
        "patient": {
            "name": "Test Corrupt Patient",
            "gender": "female",
            "birth_date": "1992-05-15",
            "abha_number": "14-4892-3057-1930",  # Corrupted check digit (should be 6)
        },
        "encounter": {
            "hospital_id": "HOSP-01",
            "admission_date": "2026-09-08",
            "attending_doctor": "Dr. Sarah",
            "doctor_registration_no": "MCI-12345",
        },
        "stage": "preauth",
        "clinical_notes": "Patient scheduled for elective surgery.",
    }

    resp = client.post(
        "/api/v1/cases/ingest",
        json=payload,
        headers={"Idempotency-Key": str(uuid4())},
    )

    assert resp.status_code == 400
    assert "Anti-Fraud Gatekeeper" in resp.json()["detail"]
    assert "checksum validation failed" in resp.json()["detail"]
