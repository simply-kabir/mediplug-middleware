# MediPlug — Stretch Goals

> **Rule for this file: nothing here gets touched until the core pipeline
> (Phase 3 → Phase 8 in the build guide) is done and the pre-flight
> rule-engine demo works end to end.** The guide is explicit that the
> pre-flight blocker is "the entire project" and should never be cut for
> anything on this list. These are for spare time only, roughly in priority
> order.

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

*(Add any new stretch ideas here as they come up, most recent last.)*
