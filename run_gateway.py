import os
import sys
from pathlib import Path

# Ensure src/ is on sys.path regardless of execution environment
src_path = Path(__file__).resolve().parent / "src"
if str(src_path) not in sys.path:
    sys.path.insert(0, str(src_path))

import uvicorn

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    uvicorn.run("mediplug.gateway.main:app", host="0.0.0.0", port=port)
