"""
Phase 8 — dispatch abstraction.

Getting payer credentials should be a `.env` change, never a code change.
Everything that submits a bundle goes through the `Dispatcher` Protocol;
`get_dispatcher()` picks the implementation from `settings.dispatch_mode`
(`"mock"` default, `"nhcx"` once a sandbox lands).

Locked interface — `pipeline.py` (Kabir) depends on this shape:

    result = await get_dispatcher().submit(bundle, stage)
    # result.accepted        -> bool
    # result.correlation_id   -> str | None   (payer's async tracking id)
    # result.raw_response     -> dict          (persist to dispatch_response)
    # result.error            -> str | None
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class DispatchResult:
    accepted: bool
    correlation_id: str | None
    raw_response: dict = field(default_factory=dict)
    error: str | None = None


class Dispatcher(Protocol):
    async def submit(self, bundle: dict, stage: str) -> DispatchResult: ...


def get_dispatcher() -> Dispatcher:
    """Resolve the configured dispatcher. Import-lazy so selecting `mock`
    never imports the NHCX SDK and vice versa."""
    from ..config import settings

    if settings.dispatch_mode == "nhcx":
        from .nhcx import NHCXDispatcher

        return NHCXDispatcher()

    from .mock import MockPayerDispatcher

    return MockPayerDispatcher()
