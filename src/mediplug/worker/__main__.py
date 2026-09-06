"""
Entry point: uv run python -m mediplug.worker
"""

import asyncio

from ..logging import configure

configure()

from .consumer import run  # noqa: E402  — configure logging before imports that use structlog

if __name__ == "__main__":
    asyncio.run(run())
