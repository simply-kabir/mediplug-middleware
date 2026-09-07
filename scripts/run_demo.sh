#!/usr/bin/env bash
#
# run_demo.sh — bring up the whole MediPlug stack from one command
# (guide §12.3: "every service starts from one `docker compose up` or one script").
#
# Docker: redis + mock_payer.  Host (uv): gateway, worker, adjudication poller,
# and the Next.js dev server.  Logs go to logs/*.log, PIDs are tracked, and a
# trap on INT/TERM tears everything down cleanly — which also exercises the
# Phase 9.2 SIGTERM path on every run.
#
# Usage:
#   bash scripts/run_demo.sh            # full stack
#   bash scripts/run_demo.sh --no-ui    # skip the frontend
#
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"
LOG_DIR="$ROOT/logs"
mkdir -p "$LOG_DIR"

WITH_UI=1
[[ "${1:-}" == "--no-ui" ]] && WITH_UI=0

PIDS=()

say()  { printf '\033[1;36m▶ %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m✓ %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

cleanup() {
  echo
  say "Tearing down…"
  for pid in "${PIDS[@]:-}"; do
    if kill -0 "$pid" 2>/dev/null; then
      # SIGTERM (not SIGKILL) — lets the worker run its graceful-shutdown path.
      kill -TERM "$pid" 2>/dev/null || true
    fi
  done
  # Give the worker up to ~15s to finish an in-flight job and log worker_stopped.
  for pid in "${PIDS[@]:-}"; do
    for _ in $(seq 1 30); do kill -0 "$pid" 2>/dev/null || break; sleep 0.5; done
    kill -9 "$pid" 2>/dev/null || true
  done
  docker compose stop redis mock_payer >/dev/null 2>&1 || true
  ok "Down. Logs kept in logs/"
}
trap cleanup INT TERM EXIT

# --------------------------------------------------------------------------
# Preflight — each check fails readably
# --------------------------------------------------------------------------
say "Preflight"

[[ -f "$ROOT/.env" ]] || die ".env missing — cp .env.example .env and fill in DATABASE_URL"

# strip the value, any inline "# comment", and surrounding whitespace/quotes
DISPATCH_MODE="$(grep -E '^DISPATCH_MODE=' .env | tail -1 \
  | sed -E 's/^DISPATCH_MODE=//; s/[[:space:]]*#.*$//; s/^[[:space:]]*//; s/[[:space:]]*$//; s/^["'"'"']//; s/["'"'"']$//')"
[[ "${DISPATCH_MODE:-mock}" == "mock" ]] || die "DISPATCH_MODE='$DISPATCH_MODE' in .env — the demo needs 'mock'"

[[ -f "$ROOT/data/cache/package_embeddings.npy" ]] || die \
  "data/cache/package_embeddings.npy missing (gitignored) — run: uv run python scripts/06_build_embeddings.py  (~48s)"

# Supabase reachability — the corpus + all case state live there.
uv run python - <<'PY' || die "cannot reach the database in DATABASE_URL"
import sys, psycopg
from mediplug.config import settings
try:
    with psycopg.connect(settings.database_url, connect_timeout=8) as c, c.cursor() as cur:
        cur.execute("select 1")
except Exception as e:
    print(e, file=sys.stderr); sys.exit(1)
PY

for port in 8000 8081 3000; do
  if lsof -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
    die "port $port already in use — free it before running the demo"
  fi
done
ok "Preflight passed"

# --------------------------------------------------------------------------
# Docker services
# --------------------------------------------------------------------------
say "Starting redis + mock_payer (docker compose)"
docker compose up -d redis mock_payer

wait_healthy() {
  local svc="$1"
  for _ in $(seq 1 40); do
    local state
    state="$(docker inspect -f '{{.State.Health.Status}}' "$(docker compose ps -q "$svc")" 2>/dev/null || echo starting)"
    [[ "$state" == "healthy" ]] && { ok "$svc healthy"; return 0; }
    sleep 1
  done
  die "$svc did not become healthy — check: docker compose logs $svc"
}
wait_healthy redis
wait_healthy mock_payer

# --------------------------------------------------------------------------
# Host processes
# --------------------------------------------------------------------------
launch() {
  local name="$1"; shift
  say "Launching $name"
  ( "$@" >"$LOG_DIR/$name.log" 2>&1 ) &
  local pid=$!
  PIDS+=("$pid")
  echo "$pid" > "$LOG_DIR/$name.pid"
  ok "$name  (pid $pid, logs/$name.log)"
}

launch gateway     uv run python -m mediplug.gateway
launch worker      uv run python -m mediplug.worker
launch adjudicator uv run python -m mediplug.worker.adjudication

if [[ "$WITH_UI" == "1" ]]; then
  say "Launching frontend (npm run dev)"
  ( cd frontend && npm run dev >"$LOG_DIR/frontend.log" 2>&1 ) &
  fe_pid=$!
  PIDS+=("$fe_pid")
  echo "$fe_pid" > "$LOG_DIR/frontend.pid"
  ok "frontend  (pid $fe_pid, logs/frontend.log)"
fi

# --------------------------------------------------------------------------
# URL table
# --------------------------------------------------------------------------
sleep 2
cat <<EOF

  ┌──────────────────────────────────────────────────────────────┐
  │  MediPlug demo stack is up                                    │
  ├──────────────────────────────────────────────────────────────┤
  │  Gateway API      http://localhost:8000  (/health, /admin/queue)
  │  Mock payer       http://localhost:8081  (/healthz, /received)
  $( [[ "$WITH_UI" == "1" ]] && echo "│  Aarogyamitra UI  http://localhost:3000" )
  │  Redis            localhost:6379         (docker)
  ├──────────────────────────────────────────────────────────────┤
  │  Drive the demo:  uv run python scripts/10_demo.py            │
  │  Failure drills:  uv run python scripts/11_failure_drills.py  │
  │  Stop everything: Ctrl-C here                                 │
  └──────────────────────────────────────────────────────────────┘

EOF

# Hold the foreground so the trap fires on Ctrl-C.
wait
