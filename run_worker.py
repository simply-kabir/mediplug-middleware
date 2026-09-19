import os
import sys
from pathlib import Path

# Ensure src/ is on sys.path regardless of execution environment
src_path = Path(__file__).resolve().parent / "src"
if str(src_path) not in sys.path:
    sys.path.insert(0, str(src_path))

from mediplug.logging import configure
configure()

import asyncio
from mediplug.worker.consumer import run

if __name__ == "__main__":
    asyncio.run(run())
