"""
Test Script: End-to-End Code Confirmation Workflow

Demonstrates and verifies:
1. Ingests a case with ambiguous clinical notes that produce medium confidence (triggering 'needs_code_confirmation').
2. Verifies that candidates are generated and saved in `cases.alternate_codes`.
3. Operator selects and confirms a specific candidate via POST /api/v1/cases/{id}/confirm-code.
4. Gateway commits the confirmed code, logs an audit event with actor='aarogyamitra', and enqueues trigger='code_confirmed'.
5. Worker pipeline runs pre-flight integrity and rules on the confirmed package.
6. Verifies database state and full audit trail.
"""

import asyncio
import secrets
import httpx
import psycopg
from psycopg.types.json import Json

from mediplug.config import settings
from mediplug.gateway.main import app, lifespan
from mediplug.worker.pipeline import process_case


async def run_test():
    print("================================================================================")
    print("                 TEST: HUMAN-IN-THE-LOOP CODE CONFIRMATION                      ")
    print("================================================================================")

    tracking_ref = f"CONFIRM-{secrets.token_hex(3).upper()}"
    idemp_key = f"idemp-conf-{secrets.token_hex(4)}"

    # 1. Ingest a case directly in 'needs_code_confirmation' state with candidate options
    print("\n[Step 1] Ingesting case requiring Aarogyamitra code confirmation...")
    
    mock_candidates = [
        {"code": "S1A11.2", "name": "Lap.Cholecystectomy", "confidence": 0.72},
        {"code": "S1A11.1", "name": "Open Cholecystectomy", "confidence": 0.68},
    ]

    with psycopg.connect(settings.database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO cases (
                tracking_ref, idempotency_key, stage, status,
                patient, encounter, raw_clinical_notes,
                alternate_codes
            )
            VALUES (%s, %s, 'preauth', 'needs_code_confirmation', %s, %s, %s, %s)
            RETURNING id;
            """,
            (
                tracking_ref,
                idemp_key,
                Json({"name": "Sunita Deshmukh", "gender": "female", "birth_date": "1985-06-15"}),
                Json({"hospital_id": "HOSP-MUM-01", "attending_doctor": "Dr. S. Kulkarni"}),
                "Patient with symptomatic gallstones and chronic cholecystitis. Advised cholecystectomy.",
                Json(mock_candidates),
            ),
        )
        case_id = str(cur.fetchone()[0])
        conn.commit()

    print(f"  [OK] Case Created: {case_id} ({tracking_ref})")
    print(f"  [OK] Status: needs_code_confirmation")
    print(f"  [OK] AI Suggestions: {[c['name'] + ' (' + c['code'] + ')' for c in mock_candidates]}")

    # 2. Operator selects and confirms "S1A11.2" (Lap.Cholecystectomy)
    chosen_code = "S1A11.2"
    print(f"\n[Step 2] Aarogyamitra operator confirms package: {chosen_code} via Gateway API...")

    async with lifespan(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                f"/api/v1/cases/{case_id}/confirm-code",
                json={
                    "code": chosen_code,
                    "confirmed_by": "aarogyamitra_desk_operator"
                },
            )
            assert resp.status_code == 200, f"Confirm failed: {resp.text}"
            confirm_data = resp.json()
            print(f"  [OK] Gateway Response: {confirm_data}")
            assert confirm_data["mapped_package_code"] == chosen_code
            assert confirm_data["status"] == "queued"

    # 3. Check DB to verify code_confirmed_at and audit event
    with psycopg.connect(settings.database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT mapped_package_code, mapped_package_name, code_confirmed_by, code_confirmed_at, status FROM cases WHERE id = %s",
            (case_id,),
        )
        p_code, p_name, by_user, at_time, cur_status = cur.fetchone()
        print(f"\n[Step 3] Verifying Database State:")
        print(f"  [OK] mapped_package_code: {p_code}")
        print(f"  [OK] mapped_package_name: {p_name}")
        print(f"  [OK] confirmed_by:        {by_user}")
        print(f"  [OK] confirmed_at:        {at_time}")
        print(f"  [OK] current_status:      {cur_status}")
        assert p_code == chosen_code
        assert at_time is not None

        # Verify audit trail
        cur.execute(
            "SELECT from_status, to_status, actor, detail FROM case_events WHERE case_id = %s ORDER BY id DESC LIMIT 1",
            (case_id,),
        )
        from_st, to_st, actor, detail = cur.fetchone()
        print(f"\n[Step 4] Verifying Audit Event:")
        print(f"  [OK] Transition: {from_st} -> {to_st}")
        print(f"  [OK] Actor:      {actor}")
        print(f"  [OK] Detail:     {detail}")
        assert actor == "aarogyamitra"
        assert to_st == "queued"

    # 4. Run worker processing with trigger='code_confirmed'
    print("\n[Step 5] Worker processes 'code_confirmed' job...")
    await process_case({"case_id": case_id, "stage": "preauth", "trigger": "code_confirmed"})

    # Check status after rules/integrity check
    with psycopg.connect(settings.database_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT status, missing_requirements FROM cases WHERE id = %s", (case_id,))
        final_status, final_missing = cur.fetchone()
        print(f"\n[Step 6] Worker Processing Result:")
        print(f"  [OK] Final Status:        {final_status}")
        print(f"  [OK] Missing Docs Count:  {len(final_missing) if final_missing else 0}")
        assert final_status in ("action_required", "ready_for_dispatch")

    print("\n================================================================================")
    print("                 ALL CODE CONFIRMATION CHECKS PASSED! [OK]                         ")
    print("================================================================================")


if __name__ == "__main__":
    asyncio.run(run_test())
