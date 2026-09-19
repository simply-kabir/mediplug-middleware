"""
Anti-Fraud & Clinical Integrity Package.

Pillar 1: ABHA Identity & Checksum Validation (abha.py)
Pillar 2: Ghost Hospital Concurrent Admissions Check (collision.py)
Pillar 3: Anatomical Impossibility & Cooldown Restrictions (restrictions.py)
Engine: Unified Integrity Check (engine.py)
"""

from .abha import validate_abha, validate_phr_address, validate_verhoeff
from .collision import check_concurrent_admission
from .engine import IntegrityCheckResult, evaluate_clinical_integrity
from .restrictions import check_procedure_restrictions

__all__ = [
    "validate_abha",
    "validate_phr_address",
    "validate_verhoeff",
    "check_concurrent_admission",
    "check_procedure_restrictions",
    "evaluate_clinical_integrity",
    "IntegrityCheckResult",
]
