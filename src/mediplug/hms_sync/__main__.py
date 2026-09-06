"""
Entry point: python -m mediplug.hms_sync

Polls the teammate's HMS Supabase for new encounters and syncs them
to our gateway.
"""

import asyncio

from ..logging import configure

configure()

from .poll import run_forever  # noqa: E402

if __name__ == "__main__":
    asyncio.run(run_forever())
