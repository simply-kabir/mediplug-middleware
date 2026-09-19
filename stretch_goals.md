# MediPlug — Stretch Goals

> **Rule for this file: nothing here gets touched until the core pipeline
> (Phase 3 → Phase 8 in the build guide) is done and the pre-flight
> rule-engine demo works end to end.** The guide is explicit that the
> pre-flight blocker is "the entire project" and should never be cut for
> anything on this list. These are for spare time only, roughly in priority
> order.

---

## 0. Anti-Fraud & Clinical Integrity Engine (Healthcare FWA Prevention)

**The idea:** Protect national healthcare schemes (e.g. PM-JAY / ABDM) from fraudulent, duplicate, and abusive claims before they leave the hospital edge.

### Architecture & Implementation Roadmap:
1. **ABHA Identity & Checksum Validation:**
   - Validate 14-digit numeric ABHA (`XX-XXXX-XXXX-XXXX`) and PHR address format (`user@abdm`). Reject repetitive dummy sequences (`00-0000-0000-0000`).
   - Hook into `gateway/main.py` on `POST /api/v1/cases/ingest` for fast fail-early rejection (`400 Bad Request`).
2. **Simultaneous Active Inpatient Admission Check (Ghost Hospital Admissions):**
   - Query PostgreSQL for concurrent active, un-discharged admissions (`encounter->>'discharge_date' IS NULL`) under the same ABHA across different hospitals.
   - Halt conflicting claims at `action_required` with explicit fraud alerts logged in `case_events` (`actor='anti_fraud_engine'`).
3. **Historical Procedure Restrictions (Anatomical Impossibility + Cooldown Windows):**
   - **Lifetime Single-Excision Registry:** Organs that can never be excised twice (Gallbladder `S8G5.11`, Appendix `S8G4.1`, Uterus `S4G1.1`, Spleen `S8G12.1`).
   - **Procedure Cooldown Windows:** Mandatory minimum intervals between repeatable procedures (e.g. Cataract surgery on same eye $\ge 3\text{ years}$, Stents $\ge 6\text{ months}$).
   - Short-circuit re-claims to `action_required` with remaining days or prior claim references.
4. **Document Integrity & Content-Requirement Matching Layer (Fake / Mismatched Document Prevention):**
   - **The Problem:** Hospital staff uploading dummy files, blank PDFs, billing receipts, or mismatched scans (e.g. labeling an Aadhaar card or blood report as `biopsy_report` or `usg`) to game automated pre-authorization.
   - **Layer Architecture:** Positioned in between document ingestion (`POST /cases/{id}/documents` & `POST /preauth`) and the pre-flight rule engine.
   - **Multi-Level Verification:**
     - *Level 1 (MIME & Format Gatekeeper):* Validates file extensions, MIME types, and minimum byte sizes (e.g., `clinical_photograph` must be JPG/PNG image, `biopsy_report` must be PDF/DICOM, blocks empty 0-byte dummy uploads).
     - *Level 2 (Semantic Clinical Content & Marker Classifier):* Lightweight text-inspection / OCR scanning for mandatory clinical vocabulary matching the declared document category:
       - `usg` (Ultrasound): Requires sonographic markers (*"echogenic"*, *"calculus"*, *"sonography"*, *"acoustic shadow"*, *"gallbladder/appendix visualization"*).
       - `biopsy_report` (Histopathology): Requires pathology markers (*"histopathology"*, *"microscopic examination"*, *"specimen"*, *"malignancy"*).
       - `operative_notes`: Requires surgical markers (*"surgeon"*, *"anesthesia"*, *"incision"*, *"operative findings"*).
       - `aadhaar_card`: Verifies 12-digit UID pattern or Government of India identity markers.
     - *Level 3 (VLM / Multi-modal Vision Classifier):* Uses lightweight vision models to verify clinical photographs depict actual anatomical surgical sites vs. non-clinical photos, downloaded clipart, or random paperwork.
   - **Enforcement & UI Alerting:**
     - Stores `is_verified` (bool) and `rejection_reason` (text) in `case_documents`.
     - Pre-flight rule engine only considers documents with `is_verified = true` towards satisfying package rules.
     - Mismatched documents flag `action_required` with specific human alert: e.g. *"Ultrasound (USG) rejected: Document content does not contain required sonographic findings."*
5. **Validation Suite:**
   - Isolated unit tests in `tests/test_fraud.py` covering format validation, temporal collision, anatomical impossibility, document content matching, and cooldown date math.

---

## 1. Multi-hospital adapter pattern (SIH wow-factor idea)

**The idea:** real hospitals run different EMR systems with different field
naming conventions. Rather than building N full adapters (expensive, and
mostly invisible to judges in a live demo), do the lightweight version:

- Build **one** thin adapter function that translates a differently-shaped
  payload (e.g. the Mock HMS's raw internal format, or a second synthetic
  "Hospital B" format) into the canonical `IngestRequest` shape before
  gateway validation.
- This proves the *pattern* works without needing three fake EMR formats
  built out.
- **Pitch it even if it's barely demoed:** "MediPlug's gateway is
  standards-based — hospitals with different internal systems integrate via
  a thin adapter, not a rewrite." This is already *true* of the
  architecture (contracts + validation happen at the edge), so it costs
  little to say and lands well with judges.
- Pair it with the fact that **dispatch is already payer-agnostic**
  (mock payer vs NHCX sandbox, config swap) — "hospital-agnostic on the way
  in, payer-agnostic on the way out" is a strong, true, cheap talking point.

**Effort:** low (one adapter function) to get most of the credit. Full
multi-adapter build is medium-high effort for mostly invisible demo value —
only worth it if there's genuinely spare time near the end.

---

## 2. Admin queue-depth dashboard

The guide (Phase 9, §11.2) already suggests a `/admin/queue` endpoint
exposing `XPENDING`, `XLEN`, DLQ contents, and consumer group health.
A live queue-depth number on screen during the demo is called out as "a
strong architecture demo" — cheap to build since the Redis commands already
exist, just needs a thin endpoint + a small live-updating UI panel.

---

## 3. Confidence-based routing visibility

Phase 5 (semantic code mapping) will already compute a confidence score for
mapping clinical notes to package codes. If there's time, surface this in
the UI when confidence is borderline — e.g. showing the top-3 alternate
package matches with their scores when a case lands in
`needs_code_confirmation`, rather than just a binary success/fail. Makes
the AI piece feel more transparent and trustworthy to judges, not just a
black box.

---

## 4. Spot-check the 801 missing-ICD / 474 no-preauth-req packages

Not a wow-factor item, but cheap insurance: pick a handful of the 474
packages with zero preauth requirements and manually check them against the
MJPJAY portal, to confirm it's genuine (some day-care procedures really do
need no preauth) rather than a silent data gap. Worth doing once, low
effort, protects against an awkward "wait, why does this package need
nothing?" question during Q&A.

---

## 5. `data/packages.json` export

The build guide's directory structure and demo-night checklist expect this
file to exist and be committed. Current approach queries the DB directly
instead (via `03_qa_report.py` / `check_unmapped_pct.py`). If there's spare
time, add a one-line dump from `03_load_packages.py` after the upsert, just
to fully match the guide's expected artifact — not required for
functionality, just for guide-compliance / in case a judge asks to see the
canonical data file directly.

---

## Log
## 6. Database Webhook instead of polling sync script

Current HMS-sync approach (Step 5 of the integration plan) polls his
Supabase project every few seconds and POSTs new rows to the gateway.
Works, simple to debug, but not truly event-driven.

**Upgrade path if there's time near the end:** use Supabase Database
Webhooks (built on `pg_net`) on his HMS project — a trigger fires
immediately on `INSERT` to the patient/case table and POSTs straight to
the gateway, no polling delay.

**Why this was skipped for the initial build, not just "not gotten to
yet":** webhooks fire from Supabase's own servers on the public internet.
If the gateway is running locally during dev, Supabase literally can't
reach it — needs a persistent public tunnel (ngrok/Cloudflare Tunnel)
running the whole time, one more thing to keep alive through dev and
demo day. Polling avoids that entirely since it's a script you run and
control yourself.

**Worth it if:** the gateway ends up deployed somewhere with a real public
URL anyway (not just localhost) by demo time — then the tunnel problem
disappears and this becomes close to free. Also a genuinely good pitch
line either way: "the sync path can be either near-real-time polling or
fully event-driven via webhook, same mapping logic underneath."

**Effort:** low once the gateway has a stable public URL — mostly just
wiring the webhook in Supabase's dashboard and confirming the payload
shape from `pg_net` maps the same way the polling script already does.

# MediPlug — Future Goals / Deferred Decisions

Tracking items that came up during review but were intentionally deferred
rather than fixed on the spot — either because they're not urgent yet, or
because they need a team decision before implementing. Check this before
building the phase noted for each item.

---

## 1. Split `action_required` into two distinct meanings (deferred from Phase 6)

**Where:** `worker/pipeline.py`

**The situation:** `action_required` currently gets set for two different
reasons that mean very different things to a human reviewer:
- Low-confidence semantic mapping (`confidence < settings.confidence_floor`) —
  the AI couldn't confidently identify a package at all.
- Missing pre-flight documents (`rule_res.passed == False`) — the package is
  known, the case just needs a document uploaded.

**Why it wasn't fixed:** matches the guide's own Phase 5 code path for the
low-confidence case (guide routes this to `failed`, not `action_required`),
and changing it means touching tested Phase 5 logic. Decided to leave as-is
for now rather than risk regressing something that already works.

**How the ambiguity is actually resolved today, without a status change:**
the two cases are already distinguishable by whether `mapped_package_code`
is null. Low-confidence mapping never sets it; missing-docs cases always
have a real code. The frontend can branch on that field instead of the
status enum.

**Revisit when:** Phase 6-8 are otherwise solid and there's time to decide,
as a team, whether a dedicated status (e.g. `docs_required`) is worth the
UI + enum changes, or whether the `mapped_package_code`-null check is good
enough to ship with.

---

## 2. Guard `/documents` endpoint against re-queuing already-dispatched cases (Phase 8 pre-work)

**Where:** `gateway/main.py`, `upload_documents()` handler

**The situation:** the endpoint unconditionally resets `status` to `QUEUED`
and re-enqueues the case, with no check on what the case's status was
before the upload. In Phase 6 this is harmless — nothing downstream of
`ready_for_dispatch` exists yet, so there's no state where re-queuing could
cause real damage.

**Why it matters starting in Phase 8:** once dispatch exists, a case could
already be `submitted` (sent to NHCX). If someone hits `/documents` on a
submitted case, it gets silently pulled back to `QUEUED` and reprocessed.
That might be exactly the amendment flow you want — or it might silently
cause a duplicate/conflicting dispatch. Needs a team decision, not just a
code fix.

**Suggested fix, once the intended behavior is decided:**
```python
# after reading prev_status, before accepting the upload
if prev_status in (CaseStatus.SUBMITTED, CaseStatus.DISPATCHED):  # adjust to actual enum values
    raise HTTPException(
        409,
        f"Cannot add documents to a case in '{prev_status}' status. "
        "Contact support for amendments to already-submitted claims.",
    )
```

**Revisit when:** starting Phase 8 (dispatch), before or alongside building
the actual NHCX submission logic — decide whether post-submission document
uploads should be blocked, allowed with a warning, or trigger a separate
"amendment" trigger type instead of reusing `docs_updated`.

# MediPlug — Future Goals / Deferred Decisions

Tracking items that came up during review but were intentionally deferred
rather than fixed on the spot — either because they're not urgent yet, or
because they need a team decision before implementing. Check this before
building the phase noted for each item.

---

## 1. Split `action_required` into two distinct meanings (deferred from Phase 6)

**Where:** `worker/pipeline.py`

**The situation:** `action_required` currently gets set for two different
reasons that mean very different things to a human reviewer:
- Low-confidence semantic mapping (`confidence < settings.confidence_floor`) —
  the AI couldn't confidently identify a package at all.
- Missing pre-flight documents (`rule_res.passed == False`) — the package is
  known, the case just needs a document uploaded.

**Why it wasn't fixed:** matches the guide's own Phase 5 code path for the
low-confidence case (guide routes this to `failed`, not `action_required`),
and changing it means touching tested Phase 5 logic. Decided to leave as-is
for now rather than risk regressing something that already works.

**How the ambiguity is actually resolved today, without a status change:**
the two cases are already distinguishable by whether `mapped_package_code`
is null. Low-confidence mapping never sets it; missing-docs cases always
have a real code. The frontend can branch on that field instead of the
status enum.

**Revisit when:** Phase 6-8 are otherwise solid and there's time to decide,
as a team, whether a dedicated status (e.g. `docs_required`) is worth the
UI + enum changes, or whether the `mapped_package_code`-null check is good
enough to ship with.

---

## 2. Guard `/documents` endpoint against re-queuing already-dispatched cases (Phase 8 pre-work)

**Where:** `gateway/main.py`, `upload_documents()` handler

**The situation:** the endpoint unconditionally resets `status` to `QUEUED`
and re-enqueues the case, with no check on what the case's status was
before the upload. In Phase 6 this is harmless — nothing downstream of
`ready_for_dispatch` exists yet, so there's no state where re-queuing could
cause real damage.

**Why it matters starting in Phase 8:** once dispatch exists, a case could
already be `submitted` (sent to NHCX). If someone hits `/documents` on a
submitted case, it gets silently pulled back to `QUEUED` and reprocessed.
That might be exactly the amendment flow you want — or it might silently
cause a duplicate/conflicting dispatch. Needs a team decision, not just a
code fix.

**Suggested fix, once the intended behavior is decided:**
```python
# after reading prev_status, before accepting the upload
if prev_status in (CaseStatus.SUBMITTED, CaseStatus.DISPATCHED):  # adjust to actual enum values
    raise HTTPException(
        409,
        f"Cannot add documents to a case in '{prev_status}' status. "
        "Contact support for amendments to already-submitted claims.",
    )
```

**Revisit when:** starting Phase 8 (dispatch), before or alongside building
the actual NHCX submission logic — decide whether post-submission document
uploads should be blocked, allowed with a warning, or trigger a separate
"amendment" trigger type instead of reusing `docs_updated`.

---

## 3. Sequential worker loop can stall unrelated jobs behind a retrying one (deferred from Phase 6)

**Where:** `worker/consumer.py`, `run()`

**The situation:** the main loop reads one message via `xreadgroup(count=1)`
and fully `await`s `_handle()` before reading the next message. If a job
fails and enters the backoff path, the entire worker sits idle for up to
`min(2**attempt, 30)` seconds on that one job — every other case in the
stream, including an unrelated one, waits behind it.

**Why it wasn't fixed now:** the real fix (bounded concurrency via
`asyncio.create_task()` + a semaphore, so a backing-off job can't block the
read loop) is untested new code — new concurrent-DB-connection behavior,
new config value, new failure mode to verify (task-based exceptions must
still surface as `job_failed`/`job_dead_lettered`, not vanish silently).
Not worth introducing right before a hackathon deadline when the current
sequential version is simple, already tested, and easy to debug.

**Mitigate operationally until then:** before any live demo, clear the
Redis stream and DLQ of stale/failing test jobs so nothing is mid-backoff
when the actual demo case runs. Test the exact demo script once, right
before presenting, against a clean queue.

**The fix, when there's time to test it properly (post-hackathon):**
```python
sem = asyncio.Semaphore(settings.max_concurrent_jobs)  # e.g. 5

async def _handle_bounded(r: Redis, msg_id: str, raw: dict) -> None:
    async with sem:
        await _handle(r, msg_id, raw)

# in run()'s loop, replace `await _handle(r, msg_id, raw)` with:
asyncio.create_task(_handle_bounded(r, msg_id, raw))
```

**Revisit when:** after the hackathon deadline, or if in practice a demo
run actually does get visibly stalled by a stuck job — whichever comes
first. Test by deliberately forcing one job to fail/retry (bad `case_id`,
or a temporarily wrong `DATABASE_URL`) and confirming other jobs still
process promptly instead of queuing behind it.

# MediPlug — Future Goals / Deferred Decisions

Tracking items that came up during review but were intentionally deferred
rather than fixed on the spot — either because they're not urgent yet, or
because they need a team decision before implementing. Check this before
building the phase noted for each item.

---

## 1. Split `action_required` into two distinct meanings (deferred from Phase 6)

**Where:** `worker/pipeline.py`

**The situation:** `action_required` currently gets set for two different
reasons that mean very different things to a human reviewer:
- Low-confidence semantic mapping (`confidence < settings.confidence_floor`) —
  the AI couldn't confidently identify a package at all.
- Missing pre-flight documents (`rule_res.passed == False`) — the package is
  known, the case just needs a document uploaded.

**Why it wasn't fixed:** matches the guide's own Phase 5 code path for the
low-confidence case (guide routes this to `failed`, not `action_required`),
and changing it means touching tested Phase 5 logic. Decided to leave as-is
for now rather than risk regressing something that already works.

**How the ambiguity is actually resolved today, without a status change:**
the two cases are already distinguishable by whether `mapped_package_code`
is null. Low-confidence mapping never sets it; missing-docs cases always
have a real code. The frontend can branch on that field instead of the
status enum.

**Revisit when:** Phase 6-8 are otherwise solid and there's time to decide,
as a team, whether a dedicated status (e.g. `docs_required`) is worth the
UI + enum changes, or whether the `mapped_package_code`-null check is good
enough to ship with.

---

## 2. Guard `/documents` endpoint against re-queuing already-dispatched cases (Phase 8 pre-work)

**Where:** `gateway/main.py`, `upload_documents()` handler

**The situation:** the endpoint unconditionally resets `status` to `QUEUED`
and re-enqueues the case, with no check on what the case's status was
before the upload. In Phase 6 this is harmless — nothing downstream of
`ready_for_dispatch` exists yet, so there's no state where re-queuing could
cause real damage.

**Why it matters starting in Phase 8:** once dispatch exists, a case could
already be `submitted` (sent to NHCX). If someone hits `/documents` on a
submitted case, it gets silently pulled back to `QUEUED` and reprocessed.
That might be exactly the amendment flow you want — or it might silently
cause a duplicate/conflicting dispatch. Needs a team decision, not just a
code fix.

**Suggested fix, once the intended behavior is decided:**
```python
# after reading prev_status, before accepting the upload
if prev_status in (CaseStatus.SUBMITTED, CaseStatus.DISPATCHED):  # adjust to actual enum values
    raise HTTPException(
        409,
        f"Cannot add documents to a case in '{prev_status}' status. "
        "Contact support for amendments to already-submitted claims.",
    )
```

**Revisit when:** starting Phase 8 (dispatch), before or alongside building
the actual NHCX submission logic — decide whether post-submission document
uploads should be blocked, allowed with a warning, or trigger a separate
"amendment" trigger type instead of reusing `docs_updated`.

---

## 3. Sequential worker loop can stall unrelated jobs behind a retrying one (deferred from Phase 6)

**Where:** `worker/consumer.py`, `run()`

**The situation:** the main loop reads one message via `xreadgroup(count=1)`
and fully `await`s `_handle()` before reading the next message. If a job
fails and enters the backoff path, the entire worker sits idle for up to
`min(2**attempt, 30)` seconds on that one job — every other case in the
stream, including an unrelated one, waits behind it.

**Why it wasn't fixed now:** the real fix (bounded concurrency via
`asyncio.create_task()` + a semaphore, so a backing-off job can't block the
read loop) is untested new code — new concurrent-DB-connection behavior,
new config value, new failure mode to verify (task-based exceptions must
still surface as `job_failed`/`job_dead_lettered`, not vanish silently).
Not worth introducing right before a hackathon deadline when the current
sequential version is simple, already tested, and easy to debug.

**Mitigate operationally until then:** before any live demo, clear the
Redis stream and DLQ of stale/failing test jobs so nothing is mid-backoff
when the actual demo case runs. Test the exact demo script once, right
before presenting, against a clean queue.

**The fix, when there's time to test it properly (post-hackathon):**
```python
sem = asyncio.Semaphore(settings.max_concurrent_jobs)  # e.g. 5

async def _handle_bounded(r: Redis, msg_id: str, raw: dict) -> None:
    async with sem:
        await _handle(r, msg_id, raw)

# in run()'s loop, replace `await _handle(r, msg_id, raw)` with:
asyncio.create_task(_handle_bounded(r, msg_id, raw))
```

**Revisit when:** after the hackathon deadline, or if in practice a demo
run actually does get visibly stalled by a stuck job — whichever comes
first. Test by deliberately forcing one job to fail/retry (bad `case_id`,
or a temporarily wrong `DATABASE_URL`) and confirming other jobs still
process promptly instead of queuing behind it.

---

## 4. In-flight dispatch crash window not covered by the duplicate-dispatch guard (Phase 8/9)

**Where:** `worker/finalize.py`, `finalize_case()`

**The situation:** the Phase 9.4 duplicate-dispatch guard (`payer_correlation_id IS NOT NULL`)
correctly protects the window *after* `_persist_dispatch` has written a
correlation id — a retried job short-circuits instead of re-submitting.
It does **not** protect the window *during* `await dispatcher.submit(...)`
itself. If the worker crashes or the network partitions while that call is
in flight, and the payer actually received and processed the request
before the crash, `payer_correlation_id` is still `None` in our DB. A
retry then finds no correlation id, proceeds past the guard, and genuinely
re-submits the same claim to the payer a second time.

**Why it's not fixed:** this isn't fixable from our side alone — it's the
classic at-least-once-delivery duplicate-side-effect problem, and closing
it needs the payer to accept an idempotency key on submission (e.g. our
`case_id` or `job_id` as a client-supplied dedup key), so a genuine retry
of an already-processed submission is recognized and returned as the same
`correlation_id` instead of creating a second claim. Neither the guide's
reference mock payer nor our `mock.py` currently supports this.

**Revisit when:** if a real NHCX sandbox lands (Phase 8 Scenario B) — check
whether the real protocol supports a submission-level idempotency key
before dispatch volume matters. For the mock payer, worth adding as a
teaching moment for the pitch even if not fixed: "our guard covers the
crash-after-ack window; true exactly-once submission needs payer-side
idempotency support, which is a protocol question, not just an
application one."

*(Add any new stretch ideas here as they come up, most recent last.)*
