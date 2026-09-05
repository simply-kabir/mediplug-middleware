"""Track A (step 2) — fill `amount` on the package list from the cost PDF.

Input:
  data/interim/packages_raw.csv          <- from 01_extract_packages.py
  data/raw/mjpjay_package_costs.pdf      <- "MJPJAY PMJAY Package Costs" PDF

The cost PDF has one table per page with columns:
  Sr No | Catogery | Category Code | Disease Sub Category |
  Sub Category Code | Surgery | Surgery Code | 100% Package Amount

We only trust two of those cells per row — `Surgery Code` and the amount.
The name/sub-category cells are frequently mangled by column text-overlap
in this PDF, but code + amount come out clean.

Heads up: this PDF is a Studocu re-upload (watermark "lOMoARcPSD|...") and
looks like an OLDER package revision — it prices ~999 codes, while the
consolidated list has ~1670. So expect a chunk of rows to stay unpriced.
Get the official MJPJAY rate card / GR to close the gap.

Output:
  data/interim/packages.csv   (same columns as packages_raw, `amount` filled where known)
"""

import re
from pathlib import Path

import pandas as pd
import pdfplumber

RAW_CSV = Path("data/interim/packages_raw.csv")
COST_PDF = Path("data/raw/mjpjay_package_costs.pdf")
OUT = Path("data/interim/packages.csv")

CODE_RE = re.compile(r"^[A-Z]\d+[A-Z]?\d*(?:\.\d+)?$")   # S13N1.7 / S7F13.23 / M12T6.4


def load_costs(pdf_path: Path) -> dict[str, int]:
    """code -> amount, from every table row in the cost PDF."""
    costs: dict[str, int] = {}
    conflicts = 0
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables():
                for row in table:
                    if not row or len(row) < 8:
                        continue
                    code = (row[6] or "").strip()
                    amount = (row[7] or "").strip().replace(",", "").replace("/-", "")
                    if not CODE_RE.match(code) or not amount.isdigit():
                        continue
                    amount = int(amount)
                    if code in costs and costs[code] != amount:
                        conflicts += 1
                        continue  # keep first seen
                    costs.setdefault(code, amount)
    if conflicts:
        print(f"  note: {conflicts} rows had a conflicting amount for an already-seen code (kept first)")
    return costs


def main() -> None:
    pk = pd.read_csv(RAW_CSV, dtype=str).fillna("")
    costs = load_costs(COST_PDF)
    print(f"cost PDF: {len(costs)} unique code->amount pairs")

    pk["amount"] = pk["code"].map(costs).astype("Int64")

    priced = pk["amount"].notna().sum()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    pk.to_csv(OUT, index=False)

    print(f"wrote {len(pk)} rows -> {OUT}")
    print(f"  priced:   {priced}  ({priced * 100 // len(pk)}%)")
    print(f"  unpriced: {len(pk) - priced}")

    unpriced = pk[pk["amount"].isna()]
    if len(unpriced):
        top = unpriced["speciality"].value_counts().head(8)
        print("  unpriced by speciality (top 8):")
        for name, n in top.items():
            print(f"    {n:>4}  {name}")


if __name__ == "__main__":
    main()
