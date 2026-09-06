"""
Package corpus — loaded from the live `packages` table, not from a
`data/packages.json` file (that file was never produced; the DB is the
source of truth — see sih/PHASE5_PREP.md item 1).

IMPORTANT: rows come back `ORDER BY code`, and that order is not
cosmetic. mapper.py's embedding cache is a plain numpy array indexed by
row position, keyed to this exact ordering — if the corpus order ever
drifts between the row list and the embedding cache, lookups silently
return the *wrong* package with no error. Do not remove the ORDER BY,
and do not reshuffle rows after loading.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from functools import lru_cache

import psycopg

from ..config import settings


@dataclass(frozen=True)
class PackageRow:
    code: str
    name: str
    search_text: str


@lru_cache(maxsize=1)
def load_corpus() -> tuple[PackageRow, ...]:
    """Load (code, name, search_text) for every package, ordered by code.
    Cached for the life of the process — call `load_corpus.cache_clear()`
    if `packages` changes and you need a fresh read within the same run."""
    with psycopg.connect(settings.database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "select code, name, coalesce(search_text, name) "
            "from packages order by code"
        )
        rows = cur.fetchall()
    return tuple(PackageRow(code=c, name=n, search_text=s) for c, n, s in rows)


def corpus_fingerprint(rows: tuple[PackageRow, ...] | None = None) -> str:
    """Stable hash of (row count + sorted codes). Used by the embedding
    cache to detect a `packages` reload and rebuild instead of serving
    stale embeddings for a corpus that no longer matches."""
    rows = rows if rows is not None else load_corpus()
    codes = "|".join(r.code for r in rows)
    digest = hashlib.sha256(codes.encode("utf-8")).hexdigest()[:16]
    return f"{len(rows)}:{digest}"
