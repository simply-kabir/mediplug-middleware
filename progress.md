# MediPlug Middleware — Progress Log

> **How to use this file:** don't rewrite it. After each work session, paste the
> new "Session update" block (given to you in chat) at the bottom, under
> "## Log". Everything above "## Log" is stable background — architecture,
> ownership, and what's already done — and shouldn't need to change often.

---

## Project overview

**MediPlug** — the async middleware layer for an MJPJAY (Indian state health
insurance) preauth/claims automation system, built for SIH. Full spec:
`MEDIPLUG_MIDDLEWARE_BUILD_GUIDE.md`.

**Kabir's ownership scope:** ingest gateway, Redis Streams queue, worker
engine, semantic code mapping, pre-flight rule engine, FHIR R4 bundle
builder, dispatch (mock payer + NHCX). Frontend (Aarogyamitra UI) and Mock
HMS are owned by teammates.

**Architecture:**
```
[Mock HMS] → [Ingest Gateway] → [Redis Stream] → [Worker Engine] → [Postgres/Supabase] → Realtime → [Aarogyamitra UI]
   teammate        Kabir            Kabir            Kabir          Kabir (schema)         teammate
```

**Tech stack:** Python 3.11+, FastAPI, Redis Streams (not BullMQ), Postgres
via Supabase, `fhir.resources`, `pdfplumber`/`pandas`/`rapidfuzz`/
`sentence-transformers` for the data pipeline.

**Guide's phase breakdown:**
```
Phase 0 — Contracts                          ✅ DONE
Phase 1 — Package master data pipeline       ✅ DONE
Phase 2 — Database schema                    ✅ DONE
Phase 3 — Ingest gateway                     ✅ DONE
Phase 4 — Worker skeleton (walking skeleton) ✅ DONE
Phase 5 — Semantic code mapping              ✅ DONE
Phase 6 — Pre-flight rule engine             ⏳ NEXT (Day 4 target — the entire project)
Phase 7 — FHIR R4 bundle builder
Phase 8 — Dispatch (both NHCX scenarios)
Phase 9 — Resilience and observability
Phase 10 — Demo script and failure drills
```

**Milestone status against Master Build Guide (Appendix C):**
- **Day 1 Exit Criteria MET:** Package master data loaded (1,670 rows, 0.4% unmapped), schema live in Supabase.
- **Day 2 Exit Criteria MET:** Walking skeleton live — POST → gateway → Redis → worker → Supabase Realtime updates.
- **Day 3 Exit Criteria MET:** Semantic mapping + abbreviations + vector search + confidence routing (`>=0.82` auto-accept, `0.45-0.82` human review, `<0.45` action required) + `POST /confirm-code` human-in-the-loop endpoint + Next.js Aarogyamitra portal connected to Supabase Realtime.
- **Day 4 Target (UP NEXT):** Phase 6 — Pre-flight rule engine (`action_required` for missing documents, `POST /cases/{id}/documents` re-enqueue flow).

---

## Environment

Dev machine: Windows, Lenovo LOQ (Ryzen 7 7845HS). Had to enable AMD SVM
virtualization in BIOS + Windows Features (Virtual Machine Platform, WSL)
for Docker Desktop. `uv` for package management. Teammate is on Mac, clones
the same repo, mirrors setup with `uv`.

Database: shared Supabase project (same `DATABASE_URL` for both). Gotcha:
password contains a literal `@` — must be percent-encoded as `%40` in the
connection string, or the connection URL parser mis-splits the host and you
get `getaddrinfo failed` errors. Bit twice by this already — check first if
a fresh DB connection error shows a weird host string.

---

## Phase 0 — Contracts (`src/mediplug/schemas.py`)

Signed off, shared vocabulary across all teammates' work:
- `CaseStatus` enum (11 states)
- `JobEnvelope` (Redis queue shape)
- `IngestRequest` / `IngestResponse` (the HMS ↔ gateway API contract)
- `Requirement` / `RequirementOption` (the `any_of` / `human_label` shape the
  frontend renders)

Current `IngestRequest` shape (confirmed matching teammate's Mock HMS):
```python
class IngestRequest(BaseModel):
    hms_case_ref: str
    stage: Stage
    patient: Patient
    encounter: Encounter
    clinical_notes: str
    documents: list[Document] = Field(default_factory=list)
```

---

## Phase 2 — Database (Supabase)

- `schema.sql` — enums, 4 tables (`packages`, `cases`, `case_documents`,
  `case_events`), `updated_at` trigger, Realtime enabled on `cases`
- `rls.sql` — RLS on all 4 tables, public SELECT policies, writes are
  backend-only via `service_role` (bypasses RLS)
- Both applied and verified on the shared Supabase project

---

## Phase 1 — Package data pipeline (COMPLETE, gate passed)

**Track split:**
- Track A (teammate, Mac): `01_extract_packages.py` (MJPJAY portal export →
  `packages_raw.csv`, 1670 real packages vs guide's estimated 1356 — portal
  is source of truth) + `02_merge_costs.py` (joins a separate cost PDF by
  package code, 978/1670 priced)
- Track B (Kabir): `02_normalize_requirements.py` — the requirement-string
  parser + `TAXONOMY` dict. AND/OR delimiter splitting (`,`/`&` = AND, `/` =
  OR), word-boundary alias matching, punctuation normalization, dedup,
  boilerplate-phrase filtering, plus a **rapidfuzz fuzzy-matching fallback**
  for the long tail of one-off typos (restricted to short phrases to avoid
  false-positive matches on garbled sentences)

**Taxonomy iteration history** (each round = teammate reruns
`03_load_packages.py` on real 1670-row data, reports top unmapped strings,
Kabir extends `TAXONOMY`):

| Round | Unmapped refs | Distinct strings | Notes |
|---|---|---|---|
| 1 | 2705 | 565 | baseline |
| 2 | 1280 | 525 | case-insensitive collapse, typo handling |
| 3 | 1299 | 682 | briefly went up — blunt length-based fix, replaced with word-boundary regex + punctuation normalization + noise-word filtering |
| 4 | 923 | 520 | |
| 5 | 810 | 494 | 14.35% unmapped — still above the 5% gate |
| **Final (rapidfuzz)** | **21** | **14** | **0.39–0.4% unmapped — PASSES the <5% gate** |

**Other Phase 1 files:**
- `03_load_packages.py` (teammate's file, **patched by Kabir**) — was
  originally inserting raw unparsed text into the `requirements` jsonb
  column; patched to call the Track B parser before upserting
- `check_unmapped_pct.py` (Kabir) — queries the live DB directly for the
  real unmapped % (the actual QA gate metric)
- `03_qa_report.py` (Kabir, **rewritten**) — originally expected a
  `data/packages.json` file the pipeline never produced (it loads straight
  to Postgres); rewritten to query the live `packages` table directly via
  `settings.database_url`, same connection pattern as
  `check_unmapped_pct.py`. `--demo` standalone test mode (fake 6-row
  dataset) kept unchanged.

**Final QA report output (real data, both scripts passing):**
```
Total packages:              1670   (gate: >=1300 ✅)
Duplicate codes:              0     ✅
Missing name:                 0     ✅
Zero amount:                  692   (known — needs official rate card, not a blocker, amount is nullable)
Missing ICD:                  801   (likely a genuine gap in the portal source data, not a parser bug)
No preauth reqs:              474   (plausible for simple/day-care procedures; worth a spot-check, not a blocker)
Unmapped doc refs:            0.4%  (gate: <5% ✅)
```

---

## Known open items (not blockers)

1. 692/1670 packages still missing `amount` — needs official rate card
2. Real package count is 1670, not the guide's estimated 1356 — trusting the portal
3. Data pulled from MJPJAY **UAT** server — should confirm same export works on prod
4. 801 packages missing ICD code, 474 with no preauth requirements at all —
   likely genuine gaps/characteristics of the source data, worth a spot-check
   against the portal at some point, not urgent
5. Some unmapped strings are genuinely garbled/truncated source data (e.g.
   "Stickers As", single letters, mid-sentence fragments) — Excel
   column-bleed in the original government file, not fixable via taxonomy
6. `data/packages.json` — the guide's directory structure and night-before
   demo checklist (§12.3) expect this file to exist and be committed.
   Deliberate divergence so far: DB is treated as source of truth instead,
   and `03_qa_report.py` was rewritten to query it directly. Revisit before
   demo day if the guide's checklist item matters for judging/setup.
7. **Files that may still need committing/pushing** — check before starting
   Phase 3: `schema.sql`, `rls.sql`, `01_extract_packages.py`,
   `02_merge_costs.py`, `03_load_packages.py` (patched), final round
   `02_normalize_requirements.py` (with rapidfuzz), `check_unmapped_pct.py`,
   `03_qa_report.py` (DB-query version), `pyproject.toml` (rapidfuzz as an
   explicit dependency, not just implicitly available)

---

## Not yet built

- Pre-flight rule engine (the demo "wow" feature) — Phase 6
- FHIR R4 bundle builder — Phase 7
- Dispatch (mock payer + NHCX sandbox) — Phase 8
- Document upload re-enqueue endpoint (`POST /cases/{id}/documents`)

---

## Working preferences

Casual/direct tone, complete ready-to-use code over partial snippets, walk
through the "why" in plain language, test code before handing it over rather
than writing it blind.

---

## Log

*(New session-update blocks get pasted below this line, most recent last.)*

### Session: 2026-09-06 — Phase 3 + 4 (Walking Skeleton)

**What was built:**

- `src/mediplug/queue.py` — Redis Streams wrapper: lazy async singleton
  (`get_redis()`), idempotent consumer group creation (`ensure_group()`),
  and `enqueue()` via `XADD`. Payload is JSON-encoded into a single
  `"payload"` field on the stream.

- `src/mediplug/gateway/main.py` — FastAPI ingest endpoint:
  `POST /api/v1/cases/ingest`. Validates `IngestRequest` from `schemas.py`,
  checks `Idempotency-Key` header against existing `cases` rows, inserts
  into `cases` + `case_documents` + `case_events`, then XADDs a
  `JobEnvelope` onto the stream. Returns `IngestResponse` (202). If Redis
  is down, the case row is still safe in Postgres (logged, not lost).

- `src/mediplug/worker/consumer.py` — `XREADGROUP` loop with `block=5000ms`.
  Per-message error handling: exponential backoff on failure, dead-letters
  to `mediplug:cases:dlq` after `max_delivery_attempts` (3). Background
  `XAUTOCLAIM` task reclaims stalled messages from crashed consumers.
  **Every exit path (success, retry, DLQ) calls `XACK`** — no messages
  stuck in PENDING forever.

- `src/mediplug/worker/pipeline.py` — Day-1 "dumb" pipeline: transitions
  `queued → analyzing → (3s fake delay) → ready_for_dispatch`. Writes audit
  events to `case_events` on every transition. Uses `structlog.contextvars`
  to bind `case_id` for all log lines.

- `src/mediplug/worker/__main__.py` — entry point:
  `python -m mediplug.worker`

**Bug caught before it bit us:**

The guide's dumb pipeline hardcodes `mapped_package_code = "S4GS2.3"`, but
`cases.mapped_package_code` has a **foreign key** to `packages(code)`. That
code is from the guide's fictional example — it may not exist in our real
1670-row table. If it doesn't → FK violation → every job crashes. Fixed by
not writing `mapped_package_code` at all in the Day-1 pipeline. The column
is nullable, and Phase 5 will write real codes from actual mapping results.

**Runtime fix:**

`redis-py` async client's default socket timeout is shorter than the
`XREADGROUP block=5000ms` call. The worker crashed with
`redis.exceptions.TimeoutError` on the very first idle cycle. Fixed by
setting `socket_timeout=10.0` (> 5s block time) in `get_redis()`.

**End-to-end test results (all passing):**

```
Gateway:       202 returned, case_id + tracking_ref generated
Idempotency:   Same key → same case_id, no duplicate row
Worker logs:   processing_case_start → analyzing → ready_for_dispatch → job_completed
DB (cases):    status = ready_for_dispatch, mapped_package_code = NULL
DB (events):   3 events: →queued (gateway), queued→analyzing (worker), analyzing→ready_for_dispatch (worker)
DB (docs):     1 row: usg_abdomen
Redis stream:  pending = 0 (ACK'd), DLQ = 0
```

**Files that need committing/pushing (carry-forward from Phase 1+2 + new):**
`queue.py`, `gateway/main.py`, `worker/consumer.py`, `worker/pipeline.py`,
`worker/__main__.py`, plus the Phase 1/2 files listed in open item #7.

### Session: 2026-09-06 (cont.) — Gateway merge (yours + teammate's draft)

Both you and teammate independently built gateway/main.py + queue.py for
Phase 3 — second such collision after 03_qa_report.py. Merged rather than
picking one:

- Caught: teammate's queue.py wrote payload under "job" key, mismatched
  worker/consumer.py's expected "payload" key — would have KeyError'd on
  first message if merged as-is. Fixed to "payload".
- Adopted from teammate's draft: AsyncConnectionPool (yours used blocking
  psycopg.connect() inside async routes — stalls the event loop under
  concurrent load), and UniqueViolation handling for the idempotency race
  (yours had a TOCTOU bug: SELECT-then-INSERT with no race guard, would
  502 the losing concurrent request instead of returning the existing row).
- Kept from your draft: blank-key validation, case_events audit detail
  field, graceful Redis-down handling.
- Verified with a live 10-concurrent-request test (same idempotency key):
  1 DB row, 1 Redis enqueue, all 10 responses returned the same case_id.
- Confirmed dependency: fix only works because idempotency_key has a
  UNIQUE constraint on cases — verify this is actually in schema.sql.
- Action item: told teammate to ping before starting gateway/worker/queue
  work going forward, since that's Kabir's ownership scope per the split.

### Session: 2026-09-06 (cont.) — HMS Sync Module

**Context:** Teammate's Mock HMS lives in a separate Supabase project
(`ikdqvwsxknulmlgtypvr`). His DB has no `created_at` on encounters, so
time-based polling doesn't work. Instead, we diff his `encounters` table
against our `cases.hms_case_ref` to find what's new.

**HMS schema analysis (from `Mock HMS/MediPlug_HMS/database/sql/`):**
- 15 tables total, 5 are relevant for sync: `encounters`, `patients`,
  `clinical_notes`, `diagnostic_reports`, `documents`
- `encounter_status` enum: `'Pre-Auth Pending'`, `'Admitted'`, `'Discharged'`
  → maps cleanly to our `stage`: Pre-Auth Pending/Admitted → `"preauth"`,
  Discharged → `"claim"`
- `gender` is free-text `'M'`/`'F'` → normalized to `'male'`/`'female'`/`'other'`
- `date_of_birth` is nullable but `age` exists → synthesized birth_date
- `ration_card_type` ≠ our `ration_card_no` (category vs number) → left unset
- Documents split across TWO tables (`diagnostic_reports` + `documents`) →
  merged into single `documents: list[Document]`
- `doctor_id`/`hospital_id` can be NULL until seed script runs → mapper
  returns None for incomplete rows (skip-and-log, no crash)

**Files built:**
- `src/mediplug/hms_sync/__init__.py`
- `src/mediplug/hms_sync/mapper.py` — pure-logic row-to-IngestRequest mapper
- `src/mediplug/hms_sync/poll.py` — polling sync (diff-based, not time-based)
- `src/mediplug/hms_sync/__main__.py` — entry point: `python -m mediplug.hms_sync`

**Config changes:**
- `config.py` — added `hms_supabase_url`, `hms_supabase_key`
- `.env` — added `HMS_SUPABASE_URL`, `HMS_SUPABASE_KEY` with teammate's creds
- `pyproject.toml` — `supabase` added as dependency via `uv add`

**Idempotency:** deterministic key per encounter_id via `uuid5` — reruns
of the sync script never double-ingest the same encounter.

### Session: 2026-09-06 — Phase 5 (Semantic Code Mapping & Confirm-Code Flow)

**Work split (per `PHASE5_SPLIT.md`):**
- Part A (Shubh, Mac): `CodeCandidate` schema, `corpus.py` (ORDER BY code),
  `abbreviations.py`, `embeddings_cache.py`, `mapper.py` (two-layer: rapidfuzz
  prefilter + sentence-transformers rerank), `06_build_embeddings.py`,
  `07_mapping_eval.py`.
- Part B (Kabir): `ConfirmCodeRequest` schema, `pipeline.py` rewrite with
  confidence routing & `to_thread`, `gateway/main.py` confirm-code endpoint.

**What was built & verified:**
- Embedding cache pre-computed locally via `scripts/06_build_embeddings.py`:
  1,670 packages encoded (dim=384) in `data/cache/package_embeddings.npy`
  with sha256 fingerprint.
- `src/mediplug/schemas.py`: Added `ConfirmCodeRequest(code, confirmed_by)`.
- `src/mediplug/worker/pipeline.py`: Replaced dumb timer with real AI mapping
  via `asyncio.to_thread(map_notes, notes, 3)`.
  - $\ge 0.82 \implies$ `ready_for_dispatch` (writes code, name, confidence, alternates).
  - $0.45 - 0.82 \implies$ `needs_code_confirmation` (writes alternates + confidence, leaves code NULL).
  - $< 0.45 \implies$ `action_required`.
  - Wrapped `alternate_codes` with `psycopg.types.json.Json()` to fix jsonb serialization.
  - Suppressed duplicate audit rows if status unchanged on retries.
- `src/mediplug/gateway/main.py`: Added `POST /api/v1/cases/{case_id}/confirm-code`.
  Validates code against `packages` table, updates case, inserts audit event
  (`actor='aarogyamitra'`), and re-enqueues with `trigger='code_confirmed'`.
- Pipeline skips re-mapping on `trigger == "code_confirmed"` if code is set,
  directly promoting to `ready_for_dispatch`.

**End-to-End Verification:**
- Worker processed the live backlog of 50 hospital cases from HMS:
  12 auto-accepted (`ready_for_dispatch`), 9 required human confirmation
  (`needs_code_confirmation`), 5 required action (`action_required`).
- Tested `POST /confirm-code` on case `e7e4a80a-...`: Aarogyamitra confirmed
  code `S9H5.1`, worker picked up the re-enqueued job, and promoted it to
  `ready_for_dispatch` without re-mapping.
- Comprehensive 4-branch local test suite (`test_phase5_verified.py`) executed:
  - Branch 1: Borderline confidence (0.523) -> correctly routed to `needs_code_confirmation`,
    leaves `mapped_package_code` as NULL, saves 3 alternate candidates.
  - Branch 2: Aarogyamitra confirms code via `POST /confirm-code` -> worker picks up
    re-enqueued job with `trigger="code_confirmed"` and promotes to `ready_for_dispatch`.
  - Branch 3: Low confidence (0.331) -> correctly routed to `action_required` without writing code.
  - All tests passed 100% locally. Code is verified and kept local (not pushed yet).

### Session: 2026-09-06 (cont.) — Full Guide Audit & Automated Test Suite Verification (Phases 0–5)

**Comprehensive Audit against Master Build Guide (`MEDIPLUG_MIDDLEWARE_BUILD_GUIDE.md`):**

- **Phase 0 (Contracts):** `schemas.py` fully verified. Contains all 11 `CaseStatus` enum states, `JobEnvelope`, `IngestRequest`, `IngestResponse`, `Requirement`, `RequirementOption`, `CodeCandidate`, `ConfirmCodeRequest`. Modernized `JobEnvelope.enqueued_at` to use `datetime.now(timezone.utc)` instead of deprecated `datetime.utcnow()`.
- **Phase 1 (Package Master Pipeline):** 1,670 packages loaded in Supabase `packages` master table (0 duplicate codes, 0 missing names, 0.39% unmapped doc refs via rapidfuzz fallback, easily passing the `< 5%` QA gate).
- **Phase 2 (Database Schema):** `schema.sql` and `rls.sql` verified on Supabase. Table structures (`packages`, `cases`, `case_documents`, `case_events`), constraints (unique `tracking_ref`, unique `idempotency_key`), auto-updating timestamps, and Supabase Realtime publication confirmed active.
- **Phase 3 (Ingest Gateway):** `src/mediplug/gateway/main.py` + `queue.py` verified. Idempotency replay and race-condition handling (`UniqueViolation` retry), Postgres insertion, Redis Streams `XADD`, `GET /health`, and FastAPI `CORSMiddleware` (for frontend Aarogyamitra UI access) fully functional.
- **Phase 4 (Worker Engine):** `src/mediplug/worker/consumer.py` verified. `XREADGROUP` consumer loop with block timeout, unique `CONSUMER_NAME`, `XACK` on every exit branch (success, retry, DLQ), exponential backoff, dead-lettering to `mediplug:cases:dlq`, and background `XAUTOCLAIM` for crashed workers.
- **Phase 5 (Semantic Code Mapping & Human Confirmation Flow):**
  - Embedding matrix pre-computed at `data/cache/package_embeddings.npy` (1,670 packages, dim=384, fingerprint verified).
  - 3-layer architecture: abbreviation expansion -> rapidfuzz lexical prefilter -> sentence-transformers semantic reranking.
  - Confidence routing: $\ge 0.82 \implies$ auto-accept, $0.45 - 0.82 \implies$ `needs_code_confirmation` (stores alternates, keeps code `NULL`), $< 0.45 \implies$ `action_required`.
  - Human confirmation endpoint `POST /api/v1/cases/{case_id}/confirm-code` updates case, logs audit event, and re-enqueues with `trigger="code_confirmed"`.
  - Worker pipeline skips re-mapping on confirmed cases.
- **HMS Sync Bridge (Integration):** Diff-based polling module safely ingesting 50 real encounters (`ENC-2026-1001` to `ENC-2026-1050`) from teammate's independent Mock HMS database without modifying his schema.
- **Frontend Portal (Aarogyamitra UI):** Next.js 16 + Tailwind CSS portal in `frontend/` subscribed to Supabase Realtime WebSocket changes, live status badges, metric counters, search/filter, and interactive modal connected to `POST /confirm-code`.

**Accuracy Evaluation Harness (`scripts/07_mapping_eval.py`):**
- Ran 28 benchmark clinical note fixtures:
  - Top-1 Accuracy: **27/28 (96.4%)**
  - Top-3 Accuracy: **27/28 (96.4%)**
  - Dangerous wrong-code auto-accepts: **0**
  - Wasted correct-but-rejected matches: **0**
  - Confirmed 0.82 auto-accept / 0.45 floor thresholds are safe and effective.

**Automated Unit Tests (`tests/`):**
- Built clean pytest suites in `tests/`:
  - `tests/test_contracts.py`: Validates all 11 statuses, envelope defaults, and schemas.
  - `tests/test_normalize.py`: Validates abbreviation expansion (`expand()`), taxonomy mapping (`canonicalize()`), and requirement string parser.
  - `tests/test_mapper.py`: Validates corpus loading, sorted code order, and semantic mapping.
- Executed `pytest tests/`: **11 passed, 0 failures, 0 warnings** in 26.33s.

- **Phase 6: Pre-flight Rule Engine (COMPLETED & VERIFIED):**
  - Implemented pure functional `evaluate()` in `src/mediplug/rules/engine.py` with delimiter/case-insensitive normalization (`'USG_Abdomen'` <-> `'usg_abdomen'`). Zero external dependencies.
  - Added unit test suite in `tests/test_rules.py` covering all 5 criteria (all satisfied, partial unsatisfied, zero-requirement packages, fuzzy normalization, plain strings).
  - Wired rule checking into `src/mediplug/worker/pipeline.py` for both auto-accept mapping and human code confirmation. Short-circuits cases with missing documents to `action_required` (with `missing_requirements` array populated) and advances valid cases to `ready_for_dispatch`.
  - Added `docs_updated` trigger handling to skip semantic mapping and evaluate pre-flight rules directly when documents are added.
  - Added `POST /api/v1/cases/{case_id}/documents` endpoint in `src/mediplug/gateway/main.py` with `UploadDocumentsRequest` in `src/mediplug/schemas.py`.
  - Validated Step 7 & 8 live end-to-end: Ingested case with missing documents -> halted at `action_required` -> uploaded missing files via `/documents` -> worker re-evaluated and flipped status to `ready_for_dispatch`. Tested zero-requirement package drill (sails directly to `ready_for_dispatch`).
  - Full regression test suite: **16/16 passed** (`tests/test_contracts.py`, `tests/test_mapper.py`, `tests/test_normalize.py`, `tests/test_rules.py`).
- **Phase 7: FHIR R4 Bundle Assembly (COMPLETED & VERIFIED):**
  - Implemented `src/mediplug/fhir/builder.py` assembling NRCES-compliant FHIR R4 Bundles.
  - Builds `Claim`, `Patient`, `Coverage`, `Condition` (ICD-10), and `DocumentReference` resources.
  - Passes all 14 FHIR test cases (`tests/test_fhir.py`).

- **Phase 8: Payer Dispatch & Pipeline Finalization (COMPLETED & VERIFIED):**
  - Implemented pluggable dispatchers in `src/mediplug/dispatch/`: `MockDispatcher` and `NhcxDispatcher`.
  - Implemented `src/mediplug/worker/finalize.py` with double-dispatch idempotency guard (`payer_correlation_id`).
  - Added background adjudication poller in `src/mediplug/worker/adjudication.py`.
  - Full test suite passes 20/20 tests (`tests/test_dispatch.py`, `tests/test_finalize.py`, `tests/test_adjudication.py`).

- **Phase 9: Resilience & Observability (COMPLETED & VERIFIED):**
  - Graceful worker shutdown via `src/mediplug/shutdown.py` (finishes in-flight job, flushes Redis, prevents dropped messages).
  - Poison-pill resilience in `src/mediplug/worker/consumer.py`: unparseable payloads are dead-lettered to DLQ without crashing workers.
  - Self-reclaim guard tracking `_inflight` message IDs to eliminate worker race conditions.
  - Admin endpoints in `src/mediplug/gateway/main.py` (`GET /admin/queue`, `POST /admin/cases/{case_id}/requeue`) gated behind `X-Admin-Token`.

- **Phase 10: Demo Driver, Failure Drills & UI Experience (COMPLETED & VERIFIED):**
  - Automated demo script `scripts/10_demo.py` and runner `scripts/run_demo.sh`.
  - 8 automated failure drills in `scripts/11_failure_drills.py`.
  - Aarogyamitra UI updated in `frontend/app/page.tsx` with live queue depth bar (`queue`, `pending`, `dlq`), all 11 status badges, and unmet requirements details.

- **Full Project Status:**
  - **77/77 Unit & Integration Tests Passing (100%)** across Phases 0 through 10.
  - Working tree clean, synced with `origin/main` (Commit `0588bd6`).