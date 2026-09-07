"""
Phase 7 — pure unit tests for the FHIR R4 bundle builder.

No DB, no network. Hand-built `case` / `package` / `documents` fixtures.
Real-data coverage lives in `scripts/09_fhir_sample.py`.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mediplug.fhir import build_claim_bundle, validate
from mediplug.fhir.builder import ICD10_SYSTEM, MJPJAY_SYSTEM


def _case(**patient_over):
    patient = {
        "name": "Ramesh Kumar",
        "gender": "male",
        "birth_date": "1980-05-15",
        "abha_number": "12-3456-7890-1234",
        "ration_card_no": "RC-99881",
    }
    patient.update(patient_over)
    return {
        "patient": patient,
        "encounter": {
            "admission_date": "2026-09-06",
            "attending_doctor": "Dr Meera Nair",
            "doctor_registration_no": "MH-2011-4456",
            "hospital_id": "HOSP-4471",
        },
    }


def _package(**over):
    pkg = {
        "code": "S1A11.2",
        "name": "Lap.Cholecystectomy",
        "amount": 22000,
        "icd_code": "K80.2",
    }
    pkg.update(over)
    return pkg


def _documents():
    return [
        {
            "document_type": "usg_report",
            "file_url": "https://files.example.com/usg-1.pdf",
            "file_name": "abdomen_usg.pdf",
        },
        {
            "document_type": "blood_report",
            "file_url": "https://files.example.com/cbc-1.pdf",
            "file_name": None,
        },
    ]


def _claim(bundle):
    claims = [
        entry["resource"]
        for entry in bundle["entry"]
        if entry["resource"]["resourceType"] == "Claim"
    ]
    assert len(claims) == 1, "bundle must contain exactly one Claim"
    return claims[0]


def test_happy_path_validates():
    bundle = build_claim_bundle(_case(), _package(), _documents(), "preauth")
    validate(bundle)  # R4B — raises on a bad shape
    assert bundle["resourceType"] == "Bundle"
    assert bundle["type"] == "collection"


def test_claim_has_required_non_empty_fields():
    claim = _claim(build_claim_bundle(_case(), _package(), _documents(), "preauth"))
    assert claim["patient"]["reference"]
    assert claim["provider"]["reference"]
    assert claim["insurer"]["reference"]
    assert claim["insurance"] and claim["insurance"][0]["coverage"]["reference"]
    assert claim["item"] and claim["item"][0]["productOrService"]["coding"][0]["code"] == "S1A11.2"
    assert claim["item"][0]["productOrService"]["coding"][0]["system"] == MJPJAY_SYSTEM


def test_reference_integrity_all_urn_refs_resolve():
    """Every internal `.reference` (urn:uuid:...) must resolve to some
    `entry[].fullUrl`, and no reference may be a bare UUID. Guards the
    guide's Claim/Bundle id inconsistency (PHASE7_SPLIT.md §2.2)."""
    bundle = build_claim_bundle(_case(), _package(), _documents(), "preauth")
    full_urls = {entry["fullUrl"] for entry in bundle["entry"]}
    assert all(u.startswith("urn:uuid:") for u in full_urls)

    seen_refs = []

    def walk(node):
        if isinstance(node, dict):
            if "reference" in node and isinstance(node["reference"], str):
                seen_refs.append(node["reference"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(bundle["entry"])
    assert seen_refs, "expected at least one internal reference"
    for ref in seen_refs:
        assert ref.startswith("urn:uuid:"), f"bare / non-urn reference: {ref}"
        assert ref in full_urls, f"dangling reference: {ref}"


def test_stage_maps_to_claim_use():
    preauth = _claim(build_claim_bundle(_case(), _package(), [], "preauth"))
    assert preauth["use"] == "preauthorization"
    claim = _claim(build_claim_bundle(_case(), _package(), [], "claim"))
    assert claim["use"] == "claim"


def test_icd_present_emits_diagnosis():
    claim = _claim(build_claim_bundle(_case(), _package(icd_code="K80.2"), [], "preauth"))
    assert claim["diagnosis"][0]["diagnosisCodeableConcept"]["coding"][0]["system"] == ICD10_SYSTEM
    assert claim["diagnosis"][0]["diagnosisCodeableConcept"]["coding"][0]["code"] == "K80.2"


@pytest.mark.parametrize("missing_icd", [None, "", 0])
def test_icd_absent_omits_diagnosis_but_still_validates(missing_icd):
    bundle = build_claim_bundle(_case(), _package(icd_code=missing_icd), [], "preauth")
    claim = _claim(bundle)
    assert "diagnosis" not in claim
    validate(bundle)  # still valid R4B without diagnosis


def test_empty_documents_is_valid():
    bundle = build_claim_bundle(_case(), _package(), [], "preauth")
    validate(bundle)
    claim = _claim(bundle)
    assert claim["supportingInfo"] == []
    assert not any(
        e["resource"]["resourceType"] == "DocumentReference" for e in bundle["entry"]
    )


def test_documents_produce_docref_and_supporting_info():
    bundle = build_claim_bundle(_case(), _package(), _documents(), "preauth")
    docrefs = [
        e["resource"] for e in bundle["entry"]
        if e["resource"]["resourceType"] == "DocumentReference"
    ]
    assert len(docrefs) == 2
    # file_name falls back to document_type when absent
    titles = {d["content"][0]["attachment"]["title"] for d in docrefs}
    assert "abdomen_usg.pdf" in titles
    assert "blood_report" in titles
    assert len(_claim(bundle)["supportingInfo"]) == 2


def test_missing_amount_coerces_to_zero_and_validates():
    bundle = build_claim_bundle(_case(), _package(amount=None), [], "preauth")
    validate(bundle)
    claim = _claim(bundle)
    assert claim["item"][0]["unitPrice"]["value"] == 0
    assert claim["total"]["value"] == 0


def test_optional_patient_fields_absent_do_not_crash():
    case = _case()
    del case["patient"]["abha_number"]
    del case["patient"]["ration_card_no"]
    bundle = build_claim_bundle(case, _package(), _documents(), "preauth")
    validate(bundle)
    patient = next(
        e["resource"] for e in bundle["entry"]
        if e["resource"]["resourceType"] == "Patient"
    )
    assert "identifier" not in patient
    coverage = next(
        e["resource"] for e in bundle["entry"]
        if e["resource"]["resourceType"] == "Coverage"
    )
    assert coverage["subscriberId"] == "UNKNOWN"


def test_hospital_name_defaults_when_encounter_lacks_it():
    bundle = build_claim_bundle(_case(), _package(), [], "preauth")
    provider = next(
        e["resource"] for e in bundle["entry"]
        if e["resource"]["resourceType"] == "Organization"
        and e["resource"]["name"] != "State Health Assurance Society (MJPJAY)"
    )
    assert provider["name"] == "Demo District Hospital"


def test_entry_order_is_claim_first():
    bundle = build_claim_bundle(_case(), _package(), _documents(), "preauth")
    types = [e["resource"]["resourceType"] for e in bundle["entry"]]
    assert types[:6] == [
        "Claim", "Patient", "Organization", "Organization", "Practitioner", "Coverage",
    ]
    assert types[6:] == ["DocumentReference", "DocumentReference"]
