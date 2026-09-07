"""
Step 7 & Step 8: Live End-to-End Demo and Verification of Phase 6.

Demonstrates:
  Step 7: Lap Chole Demo Case:
    1. Ingest with notes: '45M, RUQ pain x3 days, USG s/o cholelithiasis, planned for lap chole'
       with only Aadhaar card attached (omitting both required clinical docs).
    2. Confirm package code or auto-accept -> evaluates pre-flight rules.
    3. Rules fail -> status transitions to 'action_required' with missing requirements.
    4. Aarogyamitra uploads all missing required documents via POST /api/v1/cases/{case_id}/documents.
    5. Worker re-evaluates via trigger='docs_updated' -> transitions to 'ready_for_dispatch'!
  
  Step 8: Zero-requirement package drill:
    1. Case mapping to zero-requirement package.
    2. Sails directly to 'ready_for_dispatch' with empty missing_requirements list without error.
"""

import asyncio
import json
import secrets
import httpx
import psycopg
from psycopg.types.json import Json

from mediplug.config import settings
from mediplug.gateway.main import app, lifespan
from mediplug.worker.pipeline import process_case
from mediplug.mapping.mapper import map_notes

async def run_step7_and_8():
    print("================================================================================")
    print("                     PHASE 6 LIVE END-TO-END DEMO TEST                         ")
    print("================================================================================")

    # --------------------------------------------------------------------------
    # Step 7: The Money Moment (Action Required -> Upload -> Ready for Dispatch)
    # --------------------------------------------------------------------------
    print("\n>>> STEP 7: Lap Chole Ingestion with Missing Documents...")
    
    notes = "45M, RUQ pain x3 days, USG s/o cholelithiasis, planned for laparoscopic cholecystectomy"
    candidates = map_notes(notes, top_k=3)
    best_candidate = candidates[0]
    print(f"  AI Mapping resolved: [{best_candidate.code}] {best_candidate.name} (Confidence: {best_candidate.confidence})")

    # Inspect package requirements in DB
    with psycopg.connect(settings.database_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT requirements FROM packages WHERE code = %s", (best_candidate.code,))
        pkg_reqs = cur.fetchone()[0] or {}
        pre_reqs = pkg_reqs.get("preauth", [])
    print(f"  Package has {len(pre_reqs)} preauth requirement(s): {[r.get('human_label') for r in pre_reqs]}")

    target_code = best_candidate.code
    target_name = best_candidate.name

    tracking_ref = f"DEMO-P6-{secrets.token_hex(4).upper()}"
    idemp_key = f"idemp-p6-{secrets.token_hex(4)}"

    # Ingest case with ONLY Aadhaar card (deliberately missing the required clinical docs)
    with psycopg.connect(settings.database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO cases (
                tracking_ref, idempotency_key, stage, status,
                patient, encounter, raw_clinical_notes,
                mapped_package_code, mapped_package_name, code_confirmed_at
            )
            VALUES (%s, %s, 'preauth', 'queued', %s, %s, %s, %s, %s, now())
            RETURNING id;
            """,
            (
                tracking_ref, idemp_key,
                Json({"name": "Ramesh K. Joshi", "gender": "male", "birth_date": "1981-04-10"}),
                Json({"admission_date": "2026-09-07", "attending_doctor": "Dr. V. Patil", "hospital_id": "HOSP-PUNE-01"}),
                notes, target_code, target_name
            )
        )
        case_id = str(cur.fetchone()[0])

        # Attach ONLY Aadhaar Card
        cur.execute(
            """
            INSERT INTO case_documents (case_id, document_type, file_url, file_name)
            VALUES (%s, 'aadhaar_card', 'https://storage.hospital.org/claims/aadhaar.pdf', 'aadhaar.pdf');
            """,
            (case_id,)
        )
        conn.commit()

    print(f"  Ingested Case ID: {case_id} ({tracking_ref})")
    print("  Uploaded Documents: ['aadhaar_card'] (Required clinical documents are missing!)")

    # Step 7.1: Worker processes the case with trigger='code_confirmed' (or auto-accept)
    print("\n  [Worker] Running pre-flight rule validation...")
    await process_case({"case_id": case_id, "stage": "preauth", "trigger": "code_confirmed"})

    # Check database state
    with psycopg.connect(settings.database_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT status, missing_requirements FROM cases WHERE id = %s", (case_id,))
        status, missing = cur.fetchone()

    print(f"\n  Case State after Pre-Flight Rule Check:")
    print(f"    Status:               {status}")
    print(f"    Missing Requirements count: {len(missing)}")
    for m in missing:
        print(f"      - {m.get('human_label')}")
    assert status == "action_required", f"Expected action_required, got {status}"
    assert len(missing) > 0, "Expected missing requirements to be populated"
    print("   Correctly caught missing documents and halted at action_required!")

    # Step 7.2: Aarogyamitra uploads all missing required documents through POST /api/v1/cases/{case_id}/documents
    docs_to_upload = []
    for req in missing:
        first_opt = req["any_of"][0]
        doc_code = first_opt["code"] if isinstance(first_opt, dict) else first_opt
        docs_to_upload.append({
            "document_type": doc_code,
            "file_url": f"https://storage.hospital.org/claims/{doc_code}.pdf"
        })

    print(f"\n>>> Aarogyamitra Operator Uploads Missing Documents: {[d['document_type'] for d in docs_to_upload]}...")

    upload_payload = {
        "documents": docs_to_upload,
        "uploaded_by": "aarogyamitra_nurse_1"
    }

    async with lifespan(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            upload_res = await client.post(f"/api/v1/cases/{case_id}/documents", json=upload_payload)
            assert upload_res.status_code == 200, f"Upload failed: {upload_res.text}"
            print(f"  Gateway accepted upload: {upload_res.json()}")

    # Step 7.3: Worker automatically consumes trigger='docs_updated'
    print("\n  [Worker] Consuming 'docs_updated' job from Redis stream...")
    await process_case({"case_id": case_id, "stage": "preauth", "trigger": "docs_updated"})

    # Verify final state
    with psycopg.connect(settings.database_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT status, missing_requirements FROM cases WHERE id = %s", (case_id,))
        final_status, final_missing = cur.fetchone()
        cur.execute(
            "SELECT from_status, to_status, actor, created_at FROM case_events WHERE case_id = %s ORDER BY id ASC",
            (case_id,)
        )
        events = cur.fetchall()

    print(f"\n  Final Case State in Supabase:")
    print(f"    Status:               {final_status}")
    print(f"    Missing Requirements: {final_missing}")
    assert final_status == "ready_for_dispatch", f"Expected ready_for_dispatch, got {final_status}"
    assert final_missing == [], "Expected empty missing_requirements"

    print("\n  Audit Trail of the Money Moment:")
    for ev in events:
        print(f"    - {ev[0]} -> {ev[1]} (by {ev[2]})")

    print("\n STEP 7 PASSED: action_required -> upload -> ready_for_dispatch works completely end-to-end!")

    # --------------------------------------------------------------------------
    # Step 8: Zero-Requirement Package Drill
    # --------------------------------------------------------------------------
    print("\n================================================================================")
    print(">>> STEP 8: Zero-Requirement Package Failure Drill...")

    # Find a package with 0 preauth requirements
    with psycopg.connect(settings.database_url) as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT code, name FROM packages 
            WHERE requirements = '{}'::jsonb 
               OR (requirements ? 'preauth' AND jsonb_array_length(requirements->'preauth') = 0)
            LIMIT 1;
        """)
        zero_code, zero_name = cur.fetchone()

    print(f"  Testing with zero-requirement package: [{zero_code}] {zero_name}")
    
    zero_ref = f"ZERO-P6-{secrets.token_hex(4).upper()}"
    with psycopg.connect(settings.database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO cases (
                tracking_ref, idempotency_key, stage, status,
                patient, encounter, raw_clinical_notes,
                mapped_package_code, mapped_package_name, code_confirmed_at
            )
            VALUES (%s, %s, 'preauth', 'queued', %s, %s, %s, %s, %s, now())
            RETURNING id;
            """,
            (
                zero_ref, f"idemp-zero-{secrets.token_hex(4)}",
                Json({"name": "Zero Req Patient"}),
                Json({"hospital_id": "HOSP-01"}),
                "surgical repair notes", zero_code, zero_name
            )
        )
        zero_case_id = str(cur.fetchone()[0])
        conn.commit()

    # Process through pipeline with code_confirmed
    await process_case({"case_id": zero_case_id, "stage": "preauth", "trigger": "code_confirmed"})

    with psycopg.connect(settings.database_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT status, missing_requirements FROM cases WHERE id = %s", (zero_case_id,))
        z_status, z_missing = cur.fetchone()

        # Clean up demo cases
        cur.execute("DELETE FROM cases WHERE id IN (%s, %s)", (case_id, zero_case_id))
        conn.commit()

    print(f"  Zero-Requirement Case Final Status: {z_status}")
    print(f"  Missing Requirements:              {z_missing}")
    assert z_status == "ready_for_dispatch", f"Expected ready_for_dispatch, got {z_status}"
    assert z_missing == [], "Expected empty missing_requirements"

    print("\n STEP 8 PASSED: Zero-requirement packages sail directly to ready_for_dispatch without error!")
    print("================================================================================")

if __name__ == "__main__":
    asyncio.run(run_step7_and_8())
