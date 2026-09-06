"""
HMS Sync — polling script.

Reads encounters from the teammate's HMS Supabase, joins patient/clinical/
doctor/hospital/document data, maps each to IngestRequest, and POSTs to
our gateway. Uses our own cases.hms_case_ref as the "already synced" list
instead of timestamps (his tables have no created_at on encounters).

Run:  python -m mediplug.hms_sync.poll
"""

from __future__ import annotations

import asyncio
import logging
import uuid

import httpx
from supabase import create_client

from ..config import settings
from .mapper import map_hms_to_ingest_request

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

GATEWAY_URL = f"http://localhost:8000/api/v1/cases/ingest"


def _get_hms_client():
    """Create a Supabase client pointing at the teammate's project."""
    return create_client(settings.hms_supabase_url, settings.hms_supabase_key)


def _idempotency_key(encounter_id: str) -> str:
    """Deterministic per encounter — reruns never double-ingest."""
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"hms-encounter-{encounter_id}"))


def _get_already_synced() -> set[str]:
    """Query OUR cases table for hms_case_ref values already ingested."""
    import psycopg
    with psycopg.connect(settings.database_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT hms_case_ref FROM cases WHERE hms_case_ref IS NOT NULL")
        return {row[0] for row in cur.fetchall()}


async def sync_once() -> int:
    """One sync pass. Returns count of newly synced encounters."""
    hms = _get_hms_client()

    # --- 1. Get all encounters from HMS ---
    encounters_resp = hms.table("encounters").select("*").execute()
    all_encounters = encounters_resp.data
    log.info("HMS has %d total encounters", len(all_encounters))

    # --- 2. Filter out already-synced ones ---
    already_synced = _get_already_synced()
    new_encounters = [
        e for e in all_encounters
        if e["encounter_id"] not in already_synced
    ]
    log.info("%d new encounters to sync", len(new_encounters))

    if not new_encounters:
        return 0

    # --- 3. Batch-fetch related data from HMS ---
    # Get all patients, clinical_notes, doctors, hospitals, diagnostic_reports, documents
    # For a 50-row demo dataset this is fine. For production, filter by IDs.
    patients_resp = hms.table("patients").select("*").execute()
    patients_map = {p["patient_id"]: p for p in patients_resp.data}

    notes_resp = hms.table("clinical_notes").select("*").execute()
    notes_map: dict[str, dict] = {}
    for n in notes_resp.data:
        notes_map[n["encounter_id"]] = n  # 1:1 with encounter

    doctors_resp = hms.table("doctors").select("*").execute()
    doctors_map = {d["doctor_id"]: d for d in doctors_resp.data}

    hospitals_resp = hms.table("hospitals").select("*").execute()
    hospitals_map = {h["hospital_id"]: h for h in hospitals_resp.data}

    diag_reports_resp = hms.table("diagnostic_reports").select("*").execute()
    diag_by_encounter: dict[str, list[dict]] = {}
    for dr in diag_reports_resp.data:
        diag_by_encounter.setdefault(dr["encounter_id"], []).append(dr)

    docs_resp = hms.table("documents").select("*").execute()
    docs_by_encounter: dict[str, list[dict]] = {}
    for d in docs_resp.data:
        eid = d.get("encounter_id")
        if eid:
            docs_by_encounter.setdefault(eid, []).append(d)

    # --- 4. Map and POST each new encounter ---
    synced = 0
    async with httpx.AsyncClient(timeout=15) as client:
        for enc in new_encounters:
            enc_id = enc["encounter_id"]
            patient = patients_map.get(enc.get("patient_id"))
            if not patient:
                log.warning("skipping %s: patient_id %s not found", enc_id, enc.get("patient_id"))
                continue

            clinical_note = notes_map.get(enc_id)
            doctor = doctors_map.get(enc.get("doctor_id"))
            hospital = hospitals_map.get(enc.get("hospital_id"))
            diag_reports = diag_by_encounter.get(enc_id, [])
            encounter_docs = docs_by_encounter.get(enc_id, [])

            # Map to our contract
            ingest_req = map_hms_to_ingest_request(
                encounter=enc,
                patient=patient,
                clinical_note=clinical_note,
                doctor=doctor,
                hospital=hospital,
                diagnostic_reports=diag_reports,
                documents=encounter_docs,
            )

            if ingest_req is None:
                continue  # mapper logged the reason

            # POST to our gateway
            try:
                r = await client.post(
                    GATEWAY_URL,
                    json=ingest_req.model_dump(mode="json"),
                    headers={"Idempotency-Key": _idempotency_key(enc_id)},
                )
                if r.status_code == 202:
                    data = r.json()
                    log.info(
                        "synced %s -> case_id=%s tracking_ref=%s",
                        enc_id, data["case_id"], data["tracking_ref"],
                    )
                    synced += 1
                else:
                    log.error("gateway returned %d for %s: %s", r.status_code, enc_id, r.text[:300])
            except Exception as exc:
                log.error("failed to POST %s: %s", enc_id, exc)

    return synced


async def run_forever(poll_interval: int = 10):
    """Poll continuously. For demo, set poll_interval short (5-10s)."""
    log.info("HMS sync started, polling every %ds", poll_interval)
    while True:
        try:
            count = await sync_once()
            if count:
                log.info("synced %d new encounters this pass", count)
        except Exception as exc:
            log.error("sync pass failed: %s", exc)
        await asyncio.sleep(poll_interval)


if __name__ == "__main__":
    asyncio.run(run_forever())
