import uuid
import httpx

GATEWAY_URL = "http://localhost:8000"

def create_case():
    hms_ref = f"ENC-TEST-{uuid.uuid4().hex[:6].upper()}"
    idempotency_key = str(uuid.uuid4())

    payload = {
        "hms_case_ref": hms_ref,
        "stage": "preauth",
        "patient": {
            "name": "Kavita Rao (Action Required Demo)",
            "gender": "female",
            "birth_date": "1992-07-15"
        },
        "encounter": {
            "admission_date": "2026-09-09",
            "attending_doctor": "Dr. V. Sharma",
            "doctor_registration_no": "MCI-55210",
            "hospital_id": "HOSP-MUM-01"
        },
        "clinical_notes": "lap appy for acute appendicitis",
        "documents": []
    }

    print(f"1. Ingesting case for Kavita Rao via {GATEWAY_URL}/api/v1/cases/ingest...")
    res = httpx.post(
        f"{GATEWAY_URL}/api/v1/cases/ingest",
        json=payload,
        headers={"Idempotency-Key": idempotency_key},
        timeout=10.0
    )
    if res.status_code != 202:
        print(f"Error ingesting case: {res.status_code} {res.text}")
        return

    data = res.json()
    case_id = data["case_id"]
    tracking_ref = data["tracking_ref"]
    print(f"   Success! case_id={case_id}, tracking_ref={tracking_ref}")

    # Give worker a moment to analyze
    import time
    time.sleep(2)

    # Confirm code S1A5.1 (Lap. Appendectomy) which requires USG or X-Ray
    print("2. Confirming code S1A5.1 (Lap. Appendectomy)...")
    res_confirm = httpx.post(
        f"{GATEWAY_URL}/api/v1/cases/{case_id}/confirm-code",
        json={"code": "S1A5.1", "confirmed_by": "test-script"},
        timeout=10.0
    )
    print(f"   Confirm response: {res_confirm.status_code}")

    time.sleep(2)
    print("\n" + "="*60)
    print(f"CASE READY FOR UPLOAD TEST!")
    print(f"Patient: Kavita Rao (Action Required Demo)")
    print(f"Tracking Ref: {tracking_ref}")
    print(f"Status: action_required (Missing USG or X-Ray)")
    print("="*60)
    print("Now go to http://localhost:3000, click on Kavita Rao, and test the file upload!")

if __name__ == "__main__":
    create_case()
