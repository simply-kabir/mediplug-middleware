"""
Phase 7 — FHIR bundle builder eval against real data. Part A's actual
deliverable, not just "build_claim_bundle() exists".

Two passes, both against the live shared Supabase:

  1. MAPPED CASES — every `cases` row that has a `mapped_package_code`,
     joined to its `packages` row, with its real `case_documents`. Build a
     bundle for each, structurally validate it (R4B), tally pass/fail.

  2. PACKAGE COVERAGE SWEEP — a broad sample of real `packages` rows, each
     paired with a real patient/encounter blob lifted from an existing
     case (no documents). This is what makes the dispatch-readiness number
     meaningful: it exercises the builder across the 801-package ICD gap,
     not just the handful of packages that happen to have cases today.

For every built bundle we also compute **dispatch-readiness**: the Phase 8
mock payer rejects a Claim that is missing `diagnosis` (no ICD) or has an
empty `patient` / `provider` / `insurer` / `insurance` / `item`. The
report counts how many real bundles would bounce and why — that number
tells you whether the ICD data gap will bite the Phase 8 demo.

Outputs:
  * pass/fail validate tally for each pass, first failure path printed
  * dispatch-readiness breakdown by reject reason
  * one clean sample written to data/samples/fhir_bundle_<code>.json
    (tracked path — for slides / the "show the FHIR JSON" demo beat)
  * a one-line recommendation naming package codes that are safe demo
    picks (valid AND dispatch-ready)

Usage:
    uv run python scripts/09_fhir_sample.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import psycopg

from mediplug.config import settings
from mediplug.fhir import build_claim_bundle, validate

SWEEP_SIZE = 250
SAMPLES_DIR = Path(__file__).resolve().parents[1] / "data" / "samples"

# A Claim the mock payer (Phase 8) will accept needs all of these non-empty.
REQUIRED_CLAIM_KEYS = ("patient", "provider", "insurer", "insurance", "item", "diagnosis")


def _claim(bundle: dict) -> dict:
    for entry in bundle["entry"]:
        if entry["resource"]["resourceType"] == "Claim":
            return entry["resource"]
    raise AssertionError("bundle has no Claim")


def _reject_reasons(bundle: dict) -> list[str]:
    """Why the Phase 8 mock payer would reject this bundle, if at all."""
    claim = _claim(bundle)
    reasons = []
    for key in REQUIRED_CLAIM_KEYS:
        if not claim.get(key):
            reasons.append(f"missing {key}")
    return reasons


def _load_mapped_cases(cur) -> list[dict]:
    cur.execute(
        """
        SELECT c.id, c.stage, c.status, c.mapped_package_code,
               c.patient, c.encounter,
               p.code, p.name, p.amount, p.icd_code
        FROM cases c
        JOIN packages p ON p.code = c.mapped_package_code
        ORDER BY c.created_at DESC
        """
    )
    out = []
    rows = cur.fetchall()
    for row in rows:
        cid, stage, status, _code, patient, encounter, pcode, pname, pamount, picd = row
        with cur.connection.cursor() as dcur:
            dcur.execute(
                """SELECT document_type, file_url, file_name
                   FROM case_documents WHERE case_id = %s ORDER BY uploaded_at""",
                (cid,),
            )
            docs = [
                {"document_type": dt, "file_url": fu, "file_name": fn}
                for dt, fu, fn in dcur.fetchall()
            ]
        out.append(
            {
                "case_id": str(cid),
                "stage": stage,
                "status": status,
                "case": {"patient": patient, "encounter": encounter},
                "package": {
                    "code": pcode,
                    "name": pname,
                    "amount": pamount,
                    "icd_code": picd,
                },
                "documents": docs,
            }
        )
    return out


def _load_sweep(cur) -> tuple[dict, list[dict]]:
    """A template patient/encounter (from a real case) + a spread of real
    packages: ICD-present and ICD-absent, priced and unpriced."""
    cur.execute(
        "SELECT patient, encounter FROM cases WHERE patient IS NOT NULL "
        "ORDER BY created_at DESC LIMIT 1"
    )
    patient, encounter = cur.fetchone()
    template = {"patient": patient, "encounter": encounter}

    cur.execute(
        """
        SELECT code, name, amount, icd_code FROM packages
        ORDER BY (icd_code IS NULL), (amount IS NULL), code
        """
    )
    all_rows = cur.fetchall()
    # even stride across the ordering so we hit every quadrant
    step = max(1, len(all_rows) // SWEEP_SIZE)
    sample = all_rows[::step][:SWEEP_SIZE]
    packages = [
        {"code": c, "name": n, "amount": a, "icd_code": i} for c, n, a, i in sample
    ]
    return template, packages


def _run(label: str, items):
    """items: iterable of (tag, case, package, documents, stage). Returns
    (n, passed, first_failure, dispatch_ready_tags, reason_counter)."""
    n = passed = 0
    first_failure = None
    dispatch_ready: list[str] = []
    reason_counter: dict[str, int] = {}

    for tag, case, package, documents, stage in items:
        n += 1
        try:
            bundle = build_claim_bundle(case, package, documents, stage)
            validate(bundle)
            passed += 1
        except Exception as exc:  # noqa: BLE001 — eval script, want the message
            if first_failure is None:
                first_failure = f"{tag}: {type(exc).__name__}: {str(exc)[:300]}"
            continue

        reasons = _reject_reasons(bundle)
        if reasons:
            for r in reasons:
                reason_counter[r] = reason_counter.get(r, 0) + 1
        else:
            dispatch_ready.append(tag)

    print(f"\n{label}")
    print(f"  built + validated : {passed}/{n}")
    if first_failure:
        print(f"  first failure     : {first_failure}")
    print(f"  dispatch-ready     : {len(dispatch_ready)}/{passed}")
    rejectable = passed - len(dispatch_ready)
    print(f"  mock-payer-rejectable : {rejectable}/{passed}")
    for reason, count in sorted(reason_counter.items(), key=lambda kv: -kv[1]):
        print(f"      {reason:<22} {count}")
    return n, passed, first_failure, dispatch_ready, reason_counter


def main() -> None:
    # prepare_threshold=None: keeps this working through the Supabase
    # transaction-mode pooler (port 6543), same as scripts/03_*.
    with psycopg.connect(settings.database_url, prepare_threshold=None) as conn, \
            conn.cursor() as cur:
        mapped = _load_mapped_cases(cur)
        template, sweep_packages = _load_sweep(cur)

    print(f"Loaded {len(mapped)} mapped cases + {len(sweep_packages)} sweep packages "
          f"from {settings.database_url.split('@')[-1]}")

    mapped_items = [
        (m["package"]["code"], m["case"], m["package"], m["documents"], m["stage"])
        for m in mapped
    ]
    _, _, _, mapped_ready, _ = _run("PASS 1 — mapped cases (real docs):", mapped_items)

    sweep_items = [
        (p["code"], template, p, [], "preauth") for p in sweep_packages
    ]
    _run("PASS 2 — package coverage sweep (no docs):", sweep_items)

    # --- write one clean sample: prefer a dispatch-ready mapped case ---
    sample_source = next(
        (m for m in mapped if m["package"]["code"] in set(mapped_ready)),
        mapped[0] if mapped else None,
    )
    if sample_source:
        bundle = build_claim_bundle(
            sample_source["case"],
            sample_source["package"],
            sample_source["documents"],
            sample_source["stage"],
        )
        SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
        code = sample_source["package"]["code"]
        out_path = SAMPLES_DIR / f"fhir_bundle_{code}.json"
        out_path.write_text(json.dumps(bundle, indent=2))
        print(f"\nWrote sample bundle -> {out_path.relative_to(SAMPLES_DIR.parents[2])}"
              f"  ({len(bundle['entry'])} entries, "
              f"{'dispatch-ready' if not _reject_reasons(bundle) else 'REJECTABLE: ' + ', '.join(_reject_reasons(bundle))})")

    # --- recommendation ---
    print("\nRecommendation:")
    safe = sorted(set(mapped_ready))
    if safe:
        print(f"  Safe demo package codes (valid + dispatch-ready): {', '.join(safe)}")
    else:
        print("  No mapped case is currently dispatch-ready. Ingest a demo case whose "
              "package has a non-null icd_code before the Phase 8 demo — otherwise the "
              "mock payer will 422 on missing diagnosis.")
    if not mapped:
        print("  (No cases have a mapped_package_code yet — run some through Phase 5 first.)")


if __name__ == "__main__":
    main()
