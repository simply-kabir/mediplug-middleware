"""
Pillar 3: Anatomical Impossibility & Procedure Cooldown Registry.

1. Lifetime Single-Excision Registry:
   Anatomical organs that can never be excised twice in a human lifetime:
   - Gallbladder (S8G5.11, S1A11.2)
   - Appendix (S8G4.1, S1A5.1, S1A5.2)
   - Uterus / Total Hysterectomy (S4C4.1, S11J19.1, S11J39.3, S11J41.1)
   - Spleen (S8G12.1, S1A15.20)

2. Procedure Cooldown Windows:
   Mandatory minimum intervals between repeatable medical procedures:
   - Cataract surgery on same eye: >= 1095 days (3 years)
   - Coronary CABG / Angioplasty / Stents: >= 180 days (6 months)
   - Pacemaker Implantation: >= 180 days (6 months)
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

# Maps package code -> single-excision organ
SINGLE_EXCISION_ORGAN_BY_CODE: dict[str, str] = {
    # Gallbladder
    "S8G5.11": "Gallbladder",
    "S1A11.2": "Gallbladder",
    # Appendix
    "S8G4.1": "Appendix",
    "S1A5.1": "Appendix",
    "S1A5.2": "Appendix",
    # Uterus (Hysterectomy)
    "S4C4.1": "Uterus",
    "S11J19.1": "Uterus",
    "S11J39.3": "Uterus",
    "S11J41.1": "Uterus",
    # Spleen
    "S8G12.1": "Spleen",
    "S1A15.20": "Spleen",
}

# Maps package code -> minimum mandatory cooldown in days
PROCEDURE_COOLDOWNS_DAYS: dict[str, int] = {
    "S7F7.2": 180,    # CABG / Open Heart surgery (6 months)
    "M7F3.1": 180,    # Permanent Pacemaker (6 months)
    "S3B10.2": 1095,  # Cataract surgery on same eye (3 years)
}


def _parse_date(d_val: Any) -> date | None:
    if isinstance(d_val, date):
        return d_val
    if isinstance(d_val, str) and d_val.strip():
        try:
            return datetime.fromisoformat(d_val.strip().split("T")[0]).date()
        except ValueError:
            return None
    return None


def check_procedure_restrictions(
    cur,
    case_id: str,
    patient: dict[str, Any],
    encounter: dict[str, Any],
    package_code: str,
) -> tuple[bool, dict[str, Any] | None]:
    """Check whether the package code violates single-excision or cooldown rules.

    Returns:
        (True, None) if rules pass.
        (False, detail_dict) if anatomical impossibility or cooldown violation detected.
    """
    if not package_code:
        return True, None

    organ = SINGLE_EXCISION_ORGAN_BY_CODE.get(package_code)
    cooldown_days = PROCEDURE_COOLDOWNS_DAYS.get(package_code)

    if not organ and not cooldown_days:
        # Not a restricted or cooldown-monitored package
        return True, None

    abha = patient.get("abha_number")
    name = (patient.get("name") or "").strip().lower()
    birth_date = str(patient.get("birth_date") or "")
    current_admit = _parse_date(encounter.get("admission_date"))

    # Query prior approved or active claims for the same patient
    if abha and abha.strip():
        cur.execute(
            """
            SELECT id, tracking_ref, mapped_package_code, encounter->>'admission_date', status
            FROM cases
            WHERE id != %s::uuid
              AND patient->>'abha_number' = %s
              AND status IN ('payer_approved', 'submitted', 'ready_for_dispatch')
              AND mapped_package_code IS NOT NULL
            ORDER BY created_at DESC
            """,
            (case_id, abha.strip()),
        )
    else:
        cur.execute(
            """
            SELECT id, tracking_ref, mapped_package_code, encounter->>'admission_date', status
            FROM cases
            WHERE id != %s::uuid
              AND lower(patient->>'name') = %s
              AND patient->>'birth_date' = %s
              AND status IN ('payer_approved', 'submitted', 'ready_for_dispatch')
              AND mapped_package_code IS NOT NULL
            ORDER BY created_at DESC
            """,
            (case_id, name, birth_date),
        )

    prior_rows = cur.fetchall()

    for prior_id, prior_ref, prior_code, prior_admit_str, prior_status in prior_rows:
        prior_admit = _parse_date(prior_admit_str)

        # 1. Single-Excision Anatomical Impossibility Check
        if organ:
            prior_organ = SINGLE_EXCISION_ORGAN_BY_CODE.get(prior_code)
            if prior_organ == organ:
                detail = {
                    "violation_type": "anatomical_impossibility",
                    "organ": organ,
                    "attempted_code": package_code,
                    "prior_code": prior_code,
                    "prior_case_id": str(prior_id),
                    "prior_tracking_ref": prior_ref,
                    "prior_admission_date": prior_admit_str,
                    "prior_status": prior_status,
                    "message": (
                        f"Anatomical Impossibility: Patient previously underwent {organ} "
                        f"excision ({prior_code}) on {prior_admit_str} (Case: {prior_ref}). "
                        f"The {organ} cannot be excised twice."
                    ),
                }
                return False, detail

        # 2. Procedure Cooldown Window Check
        if cooldown_days and prior_code == package_code:
            if current_admit and prior_admit:
                days_elapsed = (current_admit - prior_admit).days
                if 0 <= days_elapsed < cooldown_days:
                    remaining = cooldown_days - days_elapsed
                    detail = {
                        "violation_type": "cooldown_violation",
                        "package_code": package_code,
                        "cooldown_days": cooldown_days,
                        "days_elapsed": days_elapsed,
                        "remaining_days": remaining,
                        "prior_case_id": str(prior_id),
                        "prior_tracking_ref": prior_ref,
                        "prior_admission_date": prior_admit_str,
                        "message": (
                            f"Procedure Cooldown Violation: Procedure {package_code} was already "
                            f"performed {days_elapsed} days ago on {prior_admit_str} (Case: {prior_ref}). "
                            f"Mandatory scheme cooldown is {cooldown_days} days ({remaining} days remaining)."
                        ),
                    }
                    return False, detail

    return True, None
