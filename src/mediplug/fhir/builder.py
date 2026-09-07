"""
Phase 7 — FHIR R4 Bundle builder.

`build_claim_bundle()` turns an approved case into a FHIR **R4B** `Bundle`
(`type: collection`) with a `Claim` as the focal resource plus every
resource it references (Patient, Organization ×2, Practitioner, Coverage,
DocumentReference ×N).

Two deliberate deviations from MEDIPLUG_MIDDLEWARE_BUILD_GUIDE.md §9.2 —
see sih/PHASE7_SPLIT.md §2 for the why:

  1. `validate()` imports from `fhir.resources.R4B.*`, NOT the package
     default. fhir.resources 8.3.0 defaults to **R5**, and R5's `Coverage`
     dropped `payor` / made `subscriberId` a list — HCX/NHCX target R4, so
     the R5 model rejects an HCX-shaped bundle.
  2. One UUID per resource, used consistently: `resource["id"]` is the bare
     UUID, its `entry.fullUrl` is `urn:uuid:<that-uuid>`, and every
     `.reference` pointing at it is `urn:uuid:<that-uuid>`. The guide's
     reference code generates fresh UUIDs for the Claim/Bundle `id` and
     rebuilds `fullUrl` from `r["id"]`, which is internally inconsistent.

Contract (locked — see sih/PHASE7_HANDOFF.md):
  * `build_claim_bundle` is a **pure, synchronous** function. No DB, no I/O,
    no async. The caller fetches `case` / `package` / `documents` rows and
    passes plain dicts.
  * It returns a plain `dict` — that dict is what goes on the wire and into
    `cases.fhir_bundle` (jsonb).
  * Empty `documents` is valid input (no `supportingInfo`, bundle still
    valid).
  * When `package["icd_code"]` is falsy the `diagnosis` key is **omitted**
    entirely (still valid R4B). The mock payer (Phase 8) rejects a Claim
    with no diagnosis — `scripts/09_fhir_sample.py` reports how many real
    packages that affects.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

MJPJAY_SYSTEM = "https://www.jeevandayee.gov.in/package-code"
ICD10_SYSTEM = "http://hl7.org/fhir/sid/icd-10"

CLAIM_TYPE_SYSTEM = "http://terminology.hl7.org/CodeSystem/claim-type"
PROCESS_PRIORITY_SYSTEM = "http://terminology.hl7.org/CodeSystem/processpriority"

ABHA_SYSTEM = "https://healthid.abdm.gov.in"
FACILITY_SYSTEM = "https://facility.abdm.gov.in"
HPR_SYSTEM = "https://hpr.abdm.gov.in"

INSURER_NAME = "State Health Assurance Society (MJPJAY)"
DEFAULT_HOSPITAL_NAME = "Demo District Hospital"


def _uuid() -> str:
    """A bare UUID string — used simultaneously as a resource `id` and,
    prefixed with `urn:uuid:`, as its `fullUrl` and every reference to it."""
    return str(uuid.uuid4())


def _urn(resource_id: str) -> dict:
    """A FHIR reference object pointing at an in-bundle resource by UUID."""
    return {"reference": f"urn:uuid:{resource_id}"}


def build_claim_bundle(
    case: dict,
    package: dict,
    documents: list[dict],
    stage: str,
) -> dict:
    """Build a FHIR R4B `Bundle` (collection) with a `Claim` focal resource.

    Args:
        case:      a `cases` row — `.patient` and `.encounter` are the JSONB
                   blobs written by the gateway (`Patient` / `Encounter`
                   `model_dump(mode="json")`).
        package:   a `packages` row — `code`, `name`, `amount`, `icd_code`.
        documents: `case_documents` rows — `document_type`, `file_url`,
                   `file_name`. May be empty.
        stage:     `"preauth"` -> `Claim.use = "preauthorization"`,
                   anything else -> `"claim"`.

    Returns:
        A plain dict. Structurally validate it with `validate()` before use.
    """
    p = case.get("patient") or {}
    e = case.get("encounter") or {}

    patient_id = _uuid()
    provider_id = _uuid()
    insurer_id = _uuid()
    practitioner_id = _uuid()
    coverage_id = _uuid()

    now = datetime.now(UTC).isoformat()

    patient = {
        "resourceType": "Patient",
        "id": patient_id,
        "name": [{"text": p.get("name", "UNKNOWN")}],
    }
    if p.get("gender"):
        patient["gender"] = p["gender"]
    if p.get("birth_date"):
        patient["birthDate"] = p["birth_date"]
    if p.get("abha_number"):
        patient["identifier"] = [{"system": ABHA_SYSTEM, "value": p["abha_number"]}]

    provider = {
        "resourceType": "Organization",
        "id": provider_id,
        "identifier": [
            {"system": FACILITY_SYSTEM, "value": e.get("hospital_id", "UNKNOWN")}
        ],
        "name": e.get("hospital_name") or DEFAULT_HOSPITAL_NAME,
    }

    insurer = {
        "resourceType": "Organization",
        "id": insurer_id,
        "name": INSURER_NAME,
    }

    practitioner = {
        "resourceType": "Practitioner",
        "id": practitioner_id,
        "identifier": [
            {"system": HPR_SYSTEM, "value": e.get("doctor_registration_no", "UNKNOWN")}
        ],
        "name": [{"text": e.get("attending_doctor", "UNKNOWN")}],
    }

    coverage = {
        "resourceType": "Coverage",
        "id": coverage_id,
        "status": "active",
        "beneficiary": _urn(patient_id),
        "payor": [_urn(insurer_id)],
        "subscriberId": p.get("ration_card_no") or "UNKNOWN",
    }

    supporting_info: list[dict] = []
    doc_resources: list[dict] = []
    for i, d in enumerate(documents, start=1):
        doc_id = _uuid()
        title = d.get("file_name") or d.get("document_type") or "attachment"
        doc_resources.append(
            {
                "resourceType": "DocumentReference",
                "id": doc_id,
                "status": "current",
                "type": {"text": d.get("document_type", "attachment")},
                "subject": _urn(patient_id),
                "content": [
                    {
                        "attachment": {
                            "url": d.get("file_url", ""),
                            "title": title,
                        }
                    }
                ],
            }
        )
        supporting_info.append(
            {
                "sequence": i,
                "category": {"text": "attachment"},
                "valueReference": _urn(doc_id),
            }
        )

    amount = package.get("amount")
    if amount is None:
        amount = 0
    money = {"value": amount, "currency": "INR"}

    claim = {
        "resourceType": "Claim",
        "id": _uuid(),
        "status": "active",
        "type": {"coding": [{"system": CLAIM_TYPE_SYSTEM, "code": "institutional"}]},
        "use": "preauthorization" if stage == "preauth" else "claim",
        "patient": _urn(patient_id),
        "created": now,
        "insurer": _urn(insurer_id),
        "provider": _urn(provider_id),
        "priority": {"coding": [{"system": PROCESS_PRIORITY_SYSTEM, "code": "normal"}]},
        "careTeam": [{"sequence": 1, "provider": _urn(practitioner_id)}],
        "supportingInfo": supporting_info,
        "insurance": [
            {"sequence": 1, "focal": True, "coverage": _urn(coverage_id)}
        ],
        "item": [
            {
                "sequence": 1,
                "productOrService": {
                    "coding": [
                        {
                            "system": MJPJAY_SYSTEM,
                            "code": package.get("code", "UNKNOWN"),
                            "display": package.get("name", ""),
                        }
                    ]
                },
                "unitPrice": money,
            }
        ],
        "total": money,
    }

    # ICD gap: 801/1670 packages have no icd_code. Omit `diagnosis` entirely
    # rather than emit an empty list or a placeholder code — still valid
    # R4B, honest about the missing data. (sih/PHASE7_SPLIT.md §2.3)
    if package.get("icd_code"):
        claim["diagnosis"] = [
            {
                "sequence": 1,
                "diagnosisCodeableConcept": {
                    "coding": [{"system": ICD10_SYSTEM, "code": package["icd_code"]}]
                },
            }
        ]

    entries = [claim, patient, provider, insurer, practitioner, coverage, *doc_resources]
    return {
        "resourceType": "Bundle",
        "id": _uuid(),
        "type": "collection",
        "timestamp": now,
        "entry": [{"fullUrl": f"urn:uuid:{r['id']}", "resource": r} for r in entries],
    }


def validate(bundle: dict) -> None:
    """Structurally validate `bundle` against the FHIR **R4B** `Bundle`
    model. Raises `pydantic.ValidationError` on a malformed shape.

    R4B, not the fhir.resources default (R5) — HCX/NHCX are R4. See the
    module docstring and sih/PHASE7_SPLIT.md §2.1.
    """
    from fhir.resources.R4B.bundle import Bundle

    Bundle.model_validate(bundle)
