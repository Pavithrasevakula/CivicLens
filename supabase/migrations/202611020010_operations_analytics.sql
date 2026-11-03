-- Operational fields and read models for the staff workflow APIs.
alter table public.escalations
  add column if not exists status text not null default 'open'
    check (status in ('open', 'acknowledged', 'in_progress', 'resolved', 'cancelled')),
  add column if not exists sla_hours integer check (sla_hours is null or sla_hours > 0),
  add column if not exists sla_due_at timestamptz,
  add column if not exists acknowledged_at timestamptz,
  add column if not exists updated_at timestamptz not null default now(),
  add column if not exists resolved_by_firebase_uid text,
  add column if not exists created_by_firebase_uid text;

alter table public.emergency_reviews
  add column if not exists assigned_reviewer_firebase_uid text;

create index if not exists escalations_status_sla_idx
  on public.escalations (status, sla_due_at);
create index if not exists financial_records_issue_idx
  on public.financial_records (master_issue_id, created_at);
create index if not exists root_cause_records_issue_idx
  on public.root_cause_records (master_issue_id, updated_at desc);

alter table public.duplicate_decision_history enable row level security;
alter table public.master_issue_merge_history enable row level security;
alter table public.priority_history enable row level security;
alter table public.emergency_reviews enable row level security;
alter table public.root_cause_records enable row level security;
alter table public.escalation_notifications enable row level security;
alter table public.parallel_experiment_results enable row level security;

create policy "staff can manage workflow history"
on public.duplicate_decision_history for all using (public.is_staff()) with check (public.is_staff());
create policy "staff can manage merge history"
on public.master_issue_merge_history for all using (public.is_staff()) with check (public.is_staff());
create policy "staff can manage priority history"
on public.priority_history for all using (public.is_staff()) with check (public.is_staff());
create policy "staff can manage emergency reviews"
on public.emergency_reviews for all using (public.is_staff()) with check (public.is_staff());
create policy "staff can manage root causes"
on public.root_cause_records for all using (public.is_staff()) with check (public.is_staff());
create policy "staff can manage escalation notifications"
on public.escalation_notifications for all using (public.is_staff()) with check (public.is_staff());
create policy "staff can manage experiment results"
on public.parallel_experiment_results for all using (public.is_staff()) with check (public.is_staff());
