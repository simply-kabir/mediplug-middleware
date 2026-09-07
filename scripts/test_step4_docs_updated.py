"""
Test Step 4: Verify 'docs_updated' trigger handling in pipeline.py.
"""

import asyncio
import secrets
import psycopg
from psycopg.types.json import Json
from mediplug.config import settings
from mediplug.worker.pipeline import process_case

async def run_test():
    test_ref = f"TEST-STEP4-{secrets.token_hex(4).upper()}"
    idemp = f"idemp-{secrets.token_hex(4)}"
    
    # We will test with package S10I11.2 (Micro Vascular Decompression)
    # which requires CT Scan or MRI
    test_pkg_code = "S10I11.2"
    test_pkg_name = "Micro Vascular Decompression"

    with psycopg.connect(settings.database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO cases (
                tracking_ref, idempotency_key, stage, status,
                patient, encounter, raw_clinical_notes,
                mapped_package_code, mapped_package_name, code_confirmed_at
            )
            VALUES (%s, %s, 'preauth', 'action_required', %s, %s, %s, %s, %s, now())
            RETURNING id;
            """,
            (
                test_ref, idemp,
                Json({"name": "Test Step4"}),
                Json({"hospital_id": "HOSP-01"}),
                "test notes for micro vascular decompression",
                test_pkg_code, test_pkg_name
            )
        )
        case_id = str(cur.fetchone()[0])
        conn.commit()

    print(f"Created test case {case_id} ({test_ref}) with code {test_pkg_code}")

    # Part A: Run docs_updated with NO documents attached
    print("\n--- Part A: Calling process_case with trigger='docs_updated' (no documents) ---")
    await process_case({"case_id": case_id, "stage": "preauth", "trigger": "docs_updated"})

    with psycopg.connect(settings.database_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT status, missing_requirements FROM cases WHERE id = %s", (case_id,))
        status, missing = cur.fetchone()
        print(f"Result without doc -> Status: {status} | Missing count: {len(missing)}")
        assert status == "action_required"
        assert len(missing) > 0

    # Part B: Now manually insert the required document (CT Scan)
    print("\n--- Part B: Inserting 'ct_scan' into case_documents ---")
    with psycopg.connect(settings.database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO case_documents (case_id, document_type, file_url, file_name)
            VALUES (%s, 'ct_scan', 'https://example.com/ct.pdf', 'ct.pdf')
            """,
            (case_id,)
        )
        conn.commit()

    # Part C: Trigger docs_updated again
    print("\n--- Part C: Calling process_case with trigger='docs_updated' (document present) ---")
    await process_case({"case_id": case_id, "stage": "preauth", "trigger": "docs_updated"})

    with psycopg.connect(settings.database_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT status, missing_requirements FROM cases WHERE id = %s", (case_id,))
        status, missing = cur.fetchone()
        print(f"Result with doc -> Status: {status} | Missing: {missing}")
        assert status == "ready_for_dispatch"
        assert missing == []

        # Cleanup test case
        cur.execute("DELETE FROM cases WHERE id = %s", (case_id,))
        conn.commit()

    print("\n Cleaned up test case. Step 4 verified successfully!")

if __name__ == "__main__":
    asyncio.run(run_test())
