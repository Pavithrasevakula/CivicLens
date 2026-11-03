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
where status not in ('submitted', 'under_verification', 'rejected');
