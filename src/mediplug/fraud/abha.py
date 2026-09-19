"""
Pillar 1: ABHA Identity & Verhoeff / Format Checksum Validator.

Validates 14-digit ABDM ABHA numbers against:
1. Pattern and length (14 numeric digits, optionally formatted as XX-XXXX-XXXX-XXXX).
2. Repetitive dummy sequences (e.g. 00-0000-0000-0000, 11-1111-1111-1111).
3. Sequential test sequences (e.g. 12-3456-7890-1234).
4. Mathematical Verhoeff checksum algorithm (used by UIDAI and ABDM).
"""

from __future__ import annotations

import re

# Verhoeff algorithm tables
_D_TABLE = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
    [1, 2, 3, 4, 0, 6, 7, 8, 9, 5],
    [2, 3, 4, 0, 1, 7, 8, 9, 5, 6],
    [3, 4, 0, 1, 2, 8, 9, 5, 6, 7],
    [4, 0, 1, 2, 3, 9, 5, 6, 7, 8],
    [5, 9, 8, 7, 6, 0, 4, 3, 2, 1],
    [6, 5, 9, 8, 7, 1, 0, 4, 3, 2],
    [7, 6, 5, 9, 8, 2, 1, 0, 4, 3],
    [8, 7, 6, 5, 9, 3, 2, 1, 0, 4],
    [9, 8, 7, 6, 5, 4, 3, 2, 1, 0],
]

_P_TABLE = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
    [1, 5, 7, 6, 2, 8, 3, 0, 9, 4],
    [5, 8, 0, 3, 7, 9, 6, 1, 4, 2],
    [8, 9, 1, 6, 0, 4, 3, 5, 2, 7],
    [9, 4, 5, 3, 1, 2, 6, 8, 7, 0],
    [4, 2, 8, 6, 5, 7, 3, 9, 0, 1],
    [2, 7, 9, 3, 8, 0, 6, 4, 1, 5],
    [7, 0, 4, 6, 9, 1, 3, 2, 5, 8],
]

_INV_TABLE = [0, 4, 3, 2, 1, 5, 6, 7, 8, 9]

_KNOWN_DUMMY_PATTERNS = {
    "12345678901234",
    "01234567890123",
    "98765432109876",
}


def compute_verhoeff_checksum(num_str: str) -> str:
    """Compute the Verhoeff check digit for a numeric string."""
    c = 0
    for i, digit in enumerate(reversed(num_str)):
        c = _D_TABLE[c][_P_TABLE[(i + 1) % 8][int(digit)]]
    return str(_INV_TABLE[c])


def validate_verhoeff(num_str: str) -> bool:
    """Validate a numeric string against the Verhoeff algorithm."""
    if not num_str.isdigit():
        return False
    c = 0
    for i, digit in enumerate(reversed(num_str)):
        c = _D_TABLE[c][_P_TABLE[i % 8][int(digit)]]
    return c == 0


def is_dummy_sequence(digits: str) -> bool:
    """Check if the digit string is a repetitive or trivial dummy sequence."""
    if len(set(digits)) == 1:
        # All same digit e.g. 00000000000000, 11111111111111
        return True
    if digits in _KNOWN_DUMMY_PATTERNS:
        return True
    return False


def validate_abha(abha: str | None) -> tuple[bool, str | None]:
    """Validate an ABHA number.

    Returns:
        (True, None) if valid.
        (False, error_message) if invalid.
    """
    if not abha or not abha.strip():
        return True, None

    cleaned = abha.strip()

    # Formatted pattern: XX-XXXX-XXXX-XXXX
    if "-" in cleaned:
        if not re.fullmatch(r"^\d{2}-\d{4}-\d{4}-\d{4}$", cleaned):
            return False, "ABHA format must be XX-XXXX-XXXX-XXXX with 14 numeric digits"
        raw_digits = cleaned.replace("-", "")
    else:
        # Raw 14 digits
        if not re.fullmatch(r"^\d{14}$", cleaned):
            return False, "ABHA must contain exactly 14 numeric digits"
        raw_digits = cleaned

    # Check for dummy sequences
    if is_dummy_sequence(raw_digits):
        return False, f"ABHA number '{abha}' is an invalid dummy sequence"

    # Check Verhoeff checksum
    if not validate_verhoeff(raw_digits):
        return False, f"ABHA checksum validation failed for '{abha}'"

    return True, None


def validate_phr_address(phr: str | None) -> tuple[bool, str | None]:
    """Validate an ABDM PHR address (e.g. user@abdm, user@sbx)."""
    if not phr or not phr.strip():
        return True, None

    cleaned = phr.strip().lower()
    pattern = r"^[a-zA-Z0-9_\.]{3,32}@(abdm|sbx|abdm\.gov\.in)$"
    if not re.fullmatch(pattern, cleaned):
        return False, "PHR address must be in username@abdm or username@sbx format (3-32 characters)"

    return True, None
