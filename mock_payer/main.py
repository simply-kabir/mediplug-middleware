"""
Mock MJPJAY payer — a stand-in for the NHCX/HCX gateway, run separately.

A mock that always returns 200 proves nothing. This one **actually
validates** the FHIR Claim: a Bundle whose Claim is missing
`diagnosis` / `patient` / `provider` / `insurer` / `insurance` / `item`
comes back 422. Rejecting a real structural gap in a demo is worth far
more than a stub that echoes 200.

Run it:
    uv run uvicorn mock_payer.main:app --port 8081
    # or:  docker compose up mock_payer

Endpoints:
    GET  /healthz                     -> {"status": "ok"}
    POST /v0.7/{flow}/submit          -> 202 + correlation_id, or 4xx
    POST /adjudicate/{correlation_id} -> demo control: approve / reject a
                                         received claim
    GET  /received                    -> everything submitted so far
"""

from __future__ import annotations

import uuid

from fastapi import FastAPI, HTTPException

app = FastAPI(title="Mock MJPJAY Payer", version="0.7")

# correlation_id -> {"flow", "code", "claim", "outcome"}
RECEIVED: dict[str, dict] = {}

REQUIRED_CLAIM_FIELDS = ("patient", "provider", "insurer", "insurance", "item")


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok", "received": len(RECEIVED)}


@app.post("/v0.7/{flow}/submit", status_code=202)
async def submit(flow: str, payload: dict) -> dict:
    if flow not in ("preauth", "claim"):
        raise HTTPException(404, f"unknown flow {flow!r}")

    bundle = payload.get("bundle") or payload
    if bundle.get("resourceType") != "Bundle":
        raise HTTPException(400, "expected a FHIR Bundle")

    claim = next(
        (
            e["resource"]
            for e in bundle.get("entry", [])
            if e.get("resource", {}).get("resourceType") == "Claim"
        ),
        None,
    )
    if claim is None:
        raise HTTPException(400, "Bundle contains no Claim resource")

    for f in REQUIRED_CLAIM_FIELDS:
        if not claim.get(f):
            raise HTTPException(422, f"Claim.{f} is required")
    if not claim.get("diagnosis"):
        raise HTTPException(422, "Claim.diagnosis (ICD-10) is required")

    code = claim["item"][0]["productOrService"]["coding"][0]["code"]
    correlation_id = str(uuid.uuid4())
    RECEIVED[correlation_id] = {
        "flow": flow,
        "code": code,
        "claim": claim,
        "outcome": "pending",
    }
    return {
        "timestamp": None,
        "correlation_id": correlation_id,
        "api_call_id": str(uuid.uuid4()),
        "result": "accepted",
    }


@app.post("/adjudicate/{correlation_id}")
async def adjudicate(correlation_id: str, body: dict | None = None) -> dict:
    """Demo control. Body: {"outcome": "approved" | "rejected"}.
    Defaults to "approved" so the happy path is a one-liner."""
    record = RECEIVED.get(correlation_id)
    if record is None:
        raise HTTPException(404, "unknown correlation_id")

    outcome = (body or {}).get("outcome", "approved")
    if outcome not in ("approved", "rejected"):
        raise HTTPException(400, "outcome must be 'approved' or 'rejected'")

    record["outcome"] = outcome
    return {
        "correlation_id": correlation_id,
        "outcome": outcome,
        "disposition": (
            "Package verified against MJPJAY master"
            if outcome == "approved"
            else "Rejected on adjudication"
        ),
    }


@app.get("/received")
async def received() -> dict:
    return {
        cid: {"flow": r["flow"], "code": r["code"], "outcome": r["outcome"]}
        for cid, r in RECEIVED.items()
    }
