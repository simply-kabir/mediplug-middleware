-- MediPlug — Row Level Security
-- Run this AFTER schema.sql. Fixes the "RLS disabled" warning while keeping
-- Realtime working for the frontend (anon key needs read access to `cases`
-- for live updates) and keeping all writes backend-only (gateway/worker use
-- service_role via DATABASE_URL, which always bypasses RLS regardless of
-- these policies).

alter table packages enable row level security;
alter table cases enable row level security;
alter table case_documents enable row level security;
alter table case_events enable row level security;

-- Anyone (anon or logged-in) can read package reference data — it's public
-- government data anyway, nothing sensitive in it.
create policy "packages are publicly readable"
  on packages for select
  using (true);

-- Frontend needs to read case status for the dashboard + Realtime updates.
-- No insert/update/delete policy exists for anon, so writes are impossible
-- through the public API — only your backend (via service_role, bypassing
-- RLS) can write.
create policy "cases are publicly readable"
  on cases for select
  using (true);

create policy "case_documents are publicly readable"
  on case_documents for select
  using (true);

create policy "case_events are publicly readable"
  on case_events for select
  using (true);
