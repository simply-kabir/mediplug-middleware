-- MediPlug — Phase 2 database schema
-- Run this whole file in the Supabase SQL Editor (or via psql).
-- Mirrors src/mediplug/schemas.py exactly — if you ever change the
-- CaseStatus enum there, update it here too.

-- ---------- enums ----------
create type case_status as enum (
  'queued','analyzing','needs_code_confirmation','action_required',
  'ready_for_dispatch','dispatching','submitted',
  'payer_approved','payer_rejected','dispatch_failed','failed'
);

create type case_stage as enum ('preauth','claim');

-- ---------- reference data ----------
create table packages (
  code            text primary key,
  name            text not null,
  speciality      text,
  sub_speciality  text,
  amount          integer,
  icd_code        text,
  requirements    jsonb not null default '{}'::jsonb,
  search_text     text
);
create index on packages using gin (to_tsvector('english', coalesce(search_text,'')));

-- ---------- cases ----------
create table cases (
  id                    uuid primary key default gen_random_uuid(),
  tracking_ref          text unique not null,
  idempotency_key       text unique not null,     -- prevents duplicate claims on HMS retry
  hms_case_ref          text,
  stage                 case_stage not null default 'preauth',
  status                case_status not null default 'queued',

  patient               jsonb not null,
  encounter             jsonb not null,
  raw_clinical_notes    text not null,

  mapped_package_code   text references packages(code),
  mapped_package_name   text,
  confidence            numeric(4,3),
  alternate_codes       jsonb default '[]'::jsonb,
  code_confirmed_by     text,
  code_confirmed_at     timestamptz,

  missing_requirements  jsonb default '[]'::jsonb,

  fhir_bundle           jsonb,
  dispatch_request      jsonb,
  dispatch_response     jsonb,
  payer_correlation_id  text,

  error_message         text,
  attempt_count         integer not null default 0,

  created_at            timestamptz not null default now(),
  updated_at            timestamptz not null default now()
);
create index on cases (status);
create index on cases (created_at desc);

-- ---------- documents ----------
create table case_documents (
  id             uuid primary key default gen_random_uuid(),
  case_id        uuid not null references cases(id) on delete cascade,
  document_type  text not null,          -- must match TAXONOMY codes
  file_url       text not null,
  file_name      text,
  uploaded_at    timestamptz not null default now()
);
create index on case_documents (case_id);

-- ---------- audit trail ----------
create table case_events (
  id          bigserial primary key,
  case_id     uuid not null references cases(id) on delete cascade,
  from_status case_status,
  to_status   case_status,
  actor       text not null default 'system',
  detail      jsonb,
  created_at  timestamptz not null default now()
);
create index on case_events (case_id, created_at);

-- ---------- auto-update updated_at ----------
create or replace function touch_updated_at() returns trigger as $$
begin new.updated_at = now(); return new; end;
$$ language plpgsql;

create trigger cases_touch before update on cases
for each row execute function touch_updated_at();

-- ---------- realtime ----------
-- This is what lets the Aarogyamitra UI update live without polling.
alter publication supabase_realtime add table cases;
