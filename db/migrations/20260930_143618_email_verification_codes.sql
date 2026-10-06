-- Migración: email_verification_codes
-- Creada:    2026-09-30 14:36:18
--
-- Debe ser idempotente (IF NOT EXISTS / ON CONFLICT). El runner la envuelve en
-- una transacción; no uses CREATE INDEX CONCURRENTLY (no corre en transacción).

create table if not exists public.email_verification_codes (
  id uuid primary key default gen_random_uuid(),
  email text not null,                       -- normalizado a minúsculas
  purpose text not null check (purpose in ('patient','doctor')),
  code_hash text not null,                   -- HMAC-SHA256(secreto, "email:purpose:codigo")
  attempts smallint not null default 0,
  sends_count smallint not null default 0,
  window_started_at timestamptz not null default now(),
  last_sent_at timestamptz not null default now(),
  expires_at timestamptz not null,
  consumed_at timestamptz,
  created_at timestamptz not null default now()
);
create unique index if not exists uq_email_verification_email_purpose
  on public.email_verification_codes (email, purpose);
alter table public.email_verification_codes enable row level security;