"""
Embedding cache for the package corpus — shared by mapper.py (read path,
at query time) and scripts/06_build_embeddings.py (write path, pre-built
once so the first real job doesn't pay for a cold model download + a full
1670-row encode).

Cache = a .npy matrix + a sidecar fingerprint file. The fingerprint is
`corpus_fingerprint()` (row count + hash of sorted codes, see corpus.py).
On load, if the fingerprint on disk doesn't match the live corpus, the
cache is treated as stale and rebuilt — this is what keeps a `packages`
table reload from silently serving embeddings for packages that no
longer exist at those row positions.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import numpy as np

from .corpus import PackageRow, corpus_fingerprint

CACHE_DIR = Path("data/cache")
EMBEDDINGS_PATH = CACHE_DIR / "package_embeddings.npy"
FINGERPRINT_PATH = CACHE_DIR / "package_embeddings.fingerprint"


def _read_fingerprint() -> str | None:
    if not FINGERPRINT_PATH.exists():
        return None
    return FINGERPRINT_PATH.read_text(encoding="utf-8").strip()


def _write_fingerprint(fp: str) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    FINGERPRINT_PATH.write_text(fp, encoding="utf-8")


def build_embeddings(
    rows: tuple[PackageRow, ...], model_factory: Callable[[], object]
) -> np.ndarray:
    """Encode every row's search_text and write both the matrix and its
    fingerprint to disk. Always rebuilds — callers wanting the
    cache-or-build behavior should use `load_or_build_embeddings`."""
    model = model_factory()
    texts = [row.search_text for row in rows]
    emb = model.encode(
        texts, normalize_embeddings=True, show_progress_bar=True, batch_size=64
    )
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    np.save(EMBEDDINGS_PATH, emb)
    _write_fingerprint(corpus_fingerprint(rows))
    return emb


def load_or_build_embeddings(
    rows: tuple[PackageRow, ...], model_factory: Callable[[], object]
) -> np.ndarray:
    """Return the cached embedding matrix if it's on disk and matches the
    current corpus; otherwise build it (and cache the result) on the
    spot. Query-path callers (mapper.py) should have already pre-built
    the cache via scripts/06_build_embeddings.py so this is a pure disk
    read in the common case."""
    current_fp = corpus_fingerprint(rows)
    if EMBEDDINGS_PATH.exists() and _read_fingerprint() == current_fp:
        return np.load(EMBEDDINGS_PATH)
    return build_embeddings(rows, model_factory)
