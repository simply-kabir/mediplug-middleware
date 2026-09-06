"""
Tests for abbreviation expansion and requirement normalization.
"""

import sys
from pathlib import Path
import pytest

# Ensure src and scripts are importable
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from mediplug.mapping.abbreviations import expand
from importlib import import_module

norm_module = import_module("02_normalize_requirements")
canonicalize = norm_module.canonicalize
parse_requirement_string = norm_module.parse_requirement_string


def test_abbreviation_expansion():
    cases = {
        "lap chole": "laparoscopic cholecystectomy",
        "lap appy": "laparoscopic appendicectomy",
        "k/c/o CAD planned for CABG": "known case of coronary artery disease planned for coronary artery bypass graft",
        "ruq pain s/o cholelithiasis": "right upper quadrant pain suggestive of cholelithiasis",
        "# femur posted for ORIF": "fracture femur posted for open reduction internal fixation",
    }
    for raw, expected in cases.items():
        assert expand(raw).lower() == expected.lower()


def test_canonicalize_exact_and_alias():
    res = canonicalize("USG Abdomen")
    assert res["code"] == "usg_abdomen"
    assert not res.get("unmapped", False)

    res_alias = canonicalize("sonography")
    assert res_alias["code"] == "usg"

    res_xray = canonicalize("skiagram")
    assert res_xray["code"] == "xray"


def test_parse_requirement_string():
    raw = "USG Abdomen / CT Abdomen, CBC Report"
    reqs = parse_requirement_string(raw, stage="preauth")
    
    assert len(reqs) == 2
    assert reqs[0]["stage"] == "preauth"
    # First requirement should have 2 options (USG or CT)
    assert len(reqs[0]["any_of"]) == 2
    codes_0 = [opt["code"] for opt in reqs[0]["any_of"]]
    assert "usg_abdomen" in codes_0
    assert "ct_abdomen" in codes_0
    
    # Second requirement should have CBC
    codes_1 = [opt["code"] for opt in reqs[1]["any_of"]]
    assert "cbc" in codes_1


def test_parse_empty_requirement():
    assert parse_requirement_string("", stage="preauth") == []
    assert parse_requirement_string("N/A", stage="preauth") == []
    assert parse_requirement_string("-", stage="preauth") == []
