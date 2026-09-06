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
Phase 5 — Semantic code mapping              ⏳ NEXT
Phase 6 — Pre-flight rule engine             ← guide calls this "the entire project," never cut
Phase 7 — FHIR R4 bundle builder
Phase 8 — Dispatch (both NHCX scenarios)
Phase 9 — Resilience and observability
Phase 10 — Demo script and failure drills
```

Phase 3+4 together were the **Day 2** target. Exit criteria met:
**walking skeleton — POST → worker processes → Supabase row updates live,
nothing intelligent yet.**

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

- Semantic/AI code mapping (clinical notes → package code)
- Pre-flight rule engine (the demo "wow" feature)
- FHIR R4 bundle builder
- Dispatch (mock payer + NHCX sandbox)
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