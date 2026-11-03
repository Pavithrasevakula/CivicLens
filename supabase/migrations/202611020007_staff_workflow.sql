alter table public.status_history
  add column if not exists changed_by_firebase_uid text;

alter table public.audit_logs
  add column if not exists actor_firebase_uid text;

alter table public.assignments
  add column if not exists assigned_by_firebase_uid text;

alter table public.assignments
  add column if not exists officer_firebase_uid text;

create index if not exists status_history_firebase_actor_idx
  on public.status_history (changed_by_firebase_uid);

create index if not exists audit_logs_firebase_actor_idx
  on public.audit_logs (actor_firebase_uid);
