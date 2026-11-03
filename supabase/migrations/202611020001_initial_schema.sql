create extension if not exists pgcrypto;
create extension if not exists postgis;
create extension if not exists vector;

create type public.app_role as enum (
  'citizen',
  'officer',
  'supervisor',
  'administrator',
  'auditor'
);

create type public.complaint_status as enum (
  'submitted',
  'under_verification',
  'verified',
  'assigned',
  'in_progress',
  'resolved',
  'closed',
  'reopened',
  'rejected'
);

create type public.priority_level as enum ('critical', 'high', 'medium', 'low');
create type public.duplicate_decision as enum ('duplicate', 'potential', 'new_issue', 'rejected');

create table public.profiles (
  user_id uuid primary key references auth.users(id) on delete cascade,
  role public.app_role not null default 'citizen',
  display_name text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table public.departments (
  id uuid primary key default gen_random_uuid(),
  name text not null unique,
  description text,
  active boolean not null default true,
  created_at timestamptz not null default now()
);

create table public.complaints (
  id uuid primary key default gen_random_uuid(),
  public_reference text not null unique default upper('CIV-' || encode(gen_random_bytes(8), 'hex')),
  tracking_token_hash text not null unique,
  reporter_user_id uuid references auth.users(id) on delete set null,
  category text not null check (length(category) between 2 and 80),
  description text not null check (length(description) between 10 and 5000),
  location geography(point, 4326) not null,
  location_label text check (location_label is null or length(location_label) <= 300),
  is_anonymous boolean not null default true,
  status public.complaint_status not null default 'submitted',
  emergency_flag boolean not null default false,
  emergency_reviewed boolean not null default false,
  submitted_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint complaints_anonymous_identity_check check (
    is_anonymous or reporter_user_id is not null
  )
);

create index complaints_location_idx on public.complaints using gist (location);
create index complaints_status_idx on public.complaints (status);
create index complaints_category_idx on public.complaints (category);
create index complaints_reporter_idx on public.complaints (reporter_user_id);

create table public.complaint_images (
  id uuid primary key default gen_random_uuid(),
  complaint_id uuid not null references public.complaints(id) on delete cascade,
  storage_path text not null unique,
  content_type text not null check (content_type in ('image/jpeg', 'image/png', 'image/webp')),
  byte_size integer not null check (byte_size between 1 and 10485760),
  sha256 text not null,
  image_embedding vector(512),
  malware_scan_status text not null default 'pending'
    check (malware_scan_status in ('pending', 'clean', 'rejected')),
  created_at timestamptz not null default now()
);

create index complaint_images_complaint_idx on public.complaint_images (complaint_id);

create table public.master_issues (
  id uuid primary key default gen_random_uuid(),
  public_reference text not null unique default upper('ISS-' || encode(gen_random_bytes(8), 'hex')),
  category text not null,
  title text not null check (length(title) between 3 and 200),
  description text not null,
  centroid geography(point, 4326) not null,
  priority public.priority_level not null default 'low',
  priority_score numeric(6, 3) not null default 0 check (priority_score between 0 and 100),
  severity_score numeric(6, 3) not null default 0 check (severity_score between 0 and 100),
  impact_score numeric(6, 3) not null default 0 check (impact_score between 0 and 100),
  recurrence_score numeric(6, 3) not null default 0 check (recurrence_score between 0 and 100),
  status public.complaint_status not null default 'under_verification',
  assigned_department_id uuid references public.departments(id) on delete set null,
  due_at timestamptz,
  verified_at timestamptz,
  resolved_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index master_issues_centroid_idx on public.master_issues using gist (centroid);
create index master_issues_priority_idx on public.master_issues (priority, status);

create table public.complaint_master_links (
  complaint_id uuid primary key references public.complaints(id) on delete cascade,
  master_issue_id uuid not null references public.master_issues(id) on delete restrict,
  linked_by uuid references auth.users(id) on delete set null,
  decision public.duplicate_decision not null,
  confidence numeric(6, 5) check (confidence between 0 and 1),
  evidence jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create index complaint_master_links_master_idx on public.complaint_master_links (master_issue_id);

create table public.duplicate_candidates (
  id uuid primary key default gen_random_uuid(),
  complaint_id uuid not null references public.complaints(id) on delete cascade,
  candidate_master_issue_id uuid not null references public.master_issues(id) on delete cascade,
  image_similarity numeric(6, 5) check (image_similarity between 0 and 1),
  text_similarity numeric(6, 5) check (text_similarity between 0 and 1),
  location_similarity numeric(6, 5) check (location_similarity between 0 and 1),
  combined_score numeric(6, 5) check (combined_score between 0 and 1),
  decision public.duplicate_decision not null default 'potential',
  reviewed_by uuid references auth.users(id) on delete set null,
  reviewed_at timestamptz,
  created_at timestamptz not null default now(),
  unique (complaint_id, candidate_master_issue_id)
);

create table public.status_history (
  id uuid primary key default gen_random_uuid(),
  complaint_id uuid references public.complaints(id) on delete cascade,
  master_issue_id uuid references public.master_issues(id) on delete cascade,
  from_status public.complaint_status,
  to_status public.complaint_status not null,
  note text,
  changed_by uuid references auth.users(id) on delete set null,
  created_at timestamptz not null default now(),
  constraint status_history_target_check check (
    (complaint_id is not null) <> (master_issue_id is not null)
  )
);

create table public.assignments (
  id uuid primary key default gen_random_uuid(),
  master_issue_id uuid not null references public.master_issues(id) on delete cascade,
  department_id uuid not null references public.departments(id) on delete restrict,
  officer_user_id uuid references auth.users(id) on delete set null,
  assigned_by uuid references auth.users(id) on delete set null,
  assigned_at timestamptz not null default now(),
  unassigned_at timestamptz
);

create table public.escalations (
  id uuid primary key default gen_random_uuid(),
  master_issue_id uuid not null references public.master_issues(id) on delete cascade,
  level integer not null check (level between 1 and 5),
  reason text not null,
  authority text not null,
  due_at timestamptz,
  escalated_at timestamptz not null default now(),
  resolved_at timestamptz,
  created_by uuid references auth.users(id) on delete set null
);

create table public.resolution_records (
  id uuid primary key default gen_random_uuid(),
  master_issue_id uuid not null references public.master_issues(id) on delete cascade,
  action text not null check (length(action) between 3 and 5000),
  evidence_paths text[] not null default '{}',
  contractor text,
  notes text,
  recorded_by uuid references auth.users(id) on delete set null,
  recorded_at timestamptz not null default now()
);

create table public.financial_records (
  id uuid primary key default gen_random_uuid(),
  master_issue_id uuid not null references public.master_issues(id) on delete cascade,
  expense_category text not null,
  amount numeric(12, 2) not null check (amount >= 0),
  currency char(3) not null default 'INR',
  description text,
  document_path text,
  recorded_by uuid references auth.users(id) on delete set null,
  created_at timestamptz not null default now()
);

create table public.audit_logs (
  id uuid primary key default gen_random_uuid(),
  actor_user_id uuid references auth.users(id) on delete set null,
  action text not null,
  entity_type text not null,
  entity_id uuid,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create index audit_logs_created_idx on public.audit_logs (created_at desc);

create or replace view public.public_master_issues as
select
  id,
  public_reference,
  category,
  title,
  st_asgeojson(st_snaptogrid(centroid::geometry, 0.01))::jsonb as approximate_location,
  priority,
  status,
  (select count(*) from public.complaint_master_links l where l.master_issue_id = m.id) as report_count,
  (select count(*) from public.resolution_records r where r.master_issue_id = m.id) as action_count,
  created_at,
  updated_at,
  resolved_at
from public.master_issues m
where status not in ('rejected');

create or replace function public.is_staff()
returns boolean
language sql
stable
security definer
set search_path = public
as $$
  select exists (
    select 1 from public.profiles
    where user_id = auth.uid()
      and role in ('officer', 'supervisor', 'administrator', 'auditor')
  );
$$;

alter table public.profiles enable row level security;
alter table public.departments enable row level security;
alter table public.complaints enable row level security;
alter table public.complaint_images enable row level security;
alter table public.master_issues enable row level security;
alter table public.complaint_master_links enable row level security;
alter table public.duplicate_candidates enable row level security;
alter table public.status_history enable row level security;
alter table public.assignments enable row level security;
alter table public.escalations enable row level security;
alter table public.resolution_records enable row level security;
alter table public.financial_records enable row level security;
alter table public.audit_logs enable row level security;

create policy "users can read their profile"
on public.profiles for select
using (user_id = auth.uid() or public.is_staff());

create policy "staff can read departments"
on public.departments for select
using (active or public.is_staff());

create policy "citizens can create complaints"
on public.complaints for insert
with check (
  (reporter_user_id is null and is_anonymous)
  or reporter_user_id = auth.uid()
);

create policy "citizens can read their complaints"
on public.complaints for select
using (reporter_user_id = auth.uid() or public.is_staff());

create policy "staff can manage complaints"
on public.complaints for update
using (public.is_staff())
with check (public.is_staff());

create policy "staff can read public issue records"
on public.master_issues for select
using (public.is_staff());

create policy "staff can manage issue records"
on public.master_issues for all
using (public.is_staff())
with check (public.is_staff());

grant select on public.public_master_issues to anon, authenticated;

create policy "staff can manage linked records"
on public.complaint_master_links for all
using (public.is_staff())
with check (public.is_staff());

create policy "staff can manage processing records"
on public.duplicate_candidates for all
using (public.is_staff())
with check (public.is_staff());

create policy "citizens can read own status history"
on public.status_history for select
using (
  public.is_staff()
  or exists (
    select 1 from public.complaints c
    where c.id = complaint_id and c.reporter_user_id = auth.uid()
  )
);

create policy "staff can manage operational records"
on public.status_history for all
using (public.is_staff())
with check (public.is_staff());

create policy "staff can manage assignments"
on public.assignments for all
using (public.is_staff())
with check (public.is_staff());

create policy "staff can manage escalations"
on public.escalations for all
using (public.is_staff())
with check (public.is_staff());

create policy "staff can manage resolutions"
on public.resolution_records for all
using (public.is_staff())
with check (public.is_staff());

create policy "public can read financial summaries"
on public.financial_records for select
using (public.is_staff());

create policy "staff can manage financial records"
on public.financial_records for all
using (public.is_staff())
with check (public.is_staff());

create policy "staff can read audit logs"
on public.audit_logs for select
using (public.is_staff());

create policy "staff can create audit logs"
on public.audit_logs for insert
with check (public.is_staff());
