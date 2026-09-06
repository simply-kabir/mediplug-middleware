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
Phase 3 — Ingest gateway                     ⏳ NEXT
Phase 4 — Worker skeleton (walking skeleton)
Phase 5 — Semantic code mapping
Phase 6 — Pre-flight rule engine             ← guide calls this "the entire project," never cut
Phase 7 — FHIR R4 bundle builder
Phase 8 — Dispatch (both NHCX scenarios)
Phase 9 — Resilience and observability
Phase 10 — Demo script and failure drills
```

Per the guide's day-by-day schedule, Phase 3+4 together are the **Day 2**
target: "Gateway + queue + dumb worker" in the morning, "Wire Realtime with
teammate" in the afternoon. Exit criteria: **walking skeleton — POST → UI
updates live, nothing intelligent yet.**

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

## Not yet built at all

- Ingest gateway (`src/mediplug/gateway/main.py`)
- Redis Streams queue wrapper (`src/mediplug/queue.py`)
- Worker skeleton + consumer + pipeline orchestration
- Semantic/AI code mapping (clinical notes → package code)
- Pre-flight rule engine (the demo "wow" feature)
- FHIR R4 bundle builder
- Dispatch (mock payer + NHCX sandbox)

---

## Working preferences

Casual/direct tone, complete ready-to-use code over partial snippets, walk
through the "why" in plain language, test code before handing it over rather
than writing it blind.

---

## Log

*(New session-update blocks get pasted below this line, most recent last.)*