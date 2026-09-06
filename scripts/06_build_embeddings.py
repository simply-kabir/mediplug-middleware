"""
Pre-build the package embedding cache — Phase 5.

Run this once after every `packages` reload, before the worker handles a
real job. Without it, the first job to call map_notes() pays for the
sentence-transformers model download plus a full encode of every package
row, inline, inside a case's processing time.

Usage:
    uv run python scripts/06_build_embeddings.py

Safe to re-run any time: it always rebuilds and overwrites the cache
(load_or_build_embeddings would skip work if the fingerprint already
matches, but this script's whole job is to force a fresh build, e.g.
right after a reload).
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mediplug.mapping.corpus import load_corpus  # noqa: E402
from mediplug.mapping.embeddings_cache import (  # noqa: E402
    EMBEDDINGS_PATH,
    build_embeddings,
)
from mediplug.mapping.mapper import _model  # noqa: E402


def main() -> None:
    rows = load_corpus()
    if not rows:
        print("packages table is empty — nothing to embed. Load Phase 1 data first.")
        sys.exit(1)

    print(f"Loaded {len(rows)} packages from the DB.")
    print("Downloading/loading the embedding model (first run pulls ~2GB of "
          "PyTorch + the model weights)...")

    start = time.monotonic()
    emb = build_embeddings(rows, _model)
    elapsed = time.monotonic() - start

    print(f"Encoded {emb.shape[0]} rows, dim={emb.shape[1]}, in {elapsed:.1f}s.")
    print(f"Cache written: {EMBEDDINGS_PATH}")


if __name__ == "__main__":
    main()
