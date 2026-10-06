-- Migración: mensajeria hilos
-- Creada:    2026-10-05 08:48:00
--
-- Amplía la tabla messages (R1), crea message_attachments cifrada (R1, R15)
-- y siembra los permisos RBAC messages.read y messages.write (R12).

-- 1) Ampliación de public.messages
alter table public.messages
  alter column body drop not null,
  add column if not exists sender_user_id uuid references public.users(id) on delete set null,
  add column if not exists direction text,
  add column if not exists channel text not null default 'web',
  add column if not exists kind text not null default 'text',
  add column if not exists call_session_id uuid,
  add column if not exists client_msg_id text,
  add column if not exists delivered_at timestamptz,
  add column if not exists delivery_status text not null default 'sent',
  add column if not exists wa_message_id text,
  add column if not exists error_code text;

update public.messages
set direction = case
  when sender_role = 'patient' then 'patient_to_doctor'
  else 'doctor_to_patient'
end
where direction is null;

alter table public.messages alter column direction set not null;

-- Constraints en messages
alter table public.messages drop constraint if exists messages_sender_role_check;
alter table public.messages add constraint messages_sender_role_check
  check (sender_role in ('doctor', 'patient', 'system'));

alter table public.messages drop constraint if exists messages_direction_check;
alter table public.messages add constraint messages_direction_check
  check (direction in ('doctor_to_patient', 'patient_to_doctor', 'system'));

alter table public.messages drop constraint if exists messages_channel_check;
alter table public.messages add constraint messages_channel_check
  check (channel in ('web', 'whatsapp'));

alter table public.messages drop constraint if exists messages_kind_check;
alter table public.messages add constraint messages_kind_check
  check (kind in ('text', 'attachment', 'call'));

alter table public.messages drop constraint if exists messages_delivery_status_check;
alter table public.messages add constraint messages_delivery_status_check
  check (delivery_status in ('sent', 'delivered', 'read', 'failed'));

-- Índices en messages
create index if not exists idx_messages_consultation_sent_id
  on public.messages (consultation_id, sent_at, id);

create unique index if not exists uq_messages_consultation_client_msg
  on public.messages (consultation_id, client_msg_id)
  where client_msg_id is not null;

create index if not exists idx_messages_consultation_unread
  on public.messages (consultation_id)
  where read_at is null;

create unique index if not exists uq_messages_wa_message_id
  on public.messages (wa_message_id)
  where wa_message_id is not null;

-- 2) Columna patient_last_seen_at en consultations
alter table public.consultations
  add column if not exists patient_last_seen_at timestamptz;

-- 3) Tabla message_attachments
create table if not exists public.message_attachments (
  id               uuid primary key default gen_random_uuid(),
  message_id       uuid references public.messages(id) on delete cascade,
  consultation_id  uuid not null references public.consultations(id) on delete cascade,
  uploader_role    text not null check (uploader_role in ('doctor', 'patient')),
  uploader_user_id uuid references public.users(id) on delete set null,
  file_name        text not null,
  mime_type        text not null check (mime_type in ('application/pdf', 'image/jpeg', 'image/png', 'image/webp')),
  file_size_bytes  integer not null check (file_size_bytes > 0 and file_size_bytes <= 10485760),
  storage_path     text not null,
  created_at       timestamptz not null default now()
);

alter table public.message_attachments drop constraint if exists message_attachments_file_name_cifrado;
alter table public.message_attachments add constraint message_attachments_file_name_cifrado
  check (file_name like 'enc:v1:%');

create index if not exists idx_message_attachments_message_id
  on public.message_attachments (message_id);

create index if not exists idx_message_attachments_consultation_created
  on public.message_attachments (consultation_id, created_at desc);

alter table public.message_attachments enable row level security;

do $$
declare
  r text;
begin
  foreach r in array array['anon', 'authenticated'] loop
    if exists (select 1 from pg_roles where rolname = r) then
      execute format('revoke all on public.message_attachments from %I', r);
    end if;
  end loop;
end $$;

-- 4) Permisos RBAC
insert into public.permissions (code, description)
select v.code, v.description
from (values
    ('messages.read',
     'Ver el buzón y listar los mensajes de consultas asignadas o administradas'),
    ('messages.write',
     'Enviar mensajes y subir archivos adjuntos en el hilo de una consulta')
) as v (code, description)
where not exists (select 1 from public.permissions p where p.code = v.code);

insert into public.role_permissions (role_id, permission_id)
select r.id, p.id
from (values
    ('doctor',      'messages.read'),
    ('doctor',      'messages.write'),
    ('admin',       'messages.read'),
    ('admin',       'messages.write'),
    ('super_admin', 'messages.read'),
    ('super_admin', 'messages.write')
) as m (role_code, perm_code)
join public.roles r on r.code = m.role_code and r.deleted_at is null
join public.permissions p on p.code = m.perm_code
where not exists (
    select 1 from public.role_permissions rp
    where rp.role_id = r.id and rp.permission_id = p.id
);
