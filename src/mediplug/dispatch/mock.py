"""
Phase 8 — mock payer dispatcher.

POSTs the FHIR Bundle to a stand-in MJPJAY payer (`mock_payer/main.py`,
run separately on :8081). That mock **actually validates** the Claim, so a
missing `diagnosis` / `patient` / etc. comes back as an HTTP 4xx and this
returns `accepted=False` — the whole point of the exercise.

Retry (tenacity) covers transient transport failures only. A 4xx from the
payer is a real rejection, not a blip: it's returned as a `DispatchResult`,
not raised, so it does not retry.
"""

from __future__ import annotations

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from ..config import settings
from .base import DispatchResult


class MockPayerDispatcher:
    def __init__(
        self,
        base_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        # `transport` is an injection seam for tests (httpx.ASGITransport
        # wrapping the mock app) — production leaves it None for real HTTP.
        self._base_url = (base_url or settings.mock_payer_url).rstrip("/")
        self._transport = transport

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, max=10),
        reraise=True,
    )
    async def submit(self, bundle: dict, stage: str) -> DispatchResult:
        flow = "preauth" if stage == "preauth" else "claim"
        url = f"{self._base_url}/v0.7/{flow}/submit"

        async with httpx.AsyncClient(timeout=30, transport=self._transport) as client:
            resp = await client.post(url, json={"bundle": bundle})

        body = resp.json() if resp.content else {}
        if resp.status_code >= 400:
            return DispatchResult(
                accepted=False,
                correlation_id=None,
                raw_response=body,
                error=f"HTTP {resp.status_code}: {resp.text[:300]}",
            )
        return DispatchResult(
            accepted=True,
            correlation_id=body.get("correlation_id"),
            raw_response=body,
        )
