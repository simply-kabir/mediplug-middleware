"""
Clinical Integrity & Anti-Fraud Engine — Evaluator.

Unifies:
- Pillar 2: Ghost Hospital Concurrent Admissions Check
- Pillar 3: Anatomical Impossibility & Cooldown Restrictions
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .collision import check_concurrent_admission
from .restrictions import check_procedure_restrictions


@dataclass
class IntegrityCheckResult:
    passed: bool
    violation_type: str | None = None
    message: str | None = None
    detail: dict[str, Any] | None = None


def evaluate_clinical_integrity(
    cur,
    case_id: str,
    patient: dict[str, Any],
    encounter: dict[str, Any],
    package_code: str,
) -> IntegrityCheckResult:
    """Evaluate patient claim against all active clinical integrity and anti-fraud checks."""
    # 1. Check for concurrent inpatient admissions across different hospitals
    collision_pass, collision_detail = check_concurrent_admission(
        cur, case_id, patient, encounter
    )
    if not collision_pass and collision_detail:
        return IntegrityCheckResult(
            passed=False,
            violation_type=collision_detail["violation_type"],
            message=collision_detail["message"],
            detail=collision_detail,
        )

    # 2. Check for anatomical impossibility (single-excision) & procedure cooldown windows
    restrictions_pass, rest_detail = check_procedure_restrictions(
        cur, case_id, patient, encounter, package_code
    )
    if not restrictions_pass and rest_detail:
        return IntegrityCheckResult(
            passed=False,
            violation_type=rest_detail["violation_type"],
            message=rest_detail["message"],
            detail=rest_detail,
        )

    return IntegrityCheckResult(passed=True)
