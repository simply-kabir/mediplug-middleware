"""
Gateway runner entry point.

Handles Windows asyncio event loop requirements for psycopg's AsyncConnectionPool
and launches uvicorn.

Run with:  python -m mediplug.gateway
"""

import asyncio
import selectors
import sys
import uvicorn


def main():
    config = uvicorn.Config("mediplug.gateway.main:app", host="0.0.0.0", port=8000, log_level="info")
    server = uvicorn.Server(config)

    if sys.platform == "win32":
        loop_factory = lambda: asyncio.SelectorEventLoop(selectors.SelectSelector())
        asyncio.run(server.serve(), loop_factory=loop_factory)
    else:
        asyncio.run(server.serve())


if __name__ == "__main__":
    main()
