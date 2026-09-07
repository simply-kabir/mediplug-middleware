"""
Step 6: Standalone test of the POST /api/v1/cases/{case_id}/documents endpoint.
Verifies:
  1. case_documents row landed in Postgres
  2. case_events audit row logged with actor
  3. JobEnvelope with trigger='docs_updated' landed on the Redis stream
"""

import asyncio
import secrets
import httpx
import psycopg
from psycopg.types.json import Json
from redis.asyncio import Redis

from mediplug.config import settings
from mediplug.gateway.main import app, lifespan

async def run_step6_test():
    test_ref = f"TEST-STEP6-{secrets.token_hex(4).upper()}"
    idemp = f"idemp-s6-{secrets.token_hex(4)}"

    # 1. Create a dummy case in Postgres
    with psycopg.connect(settings.database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO cases (
                tracking_ref, idempotency_key, stage, status,
                patient, encounter, raw_clinical_notes,
                mapped_package_code, mapped_package_name
            )
            VALUES (%s, %s, 'preauth', 'action_required', %s, %s, %s, 'S10I11.2', 'Micro Vascular Decompression')
            RETURNING id;
            """,
            (
                test_ref, idemp,
                Json({"name": "Test Step 6"}),
                Json({"hospital_id": "HOSP-01"}),
                "notes for step 6 test"
            )
        )
        case_id = str(cur.fetchone()[0])
        conn.commit()

    print(f"Created initial case {case_id} (status: action_required)")

    # 2. Hit the endpoint within app lifespan
    payload = {
        "documents": [
            {
                "document_type": "ct_scan",
                "file_url": "https://storage.hospital.org/claims/ct_scan_step6.pdf"
            }
        ],
        "uploaded_by": "aarogyamitra_nurse_1"
    }

    async with lifespan(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            # Call the endpoint
            response = await client.post(f"/api/v1/cases/{case_id}/documents", json=payload)
            print(f"POST /api/v1/cases/{case_id}/documents -> Status: {response.status_code}")
            assert response.status_code == 200, f"Failed: {response.text}"
            data = response.json()
            print(f"Response data: {data}")
            assert data["documents_added"] == 1
            assert data["status"] == "queued"
            job_id = data["job_id"]
            assert job_id is not None

    # 3. Verify in Postgres: case_documents row landed
    with psycopg.connect(settings.database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT document_type, file_url FROM case_documents WHERE case_id = %s",
            (case_id,)
        )
        docs = cur.fetchall()
        print(f"\nPostgres case_documents rows: {docs}")
        assert len(docs) == 1
        assert docs[0][0] == "ct_scan"

        # Verify case status updated to queued
        cur.execute("SELECT status FROM cases WHERE id = %s", (case_id,))
        case_status = cur.fetchone()[0]
        assert case_status == "queued"

        # Verify audit event
        cur.execute(
            "SELECT from_status, to_status, actor, detail FROM case_events WHERE case_id = %s ORDER BY id DESC LIMIT 1",
            (case_id,)
        )
        ev = cur.fetchone()
        print(f"Postgres case_events row: from={ev[0]} to={ev[1]} actor={ev[2]}")
        assert ev[1] == "queued"
        assert ev[2] == "aarogyamitra_nurse_1"

    # 4. Verify in Redis: Job actually landed on mediplug:cases with trigger='docs_updated'
    r = Redis.from_url(settings.redis_url)
    try:
        messages = await r.xrevrange(settings.stream_key, count=10)
        found_job = False
        import json
        for msg_id, raw in messages:
            payload_data = json.loads(raw[b"payload"].decode("utf-8"))
            if payload_data.get("case_id") == case_id:
                print(f"\nFound enqueued job on Redis stream {settings.stream_key}!")
                print(f"  Msg ID:  {msg_id.decode('utf-8')}")
                print(f"  Trigger: {payload_data.get('trigger')}")
                print(f"  Stage:   {payload_data.get('stage')}")
                assert payload_data.get("trigger") == "docs_updated"
                found_job = True
                break
        assert found_job, f"Job for case {case_id} was not found on stream {settings.stream_key}"
    finally:
        await r.aclose()

    # Clean up test case in DB
    with psycopg.connect(settings.database_url) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM cases WHERE id = %s", (case_id,))
        conn.commit()

    print("\n Step 6 Standalone Endpoint Test: 100% PASSED!")

if __name__ == "__main__":
    asyncio.run(run_step6_test())
