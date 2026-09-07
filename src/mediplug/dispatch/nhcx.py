"""
Phase 8 — NHCX dispatcher (thin stub).

`DISPATCH_MODE=mock` is the demo default (guide §10, hard rule). This is
only wired up if a sandbox credential set actually lands the night before.

When that happens, this becomes a thin wrapper over the Swasth HCX Python
integrator SDK (`Swasth-Digital-Health-Foundation/integration-sdks`,
`python/`) — prefer the SDK over hand-rolling the JWE crypto. Sketch:

    from hcx_integrator_sdk import HCXIntegrator   # check the SDK README
    self.hcx = HCXIntegrator(
        protocol_base_path=settings.nhcx_base_url,
        participant_code=settings.nhcx_participant_code,
        username=settings.nhcx_username,
        password=settings.nhcx_password,
        private_key=open(settings.nhcx_private_key_path).read(),
    )
    ok, response = self.hcx.process_outgoing_request(
        fhir_payload=bundle,
        operation="preauth_submit" if stage == "preauth" else "claim_submit",
        recipient_code=settings.nhcx_recipient_code,
    )

The payer replies asynchronously to a public callback
(`/v0.7/{flow}/on_submit`) — that endpoint and the JWE decrypt are the
other half, out of scope until credentials exist.
"""

from __future__ import annotations

from .base import DispatchResult


class NHCXDispatcher:
    def __init__(self) -> None:
        raise NotImplementedError(
            "NHCX dispatch is not wired yet. Keep DISPATCH_MODE=mock. "
            "See this module's docstring and guide §10 Scenario B."
        )

    async def submit(self, bundle: dict, stage: str) -> DispatchResult:  # pragma: no cover
        raise NotImplementedError
