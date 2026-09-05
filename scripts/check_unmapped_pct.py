"""
Compute the real unmapped-document percentage directly from the live
`packages` table in Supabase — this is the actual number the Phase 1 QA
gate (guide's <5% target) cares about, not the raw unmapped count printed
by 03_load_packages.py.

Run from the project root:
  python scripts/check_unmapped_pct.py
"""
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from mediplug.config import settings  # noqa: E402


def main() -> None:
    with psycopg.connect(settings.database_url, prepare_threshold=None) as conn:
        with conn.cursor() as cur:
            cur.execute("select requirements from packages")
            rows = cur.fetchall()

    total_refs = 0
    unmapped_refs = 0
    packages_with_any_unmapped = 0

    for (requirements,) in rows:
        pkg_has_unmapped = False
        for stage in ("preauth", "claim"):
            for req in requirements.get(stage, []):
                for opt in req.get("any_of", []):
                    total_refs += 1
                    if opt.get("unmapped"):
                        unmapped_refs += 1
                        pkg_has_unmapped = True
        if pkg_has_unmapped:
            packages_with_any_unmapped += 1

    pct = (unmapped_refs / total_refs * 100) if total_refs else 0
    print(f"Total packages:              {len(rows)}")
    print(f"Total document references:   {total_refs}")
    print(f"Unmapped document refs:      {unmapped_refs}")
    print(f"Unmapped %:                  {pct:.2f}%   (gate: <5%)")
    print(f"Packages with >=1 unmapped:  {packages_with_any_unmapped} "
          f"({packages_with_any_unmapped / len(rows) * 100:.1f}% of all packages)")
    print()
    if pct < 5:
        print("PASSES the <5% gate — safe to move on.")
    else:
        print("Still above the 5% gate. Your call whether to keep iterating "
              "the taxonomy or accept it and move on — remaining refs are "
              "likely genuine long-tail typos/garbled source text at this point.")


if __name__ == "__main__":
    main()