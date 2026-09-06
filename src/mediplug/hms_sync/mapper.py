"""
HMS Sync — mapper module.

Translates one "joined encounter row" from the teammate's HMS Supabase
into our IngestRequest contract. This is the ONE place HMS-specific
column names are allowed — everything downstream only sees IngestRequest.

HMS schema reality (from 002_schema.sql):
  - encounters: encounter_id, patient_id, hospital_id, admission_date,
                attending_doctor (raw string), doctor_id (FK), status (enum)
  - patients: patient_id, full_name, age, date_of_birth (nullable), gender ('M'/'F'),
              abha_id, ration_card_type (not ration_card_no)
  - clinical_notes: note_id, encounter_id, provisional_diagnosis, doctor_note_raw
  - doctors: doctor_id, doctor_name, nmc_reg_number, hpr_id
  - hospitals: hospital_id, hfr_id
  - diagnostic_reports: report_id, encounter_id, report_type, file_status, file_url
  - documents: document_id, patient_id, encounter_id, document_type, file_url, status

Key gotchas handled here:
  - gender is 'M'/'F' free-text → must normalize to 'male'/'female'/'other'
  - date_of_birth is nullable, age exists → we synthesize a birth_date if needed
  - ration_card_type ≠ ration_card_no → left unset (semantically different)
  - doctor_id / hospital_id can be NULL → skip row if essential fields missing
  - documents come from TWO tables (documents + diagnostic_reports) → merge
  - encounter_status maps to stage: 'Pre-Auth Pending' → 'preauth',
    'Discharged' → 'claim'
"""

from __future__ import annotations

import logging
from datetime import date

from ..schemas import Document, Encounter, IngestRequest, Patient

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Gender normalization
# ---------------------------------------------------------------------------

_GENDER_MAP = {
    "m": "male",
    "male": "male",
    "f": "female",
    "female": "female",
}


def _normalize_gender(raw: str | None) -> str:
    """HMS stores 'M'/'F'. Our contract needs 'male'/'female'/'other'."""
    if not raw:
        return "other"
    return _GENDER_MAP.get(raw.strip().lower(), "other")


# ---------------------------------------------------------------------------
# Stage derivation from encounter_status
# ---------------------------------------------------------------------------

_STATUS_TO_STAGE = {
    "Pre-Auth Pending": "preauth",
    "Admitted": "preauth",       # still in hospital, preauth phase
    "Discharged": "claim",       # post-discharge = claim phase
}


def _derive_stage(encounter_status: str | None) -> str:
    if not encounter_status:
        return "preauth"
    return _STATUS_TO_STAGE.get(encounter_status, "preauth")


# ---------------------------------------------------------------------------
# Birth date synthesis
# ---------------------------------------------------------------------------

def _get_birth_date(patient: dict) -> date:
    """HMS has date_of_birth (nullable) and age (integer).
    Our contract requires birth_date as a date. Synthesize if missing."""
    dob = patient.get("date_of_birth")
    if dob:
        if isinstance(dob, str):
            return date.fromisoformat(dob)
        return dob

    age = patient.get("age")
    if age and isinstance(age, int):
        today = date.today()
        return date(today.year - age, 1, 1)  # approximate: Jan 1 of birth year

    return date(1970, 1, 1)  # last-resort fallback


# ---------------------------------------------------------------------------
# Document merging (diagnostic_reports + documents → list[Document])
# ---------------------------------------------------------------------------

def _merge_documents(
    diagnostic_reports: list[dict],
    documents: list[dict],
) -> list[Document]:
    """Merge HMS's two document tables into our single documents list.

    diagnostic_reports: report_type, file_status, file_url
    documents: document_type, file_url, status
    """
    result: list[Document] = []

    for dr in diagnostic_reports:
        # Only include reports that are actually uploaded (have a URL)
        url = dr.get("file_url")
        if not url or not url.strip():
            continue
        report_type = (dr.get("report_type") or "unknown").strip()
        # Normalize report_type to lowercase_with_underscores for consistency
        doc_type = report_type.lower().replace(" ", "_")
        result.append(Document(document_type=doc_type, file_url=url.strip()))

    for doc in documents:
        url = doc.get("file_url")
        if not url or not url.strip():
            continue
        doc_type = (doc.get("document_type") or "unknown").strip()
        doc_type = doc_type.lower().replace(" ", "_")
        result.append(Document(document_type=doc_type, file_url=url.strip()))

    return result


# ---------------------------------------------------------------------------
# Main mapper
# ---------------------------------------------------------------------------

def map_hms_to_ingest_request(
    encounter: dict,
    patient: dict,
    clinical_note: dict | None,
    doctor: dict | None,
    hospital: dict | None,
    diagnostic_reports: list[dict],
    documents: list[dict],
) -> IngestRequest | None:
    """Map one HMS encounter (with joined data) into our IngestRequest.

    Returns None if essential fields are missing (caller should skip & log).
    """
    # --- Validate essential fields ---
    patient_name = patient.get("full_name")
    if not patient_name:
        log.warning("skipping encounter %s: patient has no name", encounter.get("encounter_id"))
        return None

    # Clinical notes are essential — without them, semantic mapping can't work
    notes_text = ""
    if clinical_note:
        # Combine provisional diagnosis + raw note for maximum context
        diag = clinical_note.get("provisional_diagnosis") or ""
        raw = clinical_note.get("doctor_note_raw") or ""
        notes_text = f"{diag}. {raw}".strip(". ")

    if not notes_text:
        log.warning("skipping encounter %s: no clinical notes", encounter.get("encounter_id"))
        return None

    # Doctor info — best-effort, use attending_doctor string as fallback
    attending_doctor = (
        (doctor.get("doctor_name") if doctor else None)
        or encounter.get("attending_doctor")
        or "Unknown"
    )
    doctor_reg_no = (
        (doctor.get("nmc_reg_number") if doctor else None)
        or "UNKNOWN"
    )

    # Hospital info — best-effort
    hospital_id = (
        (hospital.get("hfr_id") if hospital else None)
        or encounter.get("hospital_id")
        or "UNKNOWN"
    )

    return IngestRequest(
        hms_case_ref=encounter["encounter_id"],
        stage=_derive_stage(encounter.get("status")),
        patient=Patient(
            name=patient_name,
            gender=_normalize_gender(patient.get("gender")),
            birth_date=_get_birth_date(patient),
            abha_number=patient.get("abha_id"),
            # ration_card_type ≠ ration_card_no — intentionally left unset
        ),
        encounter=Encounter(
            admission_date=(
                date.fromisoformat(encounter["admission_date"])
                if encounter.get("admission_date")
                else date.today()
            ),
            attending_doctor=attending_doctor,
            doctor_registration_no=doctor_reg_no,
            hospital_id=hospital_id,
        ),
        clinical_notes=notes_text,
        documents=_merge_documents(diagnostic_reports, documents),
    )
