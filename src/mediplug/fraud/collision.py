"""
Pillar 2: Ghost Hospital Admissions & Concurrent Inpatient Admission Checker.

Detects simultaneous active, un-discharged admissions for the same patient
across different hospitals (e.g. ghost bed billing).
"""

from __future__ import annotations

from typing import Any


def check_concurrent_admission(
    cur,
    case_id: str,
    patient: dict[str, Any],
    encounter: dict[str, Any],
) -> tuple[bool, dict[str, Any] | None]:
    """Check if the patient has another active inpatient admission at a different hospital.

    Args:
        cur: Database cursor.
        case_id: Current case ID (to exclude self).
        patient: Patient dict containing 'abha_number', 'name', 'birth_date'.
        encounter: Encounter dict containing 'hospital_id', 'admission_date'.

    Returns:
        (True, None) if no collision.
        (False, detail_dict) if a concurrent ghost admission collision is detected.
    """
    abha = patient.get("abha_number")
    name = (patient.get("name") or "").strip().lower()
    birth_date = str(patient.get("birth_date") or "")
    current_hospital = (encounter.get("hospital_id") or "").strip()
    admission_date = str(encounter.get("admission_date") or "")

    if not current_hospital or not admission_date:
        return True, None

    # We match primarily by ABHA number if present, or by exact Name + DOB
    if abha and abha.strip():
        cur.execute(
            """
            SELECT id, tracking_ref, encounter->>'hospital_id', encounter->>'admission_date'
            FROM cases
            WHERE id != %s::uuid
              AND patient->>'abha_number' = %s
              AND status NOT IN ('payer_rejected', 'failed', 'dispatch_failed')
              AND (encounter->>'discharge_date' IS NULL OR (encounter->>'discharge_date')::date >= %s::date)
              AND encounter->>'hospital_id' != %s
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (case_id, abha.strip(), admission_date, current_hospital),
        )
    else:
        # Fallback to Name + DOB matching if ABHA is not provided
        cur.execute(
            """
            SELECT id, tracking_ref, encounter->>'hospital_id', encounter->>'admission_date'
            FROM cases
            WHERE id != %s::uuid
              AND lower(patient->>'name') = %s
              AND patient->>'birth_date' = %s
              AND status NOT IN ('payer_rejected', 'failed', 'dispatch_failed')
              AND (encounter->>'discharge_date' IS NULL OR (encounter->>'discharge_date')::date >= %s::date)
              AND encounter->>'hospital_id' != %s
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (case_id, name, birth_date, admission_date, current_hospital),
        )

    row = cur.fetchone()
    if row is not None:
        colliding_id, colliding_ref, colliding_hospital, colliding_admit = row
        detail = {
            "violation_type": "concurrent_admission_collision",
            "colliding_case_id": str(colliding_id),
            "colliding_tracking_ref": colliding_ref,
            "colliding_hospital_id": colliding_hospital,
            "colliding_admission_date": colliding_admit,
            "current_hospital_id": current_hospital,
            "message": (
                f"Ghost admission conflict: Patient is already actively admitted at "
                f"hospital '{colliding_hospital}' since {colliding_admit} (Case: {colliding_ref})"
            ),
        }
        return False, detail

    return True, None
