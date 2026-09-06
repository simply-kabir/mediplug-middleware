"""
Accuracy harness for the Phase 5 mapper — Part A's actual deliverable,
not just "map_notes() exists".

Fixture: 28 hand-written clinical-note snippets, each paired with the
package code it should resolve to. Every code below was pulled straight
from the live `packages` table (not invented) — see the query session
that built this list for the exact lookups. Several notes deliberately
use abbreviations/shorthand ("lap chole", "k/c/o", "#", "TURP", "RTA",
"CAD", "TAH", "BSO", "CBD", "GI") to exercise abbreviations.py, not just
the embedding layer.

Extend this fixture over time — every real mapping miss the team hits
during testing is worth adding here as a regression case.

Usage:
    uv run python scripts/07_mapping_eval.py

Prints top-1 / top-3 accuracy and a confidence-vs-threshold breakdown,
then a plain recommendation on whether config.py's
confidence_auto_accept (0.82) / confidence_floor (0.45) look right
against this fixture.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mediplug.config import settings  # noqa: E402
from mediplug.mapping.mapper import map_notes  # noqa: E402

# (clinical note text, expected package code, expected name — for display only)
FIXTURE: list[tuple[str, str, str, tuple[str, ...]]] = [
    # DUPLICATE PACKAGE FLAG: S8G5.11 "Laparoscopic Choleycystectomy" (sic,
    # misspelled in the source data) is a near-identical duplicate of
    # S1A11.2. Text alone cannot tell them apart — both accepted.
    ("lap chole for chronic cholelithiasis with gallstones",
     "S1A11.2", "Lap.Cholecystectomy", ("S8G5.11",)),
    ("lap appy for acute appendicitis",
     "S1A5.1", "Lap. Appendectomy", ()),
    ("k/c/o appendicular perforation with peritonitis",
     "S1A5.2", "AppendIcular Perforation", ()),
    ("right inguinal hernia repair",
     "S1A15.7", "Inguinal Hernia Repair", ()),
    ("hiatus hernia repair via abdominal approach",
     "S1A4.4", "Hiatus Hernia Repair Abdominal", ()),
    ("recurrent tonsillitis planned for tonsillectomy",
     "S2B8.1", "Tonsillectomy", ()),
    ("peritonsillar abscess for drainage",
     "S2B4.6", "Drainage of Peritonsillor abscess", ()),
    ("pediatric cataract phacoemulsification with IOL implantation",
     "S3B10.2", "Pediatric Cataract Surgery - Phacoemulsification - IOL", ()),
    ("k/c/o BPH posted for TURP",
     "S9H8.9", "Transurethral Resection Of Prostate (TURP) / Bladder neck incision", ()),
    ("varicose veins for radiofrequency ablation and ligation",
     "S1A15.21", "Varicose Veins - Radiofrequency Ablation / Excision and Ligation", ()),
    ("complete heart block for single chamber permanent pacemaker implantation",
     "M7F3.1", "Single chamber Permanent Pacemaker Implantation - 5 Days", ()),
    ("temporary pacemaker implantation for symptomatic bradycardia",
     "M7F3.2", "Temporary Pacemaker Implantation - 2 Days", ()),
    ("CAD triple vessel disease planned for on pump CABG",
     "S7F7.2", "On pump multivessel minimal - invasive coronary artery bypass grafting", ()),
    # DUPLICATE PACKAGE FLAG: S5D8.1 has the exact same name as S14O1.1 —
    # literally impossible to disambiguate by text. Both accepted.
    ("# femur planned for ORIF",
     "S14O1.1", "Open Reduction And Internal Fixation Of Long Bone Fractures",
     ("S5D8.1",)),
    ("RTA with depressed fracture of skull",
     "S10I13.4", "Depressed Fracture of Skull", ()),
    ("AVN femur head for excision arthroplasty",
     "S5D2.15", "Excision Arthoplasty of Femur head", ()),
    # DUPLICATE PACKAGE FLAG: S11J41.1 "Wertheim's Hysterectomy" (with
    # apostrophe) duplicates S11J39.3 "Wertheims Hysterectomy" (without).
    # Both accepted.
    ("Wertheims hysterectomy for cervical carcinoma",
     "S11J39.3", "Wertheims Hysterectomy", ("S11J41.1",)),
    # AMBIGUOUS: the corpus has both a plain "Abdominal Hysterectomy"
    # (S4C4.1) and this much more specific combo package. A generic "TAH
    # with BSO" note plausibly means either — both accepted.
    ("TAH with BSO for fibroid uterus",
     "S11J19.1",
     ("Total Abdominal Hysterectomy(TAH) + Bilateral Salpingo Ophorectomy (BSO)"
      " + Bilateral Pelvic Lymph Node Dissection (BPLND) + Omentectomy"),
     ("S4C4.1",)),
    ("ERCP for CBD stone",
     "M14V2.10", "ERCP For CBD /PD Stones - Day Care", ()),
    ("evacuation of brain abscess via burr hole",
     "S10I1.3", "Evacuation Of Brain Abscess - Burr Hole", ()),
    ("excision of brain abscess",
     "S10I2.11", "Excision Of Brain Abscess", ()),
    ("closed reduction and percutaneous screw fixation neck of femur",
     "S5D2.22", "Closed Reduction and Percutaneous Screw Fixation (neck Femur)", ()),
    ("achalasia cardia balloon dilatation",
     "M14V2.9", "Achalasia Cardia Balloon Dilatation - Day Care", ()),
    ("percutaneous balloon dilatation procedure in cardiology",
     "M7F5.1", "Percutaneous Balloon Dilatation procedures in Cardiology", ()),
    ("hemiarthroplasty bipolar for fracture neck of femur",
     "S5D7.8", "Hemiarthroplasty (Bipolar)", ()),
    ("skull base tumor excision",
     "S10I7.6", "Skull base tumors excision", ()),
    ("oesophageal perforation managed conservatively",
     "M14V1.2", "Oesophageal Perforation-  7 Days Stay", ()),
    ("endoscopic management of GI fistula perforation leak",
     "M14V2.3", "Endoscopic management of GI Fistule / Perforation / Leak 10 stays stay", ()),
]


def main() -> None:
    top1_hits = 0
    top3_hits = 0

    # For the threshold breakdown: how confident were we on the cases we
    # actually got right, vs. the cases we got wrong?
    correct_confidences: list[float] = []
    wrong_confidences: list[float] = []

    print(f"Running {len(FIXTURE)} fixture cases...\n")

    duplicate_flags = 0

    for notes, expected_code, expected_name, alt_codes in FIXTURE:
        accepted = {expected_code, *alt_codes}
        candidates = map_notes(notes, top_k=3)
        codes = [c.code for c in candidates]
        top1_ok = bool(codes) and codes[0] in accepted
        top3_ok = any(c in accepted for c in codes)

        top1_hits += top1_ok
        top3_hits += top3_ok

        top_conf = candidates[0].confidence if candidates else 0.0
        if top1_ok:
            correct_confidences.append(top_conf)
        else:
            wrong_confidences.append(top_conf)

        via_alt = top1_ok and codes[0] != expected_code
        if via_alt:
            duplicate_flags += 1

        mark = "OK* " if via_alt else ("OK  " if top1_ok else ("~3  " if top3_ok else "MISS"))
        got = f"{codes[0]} ({top_conf:.3f})" if candidates else "(no candidates)"
        print(f"[{mark}] expected {expected_code:<10} got {got:<28} | {notes}")
        if not top1_ok:
            print(f"       expected name: {expected_name}")
            if candidates:
                print(f"       candidates:    {[(c.code, c.confidence) for c in candidates]}")
        elif via_alt:
            print(f"       (matched via known-duplicate alt code, not {expected_code} itself)")

    n = len(FIXTURE)
    print(f"\nTop-1 accuracy: {top1_hits}/{n} ({100 * top1_hits / n:.1f}%)")
    print(f"Top-3 accuracy: {top3_hits}/{n} ({100 * top3_hits / n:.1f}%)")
    if duplicate_flags:
        print(f"({duplicate_flags} of those top-1 hits matched via a known-duplicate "
              "alt code, marked OK* above — see the DUPLICATE PACKAGE FLAG comments "
              "in FIXTURE. That's a Phase 1 data-quality issue, not a mapper bug: "
              "the packages table has multiple rows with identical/near-identical "
              "names under different codes, which text matching cannot disambiguate. "
              "Worth a spot-check / de-dup pass on packages before demo day.")

    auto_accept = settings.confidence_auto_accept
    floor = settings.confidence_floor

    correct_above_accept = sum(c >= auto_accept for c in correct_confidences)
    correct_in_band = sum(floor <= c < auto_accept for c in correct_confidences)
    correct_below_floor = sum(c < floor for c in correct_confidences)

    wrong_above_accept = sum(c >= auto_accept for c in wrong_confidences)
    wrong_in_band = sum(floor <= c < auto_accept for c in wrong_confidences)
    wrong_below_floor = sum(c < floor for c in wrong_confidences)

    print(f"\nThreshold check (auto_accept={auto_accept}, floor={floor}):")
    print("  Correct top-1 matches:")
    print(f"    >= auto_accept        : {correct_above_accept}  (auto-accepted correctly)")
    print(f"    floor..auto_accept    : {correct_in_band}  (correctly flagged for human confirm)")
    print(f"    < floor               : {correct_below_floor}  (WASTED — right answer, marked action_required)")
    print("  Wrong top-1 matches:")
    print(f"    >= auto_accept        : {wrong_above_accept}  (DANGEROUS — wrong code auto-accepted)")
    print(f"    floor..auto_accept    : {wrong_in_band}  (safe — human will see it's wrong)")
    print(f"    < floor               : {wrong_below_floor}  (safe — correctly rejected)")

    print("\nRecommendation:")
    if wrong_above_accept > 0:
        print(f"  {wrong_above_accept} wrong match(es) scored >= {auto_accept} and would be "
              "auto-accepted silently. Raise confidence_auto_accept, or fix the "
              "underlying mapping (check abbreviations.py first).")
    elif correct_below_floor > 0:
        print(f"  {correct_below_floor} correct match(es) scored below {floor} and would be "
              "needlessly rejected as action_required. Consider lowering "
              "confidence_floor, or improve the lexical/semantic blend.")
    else:
        print(f"  No dangerous auto-accepts and no wasted correct-but-rejected matches "
              f"on this fixture. {auto_accept}/{floor} look reasonable — "
              "keep expanding the fixture as real misses turn up.")


if __name__ == "__main__":
    main()
