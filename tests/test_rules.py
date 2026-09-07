"""
Tests for Phase 6 Pre-flight Rule Engine (evaluate function).
Isolated unit tests with zero DB or Redis dependencies.
"""

import sys
from pathlib import Path
import pytest

# Ensure src is importable
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mediplug.rules.engine import evaluate, RuleEvaluation


def test_all_requirements_satisfied():
    requirements = [
        {
            "requirement_id": "req_0",
            "stage": "preauth",
            "human_label": "USG Abdomen or CT Abdomen",
            "any_of": [
                {"code": "usg_abdomen", "label": "USG Abdomen"},
                {"code": "ct_abdomen", "label": "CT Abdomen"},
            ],
        },
        {
            "requirement_id": "req_1",
            "stage": "preauth",
            "human_label": "CBC Report",
            "any_of": [
                {"code": "cbc", "label": "CBC Report"},
            ],
        },
    ]
    uploaded_docs = ["usg_abdomen", "cbc", "clinical_notes"]
    result = evaluate(requirements, uploaded_docs)

    assert result.passed is True
    assert len(result.missing_requirements) == 0
    # Also test tuple unpacking
    passed, missing = result
    assert passed is True
    assert missing == []


def test_one_any_of_group_unsatisfied():
    requirements = [
        {
            "requirement_id": "req_0",
            "stage": "preauth",
            "human_label": "USG Abdomen or CT Abdomen",
            "any_of": [
                {"code": "usg_abdomen", "label": "USG Abdomen"},
                {"code": "ct_abdomen", "label": "CT Abdomen"},
            ],
        },
        {
            "requirement_id": "req_1",
            "stage": "preauth",
            "human_label": "Biopsy / Histopath",
            "any_of": [
                {"code": "biopsy_report", "label": "Biopsy / Histopath"},
            ],
        },
    ]
    # Only USG provided, biopsy is missing
    uploaded_docs = ["usg_abdomen"]
    result = evaluate(requirements, uploaded_docs)

    assert result.passed is False
    assert len(result.missing_requirements) == 1
    assert result.missing_requirements[0]["requirement_id"] == "req_1"
    assert result.missing_requirements[0]["human_label"] == "Biopsy / Histopath"


def test_empty_requirements_list():
    # Needed for the 474 packages with zero requirements
    result_empty = evaluate([], ["any_doc.pdf"])
    assert result_empty.passed is True
    assert result_empty.missing_requirements == []

    result_none = evaluate(None, [])
    assert result_none.passed is True
    assert result_none.missing_requirements == []


def test_fuzzy_and_case_normalization():
    # Covers 'USG_Abdomen' vs 'usg_abdomen', 'usg abdomen', 'USG-Abdomen'
    requirements = [
        {
            "requirement_id": "req_0",
            "stage": "preauth",
            "human_label": "USG Abdomen",
            "any_of": [
                {"code": "usg_abdomen", "label": "USG Abdomen"},
            ],
        },
    ]

    # Test uppercase with underscore
    assert evaluate(requirements, ["USG_Abdomen"]).passed is True

    # Test lowercase with space
    assert evaluate(requirements, ["usg abdomen"]).passed is True

    # Test hyphenated
    assert evaluate(requirements, ["USG-Abdomen"]).passed is True

    # Test dict-based uploaded document format from DB query
    uploaded_as_dicts = [{"document_type": "USG_Abdomen"}]
    assert evaluate(requirements, uploaded_as_dicts).passed is True


def test_any_of_with_plain_strings():
    requirements = [
        {
            "requirement_id": "req_0",
            "human_label": "MRI or CT",
            "any_of": ["mri", "ct_scan"],
        }
    ]
    assert evaluate(requirements, ["ct_scan"]).passed is True
    assert evaluate(requirements, ["xray"]).passed is False
