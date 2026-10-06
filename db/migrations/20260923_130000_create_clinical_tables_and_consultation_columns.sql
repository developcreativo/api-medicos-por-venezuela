-- Migración: create clinical tables and consultation columns
-- Creada:    2026-09-23 13:00:00
--
-- Crea las tablas clínicas legacy modeladas (prescriptions, referrals, rest_notes,
-- treatment_plans, follow_ups, messages, admin_users) y agrega las columnas faltantes
-- en public.consultations para paridad completa entre el ORM / producción y el schema local.

-- 1) Columnas faltantes en public.consultations
alter table public.consultations
  add column if not exists doctor_id uuid references public.doctors(id) on delete set null,
  add column if not exists clinical_notes text,
  add column if not exists platform_used text,
  add column if not exists meeting_link text,
  add column if not exists doctor_license_snapshot jsonb,
  add column if not exists has_prescription boolean not null default false,
  add column if not exists has_referral boolean not null default false,
  add column if not exists has_rest_note boolean not null default false,
  add column if not exists follow_up_scheduled boolean not null default false,
  add column if not exists started_at timestamptz,
  add column if not exists ended_at timestamptz;

create index if not exists idx_consultations_doctor_id on public.consultations (doctor_id);

-- Secuencia y trigger para generar consultation code
create sequence if not exists public.consultation_seq start 1;

drop trigger if exists trg_generate_consultation_code on public.consultations;
create trigger trg_generate_consultation_code
  before insert on public.consultations
  for each row
  execute function public.generate_consultation_code();

-- 2) Tablas clínicas modeladas
create table if not exists public.prescriptions (
  id              uuid primary key default gen_random_uuid(),
  consultation_id uuid not null references public.consultations(id) on delete cascade,
  medications     text not null,
  instructions    text,
  pdf_url         text,
  issued_at       timestamptz not null default now()
);
create index if not exists idx_prescriptions_consultation_id on public.prescriptions (consultation_id);

create table if not exists public.referrals (
  id              uuid primary key default gen_random_uuid(),
  consultation_id uuid not null references public.consultations(id) on delete cascade,
  referred_to     text not null,
  reason          text not null,
  pdf_url         text,
  issued_at       timestamptz not null default now()
);
create index if not exists idx_referrals_consultation_id on public.referrals (consultation_id);

create table if not exists public.rest_notes (
  id              uuid primary key default gen_random_uuid(),
  consultation_id uuid not null references public.consultations(id) on delete cascade,
  days            integer not null,
  reason          text not null,
  pdf_url         text,
  issued_at       timestamptz not null default now()
);
create index if not exists idx_rest_notes_consultation_id on public.rest_notes (consultation_id);

create table if not exists public.treatment_plans (
  id              uuid primary key default gen_random_uuid(),
  consultation_id uuid not null references public.consultations(id) on delete cascade,
  plan            text not null,
  created_at      timestamptz not null default now()
);
create index if not exists idx_treatment_plans_consultation_id on public.treatment_plans (consultation_id);

create table if not exists public.follow_ups (
  id              uuid primary key default gen_random_uuid(),
  consultation_id uuid not null references public.consultations(id) on delete cascade,
  scheduled_at    timestamptz not null,
  status          text not null default 'pending',
  notes           text,
  created_at      timestamptz not null default now()
);
create index if not exists idx_follow_ups_consultation_id on public.follow_ups (consultation_id);

create table if not exists public.messages (
  id              uuid primary key default gen_random_uuid(),
  consultation_id uuid not null references public.consultations(id) on delete cascade,
  sender_role     text not null,
  body            text not null,
  sent_at         timestamptz not null default now(),
  read_at         timestamptz
);
create index if not exists idx_messages_consultation_id on public.messages (consultation_id);

create table if not exists public.admin_users (
  id            uuid primary key default gen_random_uuid(),
  email         text unique not null,
  password_hash text not null,
  full_name     text not null,
  created_at    timestamptz not null default now()
);

-- 3) Nombres canónicos de especialidades (paridad con producción y tests)
update public.specialties set name = 'Traumatología y ortopedia' where name = 'Traumatología';
update public.specialties set name = 'Pediatría y subespecialidades' where name = 'Pediatría';
update public.specialties set name = 'Cirugía General y Digestivo' where name = 'Cirugía';
update public.specialties set name = 'Fisiatría y rehabilitacion' where name = 'Fisiatría';
