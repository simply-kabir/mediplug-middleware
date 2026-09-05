"""Track A (step 3) — load data/interim/packages.csv into the `packages` table.

Reads DATABASE_URL from .env (same Settings the gateway/worker use). Point it
at your Supabase Postgres connection string; that role bypasses RLS, so this
loads fine even with rls.sql applied.

CSV columns  : code, speciality, sub_speciality, name, amount,
               preauth_docs, claim_docs, status, icd_code
Table columns: code, name, speciality, sub_speciality, amount, icd_code,
               requirements (jsonb), search_text
Mapping:
  - code/name/speciality/sub_speciality/icd_code : straight across
  - amount            : int, or NULL when unknown
  - preauth_docs + claim_docs -> parsed through Track B's parser into the
    structured Requirement shape from schemas.py (any_of / human_label),
    NOT the raw text — see parse_requirement_string() import below.
  - search_text       : name + speciality + sub_speciality (feeds the FTS index)
  - status            : dropped (no such column; portal list is all-active)

Idempotent: INSERT ... ON CONFLICT (code) DO UPDATE, so re-running just
refreshes rows (e.g. after you get better rates and re-run 02_merge_costs.py,
or after the taxonomy in 02_normalize_requirements.py gets extended).
"""
import importlib.util
import sys
from pathlib import Path

import pandas as pd
import psycopg
from psycopg.types.json import Json

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from mediplug.config import settings  # noqa: E402

# --- load Track B's parser (filename starts with a digit, so plain
# `import` won't work — load it by file path instead) -----------------------
_normalize_path = Path(__file__).resolve().parent / "02_normalize_requirements.py"
_spec = importlib.util.spec_from_file_location("normalize_requirements", _normalize_path)
normalize_requirements = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(normalize_requirements)
parse_requirement_string = normalize_requirements.parse_requirement_string

CSV = Path("data/interim/packages.csv")

UPSERT = """
insert into packages
    (code, name, speciality, sub_speciality, amount, icd_code, requirements, search_text)
values
    (%(code)s, %(name)s, %(speciality)s, %(sub_speciality)s, %(amount)s,
     %(icd_code)s, %(requirements)s, %(search_text)s)
on conflict (code) do update set
    name           = excluded.name,
    speciality     = excluded.speciality,
    sub_speciality = excluded.sub_speciality,
    amount         = excluded.amount,
    icd_code       = excluded.icd_code,
    requirements   = excluded.requirements,
    search_text    = excluded.search_text
"""


def to_int(x):
    x = (x or "").strip()
    return int(x) if x.isdigit() else None


def build_rows(df: pd.DataFrame) -> list[dict]:
    rows = []
    unmapped_seen: dict[str, int] = {}

    for r in df.to_dict("records"):
        code = (r["code"] or "").strip()
        name = (r["name"] or "").strip()
        if not code or not name:
            continue  # code is PK, name is NOT NULL
        spec = (r["speciality"] or "").strip()
        sub = (r["sub_speciality"] or "").strip()

        preauth_reqs = parse_requirement_string(r.get("preauth_docs") or "", "preauth")
        claim_reqs = parse_requirement_string(r.get("claim_docs") or "", "claim")

        for req_list in (preauth_reqs, claim_reqs):
            for req in req_list:
                for opt in req["any_of"]:
                    if opt.get("unmapped"):
                        unmapped_seen[opt["label"]] = unmapped_seen.get(opt["label"], 0) + 1

        rows.append({
            "code": code,
            "name": name,
            "speciality": spec or None,
            "sub_speciality": sub or None,
            "amount": to_int(r["amount"]),
            "icd_code": (r["icd_code"] or "").strip() or None,
            "requirements": Json({"preauth": preauth_reqs, "claim": claim_reqs}),
            "search_text": " ".join(p for p in (name, spec, sub) if p),
        })

    if unmapped_seen:
        print(f"\n{sum(unmapped_seen.values())} unmapped document refs across "
              f"{len(unmapped_seen)} distinct strings. Top 20 — add to TAXONOMY "
              f"in 02_normalize_requirements.py:")
        for label, n in sorted(unmapped_seen.items(), key=lambda x: -x[1])[:20]:
            print(f"  {n:>4}  {label}")
        print()

    return rows


def main() -> None:
    df = pd.read_csv(CSV, dtype=str).fillna("")
    rows = build_rows(df)
    print(f"{len(rows)} rows to upsert (from {len(df)} csv rows)")

    dsn = settings.database_url
    where = dsn.split("@")[-1] if "@" in dsn else dsn
    print(f"connecting to {where}")

    # prepare_threshold=None keeps this working through a Supabase pooler
    # (pgbouncer transaction mode) as well as a direct connection.
    with psycopg.connect(dsn, prepare_threshold=None) as conn:
        with conn.cursor() as cur:
            cur.executemany(UPSERT, rows)
            cur.execute("select count(*), count(amount) from packages")
            total, priced = cur.fetchone()
        conn.commit()
    print(f"done. packages table now has {total} rows ({priced} with amount)")


if __name__ == "__main__":
    main()