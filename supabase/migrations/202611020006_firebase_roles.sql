create table if not exists public.firebase_profiles (
  firebase_uid text primary key,
  role public.app_role not null default 'citizen',
  display_name text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

alter table public.complaints
  add column if not exists reporter_firebase_uid text;

create index if not exists complaints_reporter_firebase_idx
  on public.complaints (reporter_firebase_uid);

alter table public.firebase_profiles enable row level security;

comment on table public.firebase_profiles is
  'Firebase Auth identities and CivicLens roles resolved by the backend.';

comment on column public.complaints.reporter_firebase_uid is
  'Firebase Auth UID of the authenticated citizen who submitted the complaint.';
