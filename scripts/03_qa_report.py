"""
QA gate for the `packages` table in Supabase.

Run this AFTER Track A + Track B's work has been loaded via
scripts/03_load_packages.py. Checks: total count, duplicate codes, missing
fields, zero amounts, and how much of the document taxonomy is still
unmapped.

Gate to pass before moving to Phase 2/3: >=1300 packages, <5% unmapped
document references, and 5 random rows that actually read correctly.

STANDALONE TEST MODE (no DB needed):
  python scripts/03_qa_report.py --demo
Runs the exact same checks against a small hand-written fake dataset,
so you can build and trust this script without touching Supabase.

REAL MODE (queries the live `packages` table):
  python scripts/03_qa_report.py
"""

import json
import random
import sys
from collections import Counter
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from mediplug.config import settings  # noqa: E402

# ---------------------------------------------------------------------------
# Fake sample data — same shape as a real DB row, just 6 rows.
# Deliberately includes a duplicate code, a missing name, and an unmapped
# document reference, so you can see the QA report actually catch them.
# ---------------------------------------------------------------------------

DEMO_PACKAGES = [
    {
        "code": "S7F24.9", "speciality": "Cardiac & CVTS", "sub_speciality": "Vascular Injuries",
        "name": "Abdominal Vascular Injuries Repair", "amount": 100000, "icd_code": "S098xxA",
        "requirements": {
            "preauth": [{"requirement_id": "req_0", "stage": "preauth", "any_of": [
                {"code": "angiogram", "label": "Angiogram"}, {"code": "doppler", "label": "Doppler"},
                {"code": "ct_angiogram", "label": "CT Angiogram"}], "human_label": "Angiogram or Doppler or CT Angiogram"}],
            "claim": [{"requirement_id": "req_0", "stage": "claim", "any_of": [
                {"code": "doppler", "label": "Doppler"}, {"code": "scar_photo", "label": "Scar Photo"}],
                "human_label": "Doppler or Scar Photo"}],
        },
        "search_text": "Abdominal Vascular Injuries Repair Cardiac & CVTS Vascular Injuries",
    },
    {
        "code": "S7PM1.17", "speciality": "General Surgery", "sub_speciality": "Hepatobiliary",
        "name": "Laparoscopic Cholecystectomy", "amount": 35000, "icd_code": "K80.20",
        "requirements": {
            "preauth": [{"requirement_id": "req_0", "stage": "preauth", "any_of": [
                {"code": "usg_abdomen", "label": "USG Abdomen"}, {"code": "ct_abdomen", "label": "CT Abdomen"}],
                "human_label": "USG Abdomen or CT Abdomen"}],
            "claim": [],
        },
        "search_text": "Laparoscopic Cholecystectomy General Surgery Hepatobiliary",
    },
    {
        # Duplicate code on purpose — QA report should catch this
        "code": "S7PM1.17", "speciality": "General Surgery", "sub_speciality": "Hepatobiliary",
        "name": "Duplicate Row For Testing", "amount": 35000, "icd_code": "K80.20",
        "requirements": {"preauth": [], "claim": []},
        "search_text": "Duplicate Row For Testing",
    },
    {
        # Missing name on purpose
        "code": "S7X99.1", "speciality": "Orthopaedics", "sub_speciality": "Trauma",
        "name": "", "amount": 0, "icd_code": "",
        "requirements": {
            "preauth": [{"requirement_id": "req_0", "stage": "preauth", "any_of": [
                {"code": "unmapped__weird_xray_variant", "label": "Weird Xray Variant", "unmapped": True}],
                "human_label": "Weird Xray Variant"}],
            "claim": [],
        },
        "search_text": "",
    },
    {
        "code": "S7C12.4", "speciality": "Cardiac & CVTS", "sub_speciality": "Coronary",
        "name": "Coronary Angioplasty", "amount": 120000, "icd_code": "I25.10",
        "requirements": {
            "preauth": [{"requirement_id": "req_0", "stage": "preauth", "any_of": [
                {"code": "ecg", "label": "ECG"}], "human_label": "ECG"}],
            "claim": [{"requirement_id": "req_0", "stage": "claim", "any_of": [
                {"code": "discharge_summary", "label": "Discharge Summary"}], "human_label": "Discharge Summary"}],
        },
        "search_text": "Coronary Angioplasty Cardiac & CVTS Coronary",
    },
    {
        "code": "S7N05.2", "speciality": "Neurology", "sub_speciality": "Stroke",
        "name": "Thrombolysis for Acute Stroke", "amount": 40000, "icd_code": "I63.9",
        "requirements": {
            "preauth": [{"requirement_id": "req_0", "stage": "preauth", "any_of": [
                {"code": "ct_scan", "label": "CT Scan"}, {"code": "mri", "label": "MRI"}],
                "human_label": "CT Scan or MRI"}],
            "claim": [],
        },
        "search_text": "Thrombolysis for Acute Stroke Neurology Stroke",
    },
]


def fetch_from_db() -> list[dict]:
    """Pull all packages straight from Supabase, in the same dict shape
    the checks below expect (mirrors the row shape 03_load_packages.py
    writes)."""
    with psycopg.connect(settings.database_url, prepare_threshold=None) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                select code, name, speciality, sub_speciality, amount,
                       icd_code, requirements
                from packages
            """)
            cols = [d.name for d in cur.description]
            rows = cur.fetchall()

    pkgs = []
    for row in rows:
        p = dict(zip(cols, row))
        # normalize NULLs to the same "empty" shape the demo data uses,
        # so the checks below don't need separate None-handling
        p["name"] = p["name"] or ""
        p["speciality"] = p["speciality"] or ""
        p["sub_speciality"] = p["sub_speciality"] or ""
        p["icd_code"] = p["icd_code"] or ""
        p["amount"] = p["amount"] if p["amount"] is not None else 0
        p["requirements"] = p["requirements"] or {"preauth": [], "claim": []}
        pkgs.append(p)
    return pkgs


def run_report(pkgs: list[dict]) -> None:
    print(f"Total packages: {len(pkgs)}  (gate: >=1300)")
    print(f"Duplicate codes: {len(pkgs) - len({p['code'] for p in pkgs})}")
    print(f"Missing name:    {sum(1 for p in pkgs if not p['name'])}")
    print(f"Zero amount:     {sum(1 for p in pkgs if p['amount'] == 0)}")
    print(f"Missing ICD:     {sum(1 for p in pkgs if not p['icd_code'])}")
    print(f"No preauth reqs: {sum(1 for p in pkgs if not p['requirements']['preauth'])}")

    unmapped = sum(
        1 for p in pkgs for stage in p["requirements"].values()
        for r in stage for o in r["any_of"] if o.get("unmapped")
    )
    total_doc_refs = sum(
        1 for p in pkgs for stage in p["requirements"].values()
        for r in stage for o in r["any_of"]
    )
    pct = (unmapped / total_doc_refs * 100) if total_doc_refs else 0
    print(f"Unmapped doc refs: {unmapped} / {total_doc_refs} ({pct:.1f}%)  (gate: <5%)")

    print("\nBy speciality:")
    for spec, n in Counter(p["speciality"] for p in pkgs).most_common(15):
        print(f"  {n:>5}  {spec}")

    print("\n--- Random rows, read them properly ---")
    sample_size = min(5, len(pkgs))
    for p in random.sample(pkgs, sample_size):
        print(json.dumps(p, indent=2, ensure_ascii=False, default=str)[:700], "\n")


def main() -> None:
    if "--demo" in sys.argv:
        print("=== Standalone test — using fake sample data, not the real DB ===\n")
        run_report(DEMO_PACKAGES)
        return

    print("=== Querying live `packages` table from Supabase ===\n")
    pkgs = fetch_from_db()
    if not pkgs:
        sys.exit(
            "packages table is empty — run scripts/03_load_packages.py first.\n"
            "Or run with --demo to test this script standalone:\n"
            "  python scripts/03_qa_report.py --demo"
        )
    run_report(pkgs)


if __name__ == "__main__":
    main()