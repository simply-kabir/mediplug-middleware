"""
Medical abbreviation expansion — layer 0 of the mapping pipeline.

Runs before both the lexical prefilter and the semantic rerank in
mapper.py, so "lap chole" and "Laparoscopic Cholecystectomy" land in the
same normalized space before either scorer sees them.

Guide's cheapest accuracy win in the project: add an entry every time a
real clinical note misses a match because of an abbreviation. Keep it
alphabetized-by-key roughly, doesn't need to be exact.
"""

from __future__ import annotations

import re

ABBREVIATIONS: dict[str, str] = {
    "lap chole": "laparoscopic cholecystectomy",
    "lap appy": "laparoscopic appendicectomy",
    "cabg": "coronary artery bypass graft",
    "ptca": "percutaneous transluminal coronary angioplasty",
    "tka": "total knee arthroplasty",
    "thr": "total hip replacement",
    "turp": "transurethral resection of prostate",
    "lscs": "lower segment caesarean section",
    "orif": "open reduction internal fixation",
    "fess": "functional endoscopic sinus surgery",
    "egd": "esophagogastroduodenoscopy",
    "ercp": "endoscopic retrograde cholangiopancreatography",
    "d&c": "dilatation and curettage",
    "tah": "total abdominal hysterectomy",
    "bph": "benign prostatic hyperplasia",
    "ca": "carcinoma",
    "#": "fracture",
    "s/o": "suggestive of",
    "k/c/o": "known case of",
    "c/o": "complains of",
    "h/o": "history of",
    "ruq": "right upper quadrant",
    "post op": "postoperative",
    "pre op": "preoperative",
    # --- MJPJAY-specific additions, found against real package names /
    #     eval fixture. Add to this block as more misses turn up. ---
    "dm": "diabetes mellitus",
    "htn": "hypertension",
    "cad": "coronary artery disease",
    "mi": "myocardial infarction",
    "ckd": "chronic kidney disease",
    "copd": "chronic obstructive pulmonary disease",
    "uti": "urinary tract infection",
    "rta": "road traffic accident",
    "ivf": "in vitro fertilization",
    "e/o": "evidence of",
    "b/l": "bilateral",
    "?": " ",  # bare question marks are noise, not content
}

# Keys that are pure alphanumeric words ("ca", "tka", "dm", ...) get a
# \b word-boundary anchor so they only match as their own token — without
# this, "ca" matches inside "canal", "cad" matches inside "cadaver", etc.
# (Same failure mode already hit once in Track A's alias matching — see
# sih/decisions.md, "word-boundary alias matching over pure substring
# matching".)
#
# Keys built from symbols (#, s/o, d&c, k/c/o, c/o, h/o, e/o, b/l, ?) can't
# take a \b anchor — \b only fires at a word/non-word transition, and
# these keys start or end on a non-word character already. They're left
# as plain (escaped) literals; being short, punctuation-shaped strings,
# they don't collide with ordinary words the way "ca" or "dm" would.
_WORD_KEY = re.compile(r"^[A-Za-z0-9]+( [A-Za-z0-9]+)*$")


def _pattern_for(key: str) -> str:
    escaped = re.escape(key)
    return rf"\b{escaped}\b" if _WORD_KEY.match(key) else escaped


_PATTERN = re.compile(
    "|".join(
        _pattern_for(k) for k in sorted(ABBREVIATIONS, key=len, reverse=True)
    ),
    re.IGNORECASE,
)


def expand(text: str) -> str:
    """Expand every recognized abbreviation in `text` to its full form.
    Case-insensitive match, case of the replacement is whatever's in the
    dict (lowercase) — mapper.py already lowercases its query separately."""
    return _PATTERN.sub(lambda m: ABBREVIATIONS[m.group(0).lower()], text)
