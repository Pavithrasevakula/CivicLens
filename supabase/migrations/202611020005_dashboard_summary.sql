create or replace view public.public_dashboard_summary as
select
  (select count(*) from public.master_issues where status not in ('resolved', 'closed', 'rejected'))::integer as open_master_issues,
  (select count(*) from public.complaint_master_links where decision = 'duplicate')::integer as duplicate_reports,
  (select count(*) from public.master_issues where priority = 'critical' and status not in ('resolved', 'closed', 'rejected'))::integer as critical_issues,
  (select count(*) from public.master_issues where status in ('resolved', 'closed'))::integer as resolved_issues;

grant select on public.public_dashboard_summary to anon, authenticated;
