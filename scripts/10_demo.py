"""
Phase 10.2 — the executable demo driver.

Walks the guide's §12.1 beats over REAL HTTP against a running stack (start
it with `bash scripts/run_demo.sh`): ingest -> map -> pre-flight rules ->
action_required -> document upload -> FHIR build+validate -> dispatch ->
submitted -> adjudicate -> payer_approved. Every beat carries an assertion.

Unlike scripts/test_step7_8_demo.py (which drives process_case in-process),
this one goes over the wire — the point is to prove the actual pipes.

Usage:
    bash scripts/run_demo.sh            # in one terminal
    uv run python scripts/10_demo.py    # in another

Set CLEANUP = True for a rehearsal (deletes the demo row afterwards). Leave
it False for the live demo — the UI needs the row on screen.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import sys
from pathlib import Path

import httpx
import psycopg

from mediplug.config import settings
from mediplug.fhir import validate

GATEWAY = "http://localhost:8000"
MOCK_PAYER = "http://localhost:8081"
ADMIN_HEADERS = {"X-Admin-Token": settings.admin_token}

# The guide's exact demo note. Only the Aadhaar card is attached — the USG
# is deliberately withheld so the pre-flight engine has something to catch.
DEMO_NOTE = "45M, RUQ pain x 3 days, USG s/o cholelithiasis. Planned for lap chole."
SAFE_CODES = ("S11J13.1", "S8G5.11", "S9H5.1")  # packages known to carry an icd_code

CLEANUP = False
SAMPLES_DIR = Path(__file__).resolve().parents[1] / "data" / "samples"

BAR = "=" * 80


def _db():
    return psycopg.connect(settings.database_url, prepare_threshold=None)


def _row(case_id: str, *cols: str):
    with _db() as conn, conn.cursor() as cur:
        cur.execute(f"select {', '.join(cols)} from cases where id = %s", (case_id,))
        return cur.fetchone()


def _icd_for(code: str) -> str | None:
    with _db() as conn, conn.cursor() as cur:
        cur.execute("select icd_code from packages where code = %s", (code,))
        r = cur.fetchone()
        return r[0] if r else None


def _events(case_id: str):
    with _db() as conn, conn.cursor() as cur:
        cur.execute(
            "select from_status, to_status, actor, created_at "
            "from case_events where case_id = %s order by created_at",
            (case_id,),
        )
        return cur.fetchall()


async def _poll(client: httpx.AsyncClient, case_id: str, target: set[str], timeout=60.0):
    """Poll the cases row until status is in `target` (or a terminal failure)."""
    deadline = asyncio.get_event_loop().time() + timeout
    terminal = {"dispatch_failed", "failed", "payer_rejected"}
    last = None
    while asyncio.get_event_loop().time() < deadline:
        (status,) = _row(case_id, "status")
        if status != last:
            print(f"    … {status}")
            last = status
        if status in target:
            return status
        if status in terminal and status not in target:
            raise AssertionError(f"case landed terminal {status!r}, wanted one of {target}")
        await asyncio.sleep(1.0)
    raise AssertionError(f"timed out after {timeout}s at {last!r}, wanted {target}")


def _walk_reference_integrity(bundle: dict) -> int:
    """Every urn:uuid: reference must resolve to an entry.fullUrl; no bare-uuid
    references allowed. Returns the number of references checked."""
    full_urls = {e["fullUrl"] for e in bundle["entry"] if "fullUrl" in e}
    checked = 0

    def visit(node):
        nonlocal checked
        if isinstance(node, dict):
            ref = node.get("reference")
            if isinstance(ref, str):
                checked += 1
                assert ref.startswith("urn:uuid:"), f"bare / non-urn reference: {ref!r}"
                assert ref in full_urls, f"dangling reference: {ref!r}"
            for v in node.values():
                visit(v)
        elif isinstance(node, list):
            for v in node:
                visit(v)

    visit(bundle)
    return checked


async def main() -> None:
    print(BAR)
    print("  MediPlug — Phase 10 end-to-end demo (real HTTP against the running stack)")
    print(BAR)

    async with httpx.AsyncClient(timeout=30) as client:
        # ---- STEP 1: preflight ------------------------------------------
        print("\n>>> STEP 1: Preflight — gateway, admin queue, mock payer")
        h = await client.get(f"{GATEWAY}/health")
        assert h.status_code == 200, h.text
        q = await client.get(f"{GATEWAY}/admin/queue", headers=ADMIN_HEADERS)
        assert q.status_code == 200, q.text
        print(f"    gateway ok · queue={q.json()['stream_length']} pending={q.json()['pending']} dlq={q.json()['dlq_length']}")
        hz = await client.get(f"{MOCK_PAYER}/healthz")
        assert hz.status_code == 200, hz.text
        print(f"    mock payer ok · received so far: {hz.json().get('received')}")

        # ---- STEP 2: ingest (Aadhaar only, USG withheld) ---------------
        print("\n>>> STEP 2: Ingest the demo case — only the Aadhaar card attached")
        idem = f"demo-{secrets.token_hex(6)}"
        payload = {
            "hms_case_ref": f"DEMO-{secrets.token_hex(3).upper()}",
            "stage": "preauth",
            "patient": {"name": "Ramesh Patil", "gender": "male", "birth_date": "1981-04-12"},
            "encounter": {
                "admission_date": "2026-09-07",
                "attending_doctor": "Dr. A. Kulkarni",
                "doctor_registration_no": "MH-99123",
                "hospital_id": "HOSP-PUNE-004",
            },
            "clinical_notes": DEMO_NOTE,
            "documents": [
                {"document_type": "aadhaar_card", "file_url": "https://example.test/aadhaar.pdf"}
            ],
        }
        r = await client.post(
            f"{GATEWAY}/api/v1/cases/ingest",
            json=payload,
            headers={"Idempotency-Key": idem},
        )
        assert r.status_code == 202, f"{r.status_code}: {r.text}"
        case_id = r.json()["case_id"]
        print(f"    case_id     = {case_id}")
        print(f"    tracking_ref = {r.json()['tracking_ref']}")

        # ---- STEP 3: poll to action_required, print missing reqs -------
        print("\n>>> STEP 3: Wait for the pre-flight engine — THE MONEY MOMENT")
        status = await _poll(client, case_id, {"action_required", "needs_code_confirmation"})
        code, name, missing = _row(case_id, "mapped_package_code", "mapped_package_name", "missing_requirements")

        if status == "needs_code_confirmation":
            # Borderline confidence — confirm a known-safe package to keep moving.
            code = SAFE_CODES[1]
            print(f"    borderline mapping → confirming {code} via /confirm-code")
            cc = await client.post(
                f"{GATEWAY}/api/v1/cases/{case_id}/confirm-code",
                json={"code": code, "confirmed_by": "demo-script"},
            )
            assert cc.status_code == 200, cc.text
            status = await _poll(client, case_id, {"action_required", "ready_for_dispatch", "submitted"})
            code, name, missing = _row(
                case_id, "mapped_package_code", "mapped_package_name", "missing_requirements"
            )

        print(f"    mapped package: {code}  ({name})")
        icd = _icd_for(code) if code else None
        if not icd:
            print(f"\n  ✗ mapped package {code!r} has NO icd_code — the mock payer will 422 on")
            print(f"    missing diagnosis. Re-run against a known-safe package: {', '.join(SAFE_CODES)}")
            print(f"    case_id (for cleanup): {case_id}")
            sys.exit(2)
        print(f"    icd_code: {icd}  ✓ (mock payer will accept the diagnosis)")

        missing = missing or []
        if status == "action_required":
            assert missing, "action_required but missing_requirements is empty?"
            print("    missing_requirements:")
            for req in missing:
                print(f"      - {req.get('human_label', req.get('requirement_id'))}")
        else:
            print(f"    (rules already satisfied — status {status})")

        # ---- STEP 4: upload the missing docs (the USG etc.) -----------
        if status == "action_required":
            print("\n>>> STEP 4: Aarogyamitra uploads the missing documents (USG s/o cholelithiasis)")
            docs = []
            for req in missing:
                opts = req.get("any_of", [])
                opt = opts[0] if opts else None
                dtype = opt.get("code") if isinstance(opt, dict) else (opt or "usg_abdomen")
                docs.append({"document_type": dtype, "file_url": f"https://example.test/{dtype}.pdf"})
            up = await client.post(
                f"{GATEWAY}/api/v1/cases/{case_id}/documents",
                json={"documents": docs, "uploaded_by": "demo-script"},
            )
            assert up.status_code == 200, up.text
            print(f"    uploaded {len(docs)} doc(s) → trigger=docs_updated re-enqueued")

        # ---- STEP 5: poll to submitted, validate the STORED bundle ----
        print("\n>>> STEP 5: FHIR build + validate + dispatch → submitted")
        await _poll(client, case_id, {"submitted"}, timeout=120.0)
        fhir_bundle, corr = _row(case_id, "fhir_bundle", "payer_correlation_id")
        assert fhir_bundle is not None, "reached submitted but cases.fhir_bundle is NULL"
        assert corr, "reached submitted but payer_correlation_id is NULL"
        validate(fhir_bundle)  # re-validate the persisted bundle, not a fresh one
        n_refs = _walk_reference_integrity(fhir_bundle)
        print(f"    fhir_bundle: {len(fhir_bundle['entry'])} entries, {n_refs} internal references all resolve ✓")
        print(f"    payer_correlation_id: {corr}")

        # ---- STEP 6: adjudicate → payer_approved ----------------------
        print("\n>>> STEP 6: Payer adjudicates — approve")
        adj = await client.post(f"{MOCK_PAYER}/adjudicate/{corr}", json={"outcome": "approved"})
        assert adj.status_code == 200, adj.text
        await _poll(client, case_id, {"payer_approved"}, timeout=30.0)
        print("    → payer_approved  ✓")

        # ---- STEP 7: the audit trail + save the bundle ----------------
        print("\n>>> STEP 7: Full case_events trail")
        for frm, to, actor, ts in _events(case_id):
            frm_s = frm or "—"
            print(f"    {str(ts)[:19]}  {frm_s:>24} → {to:<24} [{actor}]")

        SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
        out = SAMPLES_DIR / f"demo_bundle_{code}.json"
        out.write_text(json.dumps(fhir_bundle, indent=2))
        print(f"\n    wrote {out.relative_to(SAMPLES_DIR.parents[1])}")

        if CLEANUP:
            with _db() as conn, conn.cursor() as cur:
                cur.execute("delete from cases where id = %s", (case_id,))
                conn.commit()
            print("    CLEANUP=True → demo row deleted")
        else:
            print(f"    CLEANUP=False → row kept for the UI. case_id: {case_id}")

    print("\n" + BAR)
    print("  DEMO PASSED — ingest → map → rules → FHIR → dispatch → submitted → payer_approved")
    print(BAR)


if __name__ == "__main__":
    asyncio.run(main())
