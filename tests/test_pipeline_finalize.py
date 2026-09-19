"""
Wiring test — `process_case` calls `finalize_case` exactly on the
Phase 6 success path (`ready_for_dispatch`), and not on any other route.

Fully offline: every DB/CPU seam in `pipeline` is monkeypatched. The
rule-engine, mapper, FHIR builder and dispatcher each have their own
tests; this only pins the one integration point Phase 7/8 added.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mediplug.rules.engine import RuleEvaluation
from mediplug.schemas import CodeCandidate
from mediplug.worker import pipeline


@pytest.fixture
def wired(monkeypatch):
    state = {
        "statuses": [],
        "finalized": [],
        "case_state": ("lap chole for cholelithiasis", None, None),
        "candidates": [
            CodeCandidate(code="S8G5.11", name="Lap Chole", confidence=0.95)
        ],
        "rules": RuleEvaluation(passed=True, missing_requirements=[]),
    }

    def _set_status(case_id, new_status, **extra):
        state["statuses"].append(new_status)

    monkeypatch.setattr(pipeline, "_set_status", _set_status)
    monkeypatch.setattr(pipeline, "_read_case_state", lambda cid: state["case_state"])
    monkeypatch.setattr(pipeline, "map_notes", lambda notes, k: state["candidates"])
    monkeypatch.setattr(
        pipeline,
        "_check_integrity_and_rules",
        lambda cid, stage, code, pat, enc: (
            pipeline.IntegrityCheckResult(passed=True),
            state["rules"],
        ),
    )
    monkeypatch.setattr(
        pipeline,
        "_check_collision_db",
        lambda cid, pat, enc: (True, None),
    )

    async def _finalize(case_id):
        state["finalized"].append(case_id)

    monkeypatch.setattr(pipeline, "finalize_case", _finalize)
    return state


@pytest.mark.asyncio
async def test_finalize_called_on_auto_accept_success(wired):
    await pipeline.process_case({"case_id": "c1", "stage": "preauth"})

    assert "ready_for_dispatch" in wired["statuses"]
    assert wired["finalized"] == ["c1"]


@pytest.mark.asyncio
async def test_finalize_not_called_when_rules_fail(wired):
    wired["rules"] = RuleEvaluation(passed=False, missing_requirements=["ecg"])

    await pipeline.process_case({"case_id": "c1", "stage": "preauth"})

    assert wired["statuses"][-1] == "action_required"
    assert wired["finalized"] == []


@pytest.mark.asyncio
async def test_finalize_not_called_on_needs_confirmation(wired):
    wired["candidates"] = [
        CodeCandidate(code="S8G5.11", name="Lap Chole", confidence=0.60)
    ]

    await pipeline.process_case({"case_id": "c1", "stage": "preauth"})

    assert "needs_code_confirmation" in wired["statuses"]
    assert wired["finalized"] == []


@pytest.mark.asyncio
async def test_finalize_called_after_code_confirmed_rules_pass(wired):
    wired["case_state"] = ("notes", "S8G5.11", "2026-09-07T00:00:00Z")

    await pipeline.process_case(
        {"case_id": "c1", "stage": "preauth", "trigger": "code_confirmed"}
    )

    assert wired["statuses"] == ["analyzing", "ready_for_dispatch"]
    assert wired["finalized"] == ["c1"]
