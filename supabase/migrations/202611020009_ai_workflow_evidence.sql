-- Evidence and history tables for multimodal scoring and municipal operations.
alter table public.master_issues
  add column if not exists location_sensitivity_score numeric(6, 3) not null default 0
    check (location_sensitivity_score between 0 and 100),
  add column if not exists priority_override_reason text,
  add column if not exists priority_overridden_by uuid references auth.users(id) on delete set null,
  add column if not exists probable_root_cause text,
  add column if not exists verified_root_cause text;

alter table public.duplicate_candidates
  add column if not exists image_model_version text,
  add column if not exists text_model_version text,
  add column if not exists location_distance_meters numeric(12, 3),
  add column if not exists score_weights jsonb not null default '{"image":0.4,"text":0.35,"location":0.25}'::jsonb;

create table if not exists public.duplicate_decision_history (
  id uuid primary key default gen_random_uuid(),
  candidate_id uuid not null references public.duplicate_candidates(id) on delete cascade,
  previous_decision public.duplicate_decision,
  next_decision public.duplicate_decision not null,
  note text,
  changed_by uuid references auth.users(id) on delete set null,
  created_at timestamptz not null default now()
);

create table if not exists public.master_issue_merge_history (
  id uuid primary key default gen_random_uuid(),
  complaint_id uuid not null references public.complaints(id) on delete cascade,
  from_master_issue_id uuid references public.master_issues(id) on delete set null,
  to_master_issue_id uuid references public.master_issues(id) on delete set null,
  action text not null check (action in ('merge', 'unmerge')),
  reason text,
  changed_by uuid references auth.users(id) on delete set null,
  created_at timestamptz not null default now()
);

create table if not exists public.priority_history (
  id uuid primary key default gen_random_uuid(),
  master_issue_id uuid not null references public.master_issues(id) on delete cascade,
  previous_score numeric(6, 3),
  next_score numeric(6, 3) not null,
  previous_priority public.priority_level,
  next_priority public.priority_level not null,
  factors jsonb not null default '{}'::jsonb,
  override_reason text,
  changed_by uuid references auth.users(id) on delete set null,
  created_at timestamptz not null default now()
);

create table if not exists public.emergency_reviews (
  id uuid primary key default gen_random_uuid(),
  complaint_id uuid not null unique references public.complaints(id) on delete cascade,
  severity text not null check (severity in ('critical', 'high', 'medium')),
  review_status text not null default 'pending'
    check (review_status in ('pending', 'verified', 'not_emergency', 'routed')),
  assigned_reviewer uuid references auth.users(id) on delete set null,
  review_note text,
  reviewed_at timestamptz,
  created_at timestamptz not null default now()
);

create table if not exists public.root_cause_records (
  id uuid primary key default gen_random_uuid(),
  master_issue_id uuid not null references public.master_issues(id) on delete cascade,
  probable_cause text,
  verified_cause text,
  recurrence_interval_days integer check (recurrence_interval_days is null or recurrence_interval_days >= 0),
  repair_materials text,
  notes text,
  recorded_by uuid references auth.users(id) on delete set null,
  verified_by uuid references auth.users(id) on delete set null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.escalation_notifications (
  id uuid primary key default gen_random_uuid(),
  escalation_id uuid not null references public.escalations(id) on delete cascade,
  recipient_firebase_uid text not null,
  channel text not null check (channel in ('in_app', 'email')),
  sent_at timestamptz,
  delivered_at timestamptz,
  created_at timestamptz not null default now()
);

create table if not exists public.parallel_experiment_results (
  id uuid primary key default gen_random_uuid(),
  dataset_size integer not null check (dataset_size > 0),
  workers integer not null check (workers > 0),
  elapsed_seconds numeric(14, 6) not null check (elapsed_seconds >= 0),
  throughput_per_second numeric(14, 3) not null check (throughput_per_second >= 0),
  speedup numeric(14, 6) not null check (speedup >= 0),
  efficiency numeric(14, 6) not null check (efficiency >= 0),
  cpu_utilization numeric(6, 3),
  memory_utilization numeric(6, 3),
  pipeline_version text not null,
  created_at timestamptz not null default now()
);

create index if not exists emergency_reviews_status_idx on public.emergency_reviews (review_status, created_at);
create index if not exists priority_history_issue_idx on public.priority_history (master_issue_id, created_at);
create index if not exists merge_history_complaint_idx on public.master_issue_merge_history (complaint_id, created_at);
