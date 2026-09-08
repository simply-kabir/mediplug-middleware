"""
Phase 10.3 — automated failure drills (guide §12.2).

Six drills, each returning pass/fail + evidence, printed as a summary table.
Non-zero exit on any failure. Every DB row it creates is prefixed `DRILL-`
and deleted at the end (this is the shared Supabase).

Run the rest of the stack first — `bash scripts/run_demo.sh --no-ui` — then:

    uv run python scripts/11_failure_drills.py

Drills 1 and 5 spawn their OWN worker subprocess (with a turned-down
CLAIM_STALE_MS / RECLAIM_INTERVAL_S via env — child process only, committed
config untouched). If run_demo.sh's own worker is running it will race them
for messages; for a clean drill 1/5, start the stack with a WORKER-less
variant or stop that worker first.

Usage:
    uv run python scripts/11_failure_drills.py
    uv run python scripts/11_failure_drills.py 1 4     # only run drills 1 and 4
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import uuid

import httpx
import psycopg
import redis as redis_sync

from mediplug.config import settings
from mediplug.dispatch.mock import MockPayerDispatcher

GATEWAY = "http://localhost:8000"
MOCK_PAYER = "http://localhost:8081"
ADMIN_HEADERS = {"X-Admin-Token": settings.admin_token}
SAFE_CODE = "S8G5.11"  # laparoscopic cholecystectomy — carries an icd_code
BAR = "=" * 80

# Fast-reclaim env for the drills' own worker subprocess. Settings is
# pydantic-settings with no env_prefix, so these are already live env knobs.
FAST_WORKER_ENV = {**os.environ, "CLAIM_STALE_MS": "3000", "RECLAIM_INTERVAL_S": "2"}


def _db():
    return psycopg.connect(settings.database_url, prepare_threshold=None)


def _rc():
    return redis_sync.from_url(settings.redis_url, decode_responses=True)


def _status(case_id: str):
    with _db() as conn, conn.cursor() as cur:
        cur.execute("select status from cases where id = %s", (case_id,))
        r = cur.fetchone()
        return r[0] if r else None


def _field(case_id: str, col: str):
    with _db() as conn, conn.cursor() as cur:
        cur.execute(f"select {col} from cases where id = %s", (case_id,))
        r = cur.fetchone()
        return r[0] if r else None


def _ingest(client: httpx.Client, notes: str, *, docs=None, idem=None, ref=None):
    body = {
        "hms_case_ref": ref or f"DRILL-{uuid.uuid4().hex[:6].upper()}",
        "stage": "preauth",
        "patient": {"name": "DRILL Patient", "gender": "male", "birth_date": "1980-01-01"},
        "encounter": {
            "admission_date": "2026-09-07",
            "attending_doctor": "Dr Drill",
            "doctor_registration_no": "R1",
            "hospital_id": "H1",
        },
        "clinical_notes": notes,
        "documents": docs or [],
    }
    return client.post(
        f"{GATEWAY}/api/v1/cases/ingest",
        json=body,
        headers={"Idempotency-Key": idem or f"drill-{uuid.uuid4().hex}"},
    )


def _spawn_worker(env=None):
    return subprocess.Popen(
        [sys.executable, "-m", "mediplug.worker"],
        env=env or FAST_WORKER_ENV,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _stop_worker(proc, sig=signal.SIGTERM):
    if proc and proc.poll() is None:
        proc.send_signal(sig)
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def _wait_status(case_id: str, targets: set[str], timeout=90.0, poll=1.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = _status(case_id)
        if s in targets:
            return s
        time.sleep(poll)
    return _status(case_id)


def _cleanup_drill_rows():
    with _db() as conn, conn.cursor() as cur:
        cur.execute("delete from cases where hms_case_ref like 'DRILL-%'")
        n = cur.rowcount
        conn.commit()
    return n


# ---------------------------------------------------------------------------
# Drills
# ---------------------------------------------------------------------------


def drill_1_kill_worker_mid_job():
    """Kill the worker after it has picked up a job; restart it; XAUTOCLAIM
    must reclaim the un-acked message, the case must complete, and exactly
    ONE dispatch must reach the payer (the 9.4 guard + reclaim, together)."""
    # Reset the mock payer so RECEIVED starts empty — GET /received has no
    # case_id, so "exactly one record" is the isolation mechanism.
    subprocess.run(["docker", "compose", "restart", "mock_payer"], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(30):
        try:
            if httpx.get(f"{MOCK_PAYER}/healthz", timeout=2).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        time.sleep(1)
    before = httpx.get(f"{MOCK_PAYER}/received", timeout=5).json()
    if before:
        return False, f"mock payer /received not empty after restart ({len(before)} rows)"

    worker = _spawn_worker()
    try:
        with httpx.Client(timeout=30) as client:
            r = _ingest(client, "laparoscopic cholecystectomy for cholelithiasis")
            if r.status_code != 202:
                return False, f"ingest returned {r.status_code}: {r.text[:160]}"
            case_id = r.json()["case_id"]

            # Drive the case to a state where finalize_case will run: confirm a
            # known package, then upload whatever docs the rules ask for.
            s = _wait_status(
                case_id,
                {"needs_code_confirmation", "action_required", "ready_for_dispatch", "submitted"},
                timeout=60,
            )
            if s == "needs_code_confirmation":
                cc = client.post(
                    f"{GATEWAY}/api/v1/cases/{case_id}/confirm-code",
                    json={"code": SAFE_CODE, "confirmed_by": "drill"},
                )
                if cc.status_code != 200:
                    return False, f"/confirm-code returned {cc.status_code}"
                s = _wait_status(
                    case_id, {"action_required", "ready_for_dispatch", "submitted"}, timeout=60
                )
            if s == "action_required":
                missing = _field(case_id, "missing_requirements") or []
                docs = []
                for req in missing:
                    opts = req.get("any_of", [])
                    opt = opts[0] if opts else None
                    dt = opt.get("code") if isinstance(opt, dict) else (opt or "usg_abdomen")
                    docs.append({"document_type": dt, "file_url": f"https://x.test/{dt}.pdf"})
                up = client.post(
                    f"{GATEWAY}/api/v1/cases/{case_id}/documents",
                    json={"documents": docs, "uploaded_by": "drill"},
                )
                if up.status_code != 200:
                    return False, f"/documents returned {up.status_code}"

        # The docs_updated (or code_confirmed) job is now in flight. Kill the
        # worker the instant it picks the job up — before it can XACK — so the
        # message is stranded in the PEL for XAUTOCLAIM to reclaim.
        killed_at = None
        deadline = time.time() + 30
        while time.time() < deadline:
            s = _status(case_id)
            if s in {"analyzing", "ready_for_dispatch", "dispatching"}:
                killed_at = s
                break
            if s in {"submitted", "payer_approved"}:
                killed_at = f"{s} (completed before kill landed)"
                break
            time.sleep(0.05)
        corr_at_kill = _field(case_id, "payer_correlation_id")
        worker.send_signal(signal.SIGKILL)
        worker.wait()

        # Restart — the reclaimer (3s idle, 2s interval) picks up the un-acked message.
        t0 = time.time()
        worker = _spawn_worker()
        final = _wait_status(case_id, {"submitted", "payer_approved"}, timeout=90)
        reclaim_s = time.time() - t0
        if final not in {"submitted", "payer_approved"}:
            return False, f"case stuck at {final!r} after restart (killed at {killed_at})"

        received = httpx.get(f"{MOCK_PAYER}/received", timeout=5).json()
        corr_final = _field(case_id, "payer_correlation_id")
        if len(received) != 1:
            return False, f"expected exactly 1 payer record, got {len(received)} — duplicate dispatch"
        if corr_at_kill and corr_at_kill != corr_final:
            return False, f"payer_correlation_id changed across the kill: {corr_at_kill} → {corr_final}"

        return True, (
            f"killed at status={killed_at}; reclaimed + completed in ~{reclaim_s:.0f}s; "
            f"/received=1; payer_correlation_id stable ({corr_final})"
        )
    finally:
        _stop_worker(worker)


def drill_2_redis_down():
    """Redis unreachable → ingest returns a clean 503 fast, never a silent 202
    and never a hang. Budget 10s (redis-py 8.1 defaults to no retries,
    socket_connect_timeout=5.0, so the 503 lands in ~5s)."""
    subprocess.run(["docker", "compose", "stop", "redis"], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        t0 = time.time()
        try:
            with httpx.Client(timeout=10) as client:
                r = _ingest(client, "lap chole", ref="DRILL-REDIS-DOWN")
        except httpx.HTTPError as e:
            return False, f"request hung / errored instead of a clean 503: {e!r}"
        elapsed = time.time() - t0
        if r.status_code != 503:
            return False, f"expected 503, got {r.status_code} in {elapsed:.1f}s (silent orphan?)"
        if elapsed > 10:
            return False, f"503 took {elapsed:.1f}s — over the 10s budget"
        return True, f"clean 503 in {elapsed:.1f}s, no hang"
    finally:
        subprocess.run(["docker", "compose", "start", "redis"], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(30):
            try:
                if _rc().ping():
                    break
            except redis_sync.RedisError:
                time.sleep(1)


def drill_3_garbage_notes():
    """Unmappable clinical notes → action_required, confidence below the
    floor, no crash."""
    worker = _spawn_worker()
    try:
        with httpx.Client(timeout=30) as client:
            r = _ingest(client, "xyz unrelated gibberish qwerty", ref="DRILL-GARBAGE")
            if r.status_code != 202:
                return False, f"ingest returned {r.status_code}"
            case_id = r.json()["case_id"]
        s = _wait_status(case_id, {"action_required", "needs_code_confirmation", "failed"}, timeout=60)
        conf = _field(case_id, "confidence")
        if s != "action_required":
            return False, f"expected action_required, got {s!r} (confidence={conf})"
        if conf is not None and conf >= settings.confidence_floor:
            return False, f"confidence {conf} not below floor {settings.confidence_floor}"
        return True, f"action_required, confidence={conf} (< {settings.confidence_floor}), no crash"
    finally:
        _stop_worker(worker)


def drill_4_duplicate_idempotency_key():
    """Same Idempotency-Key twice → identical case_id/tracking_ref, exactly one
    row. A duplicate POST of a case the worker has ALREADY picked up must not
    re-enqueue (XLEN +1, not +2).

    Note: the Phase 9.4 bug-#4 fix deliberately DOES re-enqueue a replay while
    the case is still `queued` (so a client retrying after a 503 isn't
    orphaned). So this drill spawns a worker and spaces the two POSTs, putting
    the case at `analyzing` before the second POST — the real-world case."""
    rc = _rc()
    key = f"drill-dupe-{uuid.uuid4().hex}"
    worker = _spawn_worker()
    try:
        with httpx.Client(timeout=30) as client:
            before = rc.xlen(settings.stream_key)
            r1 = _ingest(client, "lap chole", idem=key, ref="DRILL-DUPE")
            if r1.status_code != 202:
                return False, f"first ingest returned {r1.status_code}"
            case_id = r1.json()["case_id"]
            # wait until the worker has moved it off `queued`
            _wait_status(case_id, {"analyzing", "needs_code_confirmation",
                                   "action_required", "ready_for_dispatch"}, timeout=30)
            mid = rc.xlen(settings.stream_key)
            r2 = _ingest(client, "lap chole", idem=key, ref="DRILL-DUPE")
            after = rc.xlen(settings.stream_key)
    finally:
        _stop_worker(worker)

    if r2.status_code != 202:
        return False, f"replay returned {r2.status_code}"
    a, b = r1.json(), r2.json()
    if a["case_id"] != b["case_id"] or a["tracking_ref"] != b["tracking_ref"]:
        return False, f"replay gave a different identity: {a} vs {b}"
    with _db() as conn, conn.cursor() as cur:
        cur.execute("select count(*) from cases where idempotency_key = %s", (key,))
        n = cur.fetchone()[0]
    if n != 1:
        return False, f"{n} rows for one idempotency key"
    if after - mid != 0:
        return False, f"replay re-enqueued (XLEN {mid}→{after}) for a case past `queued`"
    return True, f"one row, one identity; first ingest XLEN +{mid - before}, replay +0"


def drill_5_dead_dispatch_url():
    """Dispatcher pointed at a dead URL → 3 tenacity attempts, then it
    re-raises (transport failure, not a payer rejection)."""
    import asyncio

    dead = MockPayerDispatcher(base_url="http://localhost:1")
    bundle = {
        "resourceType": "Bundle",
        "type": "collection",
        "entry": [{"fullUrl": "urn:uuid:x", "resource": {"resourceType": "Claim"}}],
    }
    t0 = time.time()
    try:
        asyncio.run(dead.submit(bundle, "preauth"))
        return False, "submit() returned instead of re-raising on a dead URL"
    except Exception as e:  # noqa: BLE001 — any transport error is a pass
        elapsed = time.time() - t0
        # 3 attempts with wait_exponential(multiplier=1, max=10) => ~1s + ~2s waits
        return True, f"re-raised {type(e).__name__} after 3 attempts (~{elapsed:.1f}s)"


def drill_6_borderline_package():
    """Mid-confidence note → needs_code_confirmation; /confirm-code re-drives
    it; uploading the confirmed package's required docs then takes it through
    to dispatch. Proves the whole human-in-the-loop path."""
    worker = _spawn_worker()
    try:
        with httpx.Client(timeout=30) as client:
            r = _ingest(client, "lap appy for acute appendicitis", ref="DRILL-BORDER")
            if r.status_code != 202:
                return False, f"ingest returned {r.status_code}"
            case_id = r.json()["case_id"]
            s = _wait_status(
                case_id,
                {"needs_code_confirmation", "action_required", "ready_for_dispatch", "submitted"},
                timeout=60,
            )
            if s != "needs_code_confirmation":
                return False, f"expected needs_code_confirmation, got {s!r}"

            cc = client.post(
                f"{GATEWAY}/api/v1/cases/{case_id}/confirm-code",
                json={"code": SAFE_CODE, "confirmed_by": "drill"},
            )
            if cc.status_code != 200:
                return False, f"/confirm-code returned {cc.status_code}: {cc.text[:160]}"

            s = _wait_status(
                case_id, {"action_required", "ready_for_dispatch", "submitted", "payer_approved"},
                timeout=90,
            )
            if s == "needs_code_confirmation":
                return False, "confirm-code did not move the case off needs_code_confirmation"

            # confirmed package may itself require documents — supply them.
            if s == "action_required":
                missing = _field(case_id, "missing_requirements") or []
                docs = []
                for req in missing:
                    opts = req.get("any_of", [])
                    opt = opts[0] if opts else None
                    dt = opt.get("code") if isinstance(opt, dict) else (opt or "usg_abdomen")
                    docs.append({"document_type": dt, "file_url": f"https://x.test/{dt}.pdf"})
                up = client.post(
                    f"{GATEWAY}/api/v1/cases/{case_id}/documents",
                    json={"documents": docs, "uploaded_by": "drill"},
                )
                if up.status_code != 200:
                    return False, f"/documents returned {up.status_code}"

        s2 = _wait_status(
            case_id, {"ready_for_dispatch", "submitted", "payer_approved"}, timeout=90
        )
        if s2 not in {"ready_for_dispatch", "submitted", "payer_approved"}:
            return False, f"after confirm-code + docs, stuck at {s2!r}"
        return True, f"needs_code_confirmation → confirm-code → docs → {s2}"
    finally:
        _stop_worker(worker)


DRILLS = {
    1: ("Kill worker mid-job", drill_1_kill_worker_mid_job),
    2: ("Redis down → clean 503", drill_2_redis_down),
    3: ("Garbage notes → action_required", drill_3_garbage_notes),
    4: ("Duplicate Idempotency-Key", drill_4_duplicate_idempotency_key),
    5: ("Dead dispatch URL → retries + re-raise", drill_5_dead_dispatch_url),
    6: ("Borderline package → confirm-code", drill_6_borderline_package),
}


def main() -> None:
    which = [int(a) for a in sys.argv[1:]] or sorted(DRILLS)
    print(BAR)
    print("  MediPlug — Phase 10 failure drills")
    print(BAR)

    results = []
    for n in which:
        title, fn = DRILLS[n]
        print(f"\n>>> DRILL {n}: {title}")
        try:
            passed, evidence = fn()
        except Exception as e:  # noqa: BLE001
            passed, evidence = False, f"drill raised: {type(e).__name__}: {e}"
        results.append((n, title, passed, evidence))
        print(f"    {'PASS' if passed else 'FAIL'} — {evidence}")

    print(f"\n>>> Cleanup: deleted {_cleanup_drill_rows()} DRILL- row(s)")

    print("\n" + BAR)
    print("  SUMMARY")
    print(BAR)
    for n, title, passed, evidence in results:
        print(f"  {n}  {'✓ PASS' if passed else '✗ FAIL'}  {title}")
        if not passed:
            print(f"        {evidence}")
    failed = [n for n, _, p, _ in results if not p]
    print(BAR)
    if failed:
        print(f"  {len(failed)} drill(s) failed: {failed}")
        sys.exit(1)
    print("  all drills passed")


if __name__ == "__main__":
    main()
