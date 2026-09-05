# MediPlug Middleware

Async middleware for MJPJAY preauth/claims automation: ingest gateway →
Redis Streams → worker (semantic code mapping → pre-flight rules → FHIR
bundle → dispatch).

## Day 1 setup

```bash
# 1. Python 3.11+
python3 --version

# 2. uv (fast package manager) — if astral.sh is blocked on your network,
#    install uv via pip instead:
curl -LsSf https://astral.sh/uv/install.sh | sh
# or: pip install uv

# 3. Install dependencies
uv venv
source .venv/bin/activate
uv pip install -e ".[dev]" 2>/dev/null || uv sync

# 4. Redis (+ local Postgres if not using hosted Supabase)
docker compose up -d redis
redis-cli ping   # → PONG

# 5. Env file
cp .env.example .env   # then fill in real values
```

## What's here so far (Day 1 morning: env + Phase 0 contracts)

- `pyproject.toml` — all deps from the build guide
- `docker-compose.yml` — Redis + optional local Postgres
- `.env.example` — every config variable, documented
- `src/mediplug/config.py` — typed Settings loaded from `.env`
- `src/mediplug/logging.py` — structlog setup, call `configure()` once at
  process start in both the gateway and the worker
- `src/mediplug/schemas.py` — **Phase 0 contracts.** The case status state
  machine, the Redis job envelope shape, the ingest API request/response,
  and the `missing_requirements` shape the frontend renders. Get explicit
  sign-off from the team on this file before writing anything downstream.

## Not yet built (next phases)

- `data/packages.json` — package master extraction pipeline
- DB schema (Postgres/Supabase)
- Ingest gateway (`src/mediplug/gateway/main.py`)
- Worker skeleton + consumer + pipeline
- Semantic code mapping
- Pre-flight rule engine
- FHIR R4 bundle builder
- Dispatch (mock + NHCX)

## Ownership boundary

```
[ Mock HMS ]          ← teammate
     │ HTTP POST
     ▼
[ Ingest Gateway ]    ← you (thin, ~150 lines)
     │ XADD
     ▼
[ Redis Stream ]      ← you
     │ XREADGROUP
     ▼
[ Worker Engine ]     ← you (the actual project)
     │ UPDATE cases
     ▼
[ Postgres/Supabase ] ← shared, schema owned by you
     │ Realtime
     ▼
[ Aarogyamitra UI ]   ← teammate
```
