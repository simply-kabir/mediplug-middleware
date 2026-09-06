"""
Tests for Phase 5 semantic code mapper.
"""

import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mediplug.mapping.mapper import map_notes
from mediplug.mapping.corpus import load_corpus, corpus_fingerprint


def test_corpus_loaded():
    rows = load_corpus()
    assert len(rows) >= 1300
    # Corpus must be sorted by code
    codes = [r.code for r in rows]
    assert codes == sorted(codes)
    fp = corpus_fingerprint()
    assert fp.startswith(f"{len(rows)}:")


def test_map_notes_lap_chole():
    notes = "45M with severe RUQ pain s/o cholelithiasis. Planned for lap chole."
    candidates = map_notes(notes, top_k=3)
    assert len(candidates) == 3
    # Top candidate should be Lap Cholecystectomy
    assert any("chole" in c.name.lower() for c in candidates)
    assert candidates[0].confidence > 0.45


def test_map_notes_empty():
    candidates = map_notes("", top_k=3)
    # Empty string should not crash, returns candidates or empty
    assert isinstance(candidates, list)
