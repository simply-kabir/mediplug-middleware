"""
Spot-check real requirements data from Supabase packages table (Step 2).
"""

import json
import psycopg
from mediplug.config import settings
from mediplug.rules.engine import evaluate

def spot_check():
    with psycopg.connect(settings.database_url) as conn, conn.cursor() as cur:
        # Sample packages with preauth requirements
        cur.execute("""
            SELECT code, name, requirements
            FROM packages
            WHERE requirements ? 'preauth' 
              AND jsonb_array_length(requirements->'preauth') > 0
            LIMIT 5;
        """)
        preauth_rows = cur.fetchall()

        # Sample packages with claim requirements
        cur.execute("""
            SELECT code, name, requirements
            FROM packages
            WHERE requirements ? 'claim'
              AND jsonb_array_length(requirements->'claim') > 0
            LIMIT 3;
        """)
        claim_rows = cur.fetchall()

        # Sample packages with 0 requirements (or empty)
        cur.execute("""
            SELECT code, name, requirements
            FROM packages
            WHERE requirements = '{}'::jsonb 
               OR (requirements ? 'preauth' AND jsonb_array_length(requirements->'preauth') = 0)
            LIMIT 3;
        """)
        empty_rows = cur.fetchall()

    print(f"=== 1. Populated Preauth Packages ({len(preauth_rows)}) ===")
    for code, name, reqs in preauth_rows:
        pre_list = reqs.get("preauth", [])
        print(f"\nPackage: {code} | Name: {name}")
        print(f"  Requirements count: {len(pre_list)}")
        # Check first req shape
        first = pre_list[0]
        print(f"  Sample req keys: {list(first.keys())}")
        print(f"  Human label: {first.get('human_label')}")
        print(f"  Any_of count: {len(first.get('any_of', []))}")
        
        # Test evaluate with mock satisfying document
        first_code = first["any_of"][0]["code"] if isinstance(first["any_of"][0], dict) else first["any_of"][0]
        eval_result = evaluate(pre_list, [first_code])
        print(f"  Test evaluate() with [{first_code}]: passed={eval_result.passed}, missing={len(eval_result.missing_requirements)}")

    print(f"\n=== 2. Populated Claim Packages ({len(claim_rows)}) ===")
    for code, name, reqs in claim_rows:
        claim_list = reqs.get("claim", [])
        print(f"Package: {code} | Claim reqs: {len(claim_list)} | First label: {claim_list[0].get('human_label')}")

    print(f"\n=== 3. Zero-Requirement Packages ({len(empty_rows)}) ===")
    for code, name, reqs in empty_rows:
        pre_list = reqs.get("preauth", [])
        eval_result = evaluate(pre_list, [])
        print(f"Package: {code} | Name: {name} | Preauth reqs: {len(pre_list)} | evaluate() passed={eval_result.passed}")

if __name__ == "__main__":
    spot_check()
