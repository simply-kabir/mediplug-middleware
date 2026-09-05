"""Track A — MJPJAY package extraction.

Source: the "Surgery/Therapy List (Consolidated)" export from the MJPJAY
portal ("Generate Excel" button) -> data/raw/FrontServlet.xls

That file is a real BIFF .xls with a single sheet "Documents" and these
columns (hierarchy is ALREADY forward-filled on every row, so no merging
of parent rows is needed):

    Category Code | Category Name | Sub Category Code | Sub Category Name |
    Surgery  Code | Surgery  Name | PRE INVESTIGATION | POST  INVESTIGATION |
    MID  INVESTIGATION | PCS CODE | ICD CODE

What this list does NOT have: package `amount` (₹ rate) and `status`.
Those come from the priced Health Benefit Package master — join on `code`
in a later step. Here `amount` is left blank and `status` defaults to
'active'.

Output: data/interim/packages_raw.csv with columns
    [code, speciality, sub_speciality, name, amount,
     preauth_docs, claim_docs, status, icd_code]
"""

from pathlib import Path

import pandas as pd

SRC = Path("data/raw/FrontServlet.xls")
OUT = Path("data/interim/packages_raw.csv")

OUT_COLUMNS = [
    "code", "speciality", "sub_speciality", "name", "amount",
    "preauth_docs", "claim_docs", "status", "icd_code",
]

# portal header -> our column
RENAME = {
    "Category Name": "speciality",
    "Sub Category Name": "sub_speciality",
    "Surgery  Code": "code",          # note: two spaces in the portal header
    "Surgery  Name": "name",
    "PRE INVESTIGATION": "preauth_docs",
    "POST  INVESTIGATION": "claim_docs",
    "MID  INVESTIGATION": "mid_docs",
    "ICD CODE": "icd_code",
}


def main() -> None:
    df = pd.read_excel(SRC, sheet_name="Documents", header=0, dtype=str)
    df.columns = [c.strip() for c in df.columns]
    df = df.rename(columns={k.strip(): v for k, v in RENAME.items()})
    df = df.fillna("")

    for col in ("code", "speciality", "sub_speciality", "name",
                "preauth_docs", "claim_docs", "mid_docs", "icd_code"):
        df[col] = df[col].astype(str).str.strip()

    before = len(df)

    # 1. drop the trailing all-blank row(s) and any row without a code
    df = df[df["code"] != ""].copy()

    # 2. a "-" in an investigation column means "nothing required" -> blank
    for col in ("preauth_docs", "claim_docs", "mid_docs", "icd_code"):
        df.loc[df[col] == "-", col] = ""

    # 3. the source has a handful of exact-duplicate code rows (data-entry
    #    slips in the portal, e.g. S7F13.3 listed under both "Cvts" and
    #    "Vasular"). Keep the first, note how many we dropped.
    dup_mask = df["code"].duplicated(keep="first")
    n_dupes = int(dup_mask.sum())
    df = df[~dup_mask].copy()

    # 4. fold MID INVESTIGATION into claim_docs so we don't lose it
    both = df["mid_docs"] != ""
    df.loc[both, "claim_docs"] = (
        df.loc[both, "claim_docs"].str.cat(df.loc[both, "mid_docs"], sep="; ")
        .str.strip("; ")
    )

    # 5. columns this list can't provide
    df["amount"] = ""              # fill later from the priced package master
    df["status"] = "active"

    out = df[OUT_COLUMNS].copy()

    OUT.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(OUT, index=False)

    print(f"read {before} rows from {SRC.name}")
    print(f"dropped {n_dupes} duplicate-code rows")
    print(f"wrote {len(out)} package rows -> {OUT}")
    print(f"  specialities:        {out['speciality'].nunique()}")
    print(f"  sub-specialities:    {out['sub_speciality'].nunique()}")
    print(f"  rows with icd_code:  {(out['icd_code'] != '').sum()}")
    print(f"  rows with amount:    {(out['amount'] != '').sum()}  <-- needs the rate master")


if __name__ == "__main__":
    main()
