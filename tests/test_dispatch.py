"""
Phase 8 — dispatch tests. Fully offline: the mock payer app is driven
in-process (FastAPI TestClient / httpx ASGITransport), no server, no
network, no DB.
"""

import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from mediplug.dispatch import DispatchResult, get_dispatcher
from mediplug.dispatch.mock import MockPayerDispatcher
from mediplug.fhir import build_claim_bundle
from mock_payer.main import RECEIVED, app


def _bundle(icd_code="K80.2", stage="preauth"):
    case = {
        "patient": {"name": "Test Patient", "gender": "male", "birth_date": "1980-01-01"},
        "encounter": {
            "attending_doctor": "Dr A",
            "doctor_registration_no": "R1",
            "hospital_id": "H1",
        },
    }
    package = {"code": "S1A11.2", "name": "Lap.Cholecystectomy", "amount": 22000, "icd_code": icd_code}
    return build_claim_bundle(case, package, [], stage)


@pytest.fixture(autouse=True)
def _clear_received():
    RECEIVED.clear()
    yield
    RECEIVED.clear()


# --------------------------------------------------------------------------
# mock payer app — validates for real
# --------------------------------------------------------------------------

def test_mock_payer_accepts_valid_bundle():
    client = TestClient(app)
    resp = client.post("/v0.7/preauth/submit", json={"bundle": _bundle()})
    assert resp.status_code == 202
    body = resp.json()
    assert body["result"] == "accepted"
    assert body["correlation_id"] in RECEIVED


def test_mock_payer_rejects_missing_diagnosis():
    bundle = _bundle(icd_code=None)  # builder omits diagnosis entirely
    client = TestClient(app)
    resp = client.post("/v0.7/preauth/submit", json={"bundle": bundle})
    assert resp.status_code == 422
    assert "diagnosis" in resp.json()["detail"]


def test_mock_payer_rejects_non_bundle():
    client = TestClient(app)
    resp = client.post("/v0.7/preauth/submit", json={"bundle": {"resourceType": "Claim"}})
    assert resp.status_code == 400


def test_mock_payer_rejects_unknown_flow():
    client = TestClient(app)
    resp = client.post("/v0.7/nonsense/submit", json={"bundle": _bundle()})
    assert resp.status_code == 404


def test_mock_payer_adjudicate_flow():
    client = TestClient(app)
    cid = client.post("/v0.7/claim/submit", json={"bundle": _bundle(stage="claim")}).json()[
        "correlation_id"
    ]

    approved = client.post(f"/adjudicate/{cid}", json={"outcome": "approved"})
    assert approved.status_code == 200
    assert approved.json()["outcome"] == "approved"
    assert RECEIVED[cid]["outcome"] == "approved"

    rejected = client.post(f"/adjudicate/{cid}", json={"outcome": "rejected"})
    assert rejected.json()["outcome"] == "rejected"

    assert client.post("/adjudicate/does-not-exist").status_code == 404


def test_mock_payer_adjudicate_defaults_to_approved():
    client = TestClient(app)
    cid = client.post("/v0.7/preauth/submit", json={"bundle": _bundle()}).json()["correlation_id"]
    assert client.post(f"/adjudicate/{cid}").json()["outcome"] == "approved"


# --------------------------------------------------------------------------
# MockPayerDispatcher — against the app over an ASGI transport
# --------------------------------------------------------------------------

def _dispatcher():
    return MockPayerDispatcher(
        base_url="http://payer.test",
        transport=httpx.ASGITransport(app=app),
    )


@pytest.mark.asyncio
async def test_dispatcher_submit_accepted():
    result = await _dispatcher().submit(_bundle(), "preauth")
    assert isinstance(result, DispatchResult)
    assert result.accepted is True
    assert result.correlation_id
    assert result.raw_response["result"] == "accepted"
    assert result.error is None


@pytest.mark.asyncio
async def test_dispatcher_submit_rejected_on_missing_diagnosis():
    result = await _dispatcher().submit(_bundle(icd_code=None), "preauth")
    assert result.accepted is False
    assert result.correlation_id is None
    assert "422" in result.error


@pytest.mark.asyncio
async def test_dispatcher_routes_stage_to_flow():
    await _dispatcher().submit(_bundle(stage="claim"), "claim")
    assert [r["flow"] for r in RECEIVED.values()] == ["claim"]


def test_get_dispatcher_defaults_to_mock():
    assert isinstance(get_dispatcher(), MockPayerDispatcher)
