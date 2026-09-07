"""
Pre-flight rule engine — Phase 6.

Pure functional validator that checks uploaded documents against package requirements.
Zero external dependencies (no DB, no Redis, no network).
"""

from __future__ import annotations

import re
from typing import Any, Iterable, NamedTuple


class RuleEvaluation(NamedTuple):
    passed: bool
    missing_requirements: list[dict[str, Any]]


def _normalize_doc_type(doc: str) -> str:
    """Normalize a document type string for robust comparison.
    
    Handles case differences, hyphens, spaces, and underscores:
    e.g. 'USG_Abdomen', 'usg abdomen', 'USG-Abdomen' -> 'usg_abdomen'
    """
    if not doc:
        return ""
    # Strip whitespace, lowercase, and convert separators (-, space, _) to a single underscore
    cleaned = doc.strip().lower()
    return re.sub(r"[\s\-_]+", "_", cleaned)


def evaluate(
    requirements: list[dict[str, Any]] | None,
    uploaded_docs: Iterable[str | dict[str, Any]] | None,
) -> RuleEvaluation:
    """Evaluate whether uploaded documents satisfy package requirements.

    Args:
        requirements: List of requirement dicts for the package/stage.
            Each requirement is expected to have:
              - 'requirement_id': identifier string (e.g. 'req_0')
              - 'any_of': list of options (either strings like 'usg_abdomen'
                          or dicts like {'code': 'usg_abdomen', 'label': '...'})
              - 'human_label': readable description
        uploaded_docs: Iterable of uploaded document types.
            Can be strings (e.g. 'usg_abdomen', 'clinical_notes')
            or dicts containing 'document_type' (from DB/API rows).

    Returns:
        RuleEvaluation(passed: bool, missing_requirements: list[dict])
        - passed is True if all requirements are satisfied (or requirements is empty).
        - missing_requirements contains all requirement dicts that were not satisfied.
    """
    if not requirements:
        return RuleEvaluation(passed=True, missing_requirements=[])

    # Extract and normalize all uploaded document types
    normalized_uploaded: set[str] = set()
    if uploaded_docs:
        for item in uploaded_docs:
            if isinstance(item, dict):
                raw_code = item.get("document_type") or item.get("code") or ""
            else:
                raw_code = str(item)
            norm = _normalize_doc_type(raw_code)
            if norm:
                normalized_uploaded.add(norm)

    missing: list[dict[str, Any]] = []

    for req in requirements:
        any_of = req.get("any_of", [])
        satisfied = False

        for option in any_of:
            if isinstance(option, dict):
                opt_code = option.get("code", "")
            else:
                opt_code = str(option)

            norm_opt = _normalize_doc_type(opt_code)
            if norm_opt and norm_opt in normalized_uploaded:
                satisfied = True
                break

        if not satisfied:
            missing.append({**req, "satisfied": False})

    return RuleEvaluation(
        passed=(len(missing) == 0),
        missing_requirements=missing,
    )
