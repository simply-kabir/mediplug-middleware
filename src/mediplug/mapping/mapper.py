"""
Semantic code mapping — Phase 5.

Pure embeddings fumble "lap chole" -> "Laparoscopic Cholecystectomy" on
their own, so three layers stack:

  1. Abbreviation expansion (abbreviations.py) so the query text looks
     like the corpus text before either scorer runs.
  2. Lexical prefilter (rapidfuzz) — cuts 1670 packages down to a cheap
     shortlist and catches near-exact name matches outright.
  3. Semantic rerank (sentence-transformers) over just that shortlist.

Public interface (locked, see sih/PHASE5_SPLIT.md #1):

    map_notes(notes: str, top_k: int = 3) -> list[CodeCandidate]

Synchronous and CPU-bound on purpose — the worker calls this via
`asyncio.to_thread(map_notes, notes, 3)`, not directly, so rapidfuzz's
scoring and the transformer's forward pass never block the event loop.
"""

from __future__ import annotations

from functools import lru_cache

import numpy as np
from rapidfuzz import fuzz, process

from ..config import settings
from ..schemas import CodeCandidate
from .abbreviations import expand
from .corpus import corpus_fingerprint, load_corpus
from .embeddings_cache import load_or_build_embeddings

_PREFILTER_LIMIT = 60


@lru_cache(maxsize=1)
def _model():
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(settings.embedding_model)


def _corpus_texts() -> tuple[str, ...]:
    return tuple(row.search_text for row in load_corpus())


def map_notes(notes: str, top_k: int = 3) -> list[CodeCandidate]:
    """Return up to `top_k` package candidates for a case's clinical
    notes, confidence descending. Returns [] if the corpus is empty or
    nothing scores above zero — never raises for "no match"."""
    rows = load_corpus()
    if not rows:
        return []

    corpus_texts = _corpus_texts()
    query = expand(notes.lower())

    # Layer 1 — lexical prefilter. Cuts the corpus down cheaply and
    # catches exact/near-exact name matches that embeddings can be
    # surprisingly bad at (short, jargon-heavy medical names).
    limit = min(_PREFILTER_LIMIT, len(corpus_texts))
    prefilter = process.extract(
        query, corpus_texts, scorer=fuzz.token_set_ratio, limit=limit
    )
    idxs = [i for _, _, i in prefilter]

    # Layer 2 — semantic rerank over just that shortlist.
    embeddings = load_or_build_embeddings(rows, _model)
    q_emb = _model().encode([query], normalize_embeddings=True)[0]
    sims = embeddings[idxs] @ q_emb

    order = np.argsort(-sims)[:top_k]
    out: list[CodeCandidate] = []
    for rank in order:
        row = rows[idxs[rank]]
        lexical = prefilter[rank][1] / 100.0
        # Blend so an exact-name lexical hit scores high even if the
        # embedding model doesn't rate it as the closest semantic match.
        score = 0.7 * float(sims[rank]) + 0.3 * lexical
        out.append(
            CodeCandidate(
                code=row.code, name=row.name, confidence=round(min(score, 1.0), 3)
            )
        )
    return sorted(out, key=lambda c: -c.confidence)


def cache_key() -> str:
    """Exposed for scripts/06_build_embeddings.py and the eval harness —
    lets them report which corpus snapshot a cached embedding matrix was
    built against."""
    return corpus_fingerprint()
