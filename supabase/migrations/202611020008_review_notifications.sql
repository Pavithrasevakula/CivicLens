alter table public.firebase_profiles
  add column if not exists department_id uuid references public.departments(id) on delete set null;

alter table public.duplicate_candidates
  add column if not exists reviewed_by_firebase_uid text;

create table if not exists public.notifications (
  id uuid primary key default gen_random_uuid(),
  recipient_firebase_uid text not null,
  complaint_id uuid references public.complaints(id) on delete cascade,
  title text not null,
  body text not null,
  read_at timestamptz,
  created_at timestamptz not null default now()
);

create index if not exists notifications_recipient_idx
  on public.notifications (recipient_firebase_uid, created_at desc);

alter table public.notifications enable row level security;
