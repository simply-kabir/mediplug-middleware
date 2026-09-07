"""
Phase 7 + 8 Part B — `finalize_case` orchestration tests.

Fully offline: every DB seam in `finalize` is monkeypatched to an
in-memory recorder, and a fake dispatcher is injected. What's under test
is the glue — transition order, when errors are recorded, what re-raises —
not the builder (test_fhir.py) or the payer (test_dispatch.py).
"""

import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mediplug.dispatch import DispatchResult
from mediplug.worker import finalize


class _FakeDispatcher:
    def __init__(self, result=None, exc=None):
        self._result = result
        self._exc = exc
        self.calls = []

    async def submit(self, bundle, stage):
        self.calls.append((bundle, stage))
        if self._exc is not None:
            raise self._exc
        return self._result


@pytest.fixture
def wired(monkeypatch):
    state = {
        "inputs": (
            {
                "patient": {"name": "P", "gender": "male", "birth_date": "1980-01-01"},
                "encounter": {
                    "attending_doctor": "D",
                    "doctor_registration_no": "R",
                    "hospital_id": "H",
                },
            },
            {"code": "S1A11.2", "name": "Lap Chole", "amount": 22000, "icd_code": "K80.2"},
            [],
            "preauth",
        ),
        "transitions": [],
        "errors": [],
        "bundle_persisted": None,
        "dispatch_persisted": None,
    }
    monkeypatch.setattr(finalize, "_load_inputs", lambda cid: state["inputs"])
    monkeypatch.setattr(
        finalize, "_transition", lambda cid, s, **f: state["transitions"].append(s)
    )
    monkeypatch.setattr(
        finalize, "_record_error", lambda cid, m: state["errors"].append(m)
    )
    monkeypatch.setattr(
        finalize, "_persist_bundle", lambda cid, b: state.__setitem__("bundle_persisted", b)
    )
    monkeypatch.setattr(
        finalize,
        "_persist_dispatch",
        lambda cid, b, r: state.__setitem__("dispatch_persisted", (b, r)),
    )
    return state


@pytest.mark.asyncio
async def test_happy_path_builds_dispatches_and_submits(wired):
    d = _FakeDispatcher(DispatchResult(True, "corr-123", {"result": "accepted"}))
    await finalize.finalize_case("case-1", dispatcher=d)

    assert wired["transitions"] == ["dispatching", "submitted"]
    assert wired["errors"] == []
    assert wired["bundle_persisted"]["resourceType"] == "Bundle"
    assert wired["dispatch_persisted"][1].correlation_id == "corr-123"
    assert d.calls[0][1] == "preauth"


@pytest.mark.asyncio
async def test_payer_rejection_lands_dispatch_failed_and_reraises(wired):
    d = _FakeDispatcher(
        DispatchResult(False, None, {"detail": "no diagnosis"}, error="HTTP 422: ...")
    )
    with pytest.raises(RuntimeError, match="dispatch rejected"):
        await finalize.finalize_case("case-1", dispatcher=d)

    assert wired["transitions"] == ["dispatching", "dispatch_failed"]
    assert any("dispatch rejected" in e for e in wired["errors"])
    # the payer's response is stored before the failure is raised
    assert wired["dispatch_persisted"] is not None


@pytest.mark.asyncio
async def test_transport_error_lands_dispatch_failed_and_reraises(wired):
    d = _FakeDispatcher(exc=RuntimeError("connection refused"))
    with pytest.raises(RuntimeError, match="connection refused"):
        await finalize.finalize_case("case-1", dispatcher=d)

    assert wired["transitions"] == ["dispatching", "dispatch_failed"]
    assert any("dispatch error" in e for e in wired["errors"])
    assert wired["dispatch_persisted"] is None  # never got a response


@pytest.mark.asyncio
async def test_validation_failure_records_error_and_stops_before_dispatch(wired, monkeypatch):
    def boom(bundle):
        raise ValidationError.from_exception_data("Bundle", [])

    monkeypatch.setattr(finalize, "validate", boom)
    d = _FakeDispatcher(DispatchResult(True, "x", {}))

    with pytest.raises(ValidationError):
        await finalize.finalize_case("case-1", dispatcher=d)

    assert wired["transitions"] == []          # never reached "dispatching"
    assert wired["bundle_persisted"] is None   # invalid bundle not persisted
    assert any("FHIR validation failed" in e for e in wired["errors"])
    assert d.calls == []                       # dispatcher never called


@pytest.mark.asyncio
async def test_stage_is_passed_through_to_dispatcher(wired):
    case, package, _docs, _stage = wired["inputs"]
    wired["inputs"] = (case, package, [], "claim")
    d = _FakeDispatcher(DispatchResult(True, "c", {"result": "accepted"}))

    await finalize.finalize_case("case-1", dispatcher=d)
    assert d.calls[0][1] == "claim"


@pytest.mark.asyncio
async def test_default_dispatcher_is_resolved_when_not_injected(wired, monkeypatch):
    d = _FakeDispatcher(DispatchResult(True, "c", {"result": "accepted"}))
    monkeypatch.setattr(finalize, "get_dispatcher", lambda: d)

    await finalize.finalize_case("case-1")
    assert d.calls and wired["transitions"] == ["dispatching", "submitted"]
