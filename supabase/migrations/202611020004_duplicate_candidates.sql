alter table public.duplicate_candidates
  add column if not exists evidence jsonb not null default '{}'::jsonb;

create or replace function public.find_nearby_master_issues(
  p_category text,
  p_latitude double precision,
  p_longitude double precision,
  p_exclude_id uuid default null,
  p_radius_meters double precision default 500
)
returns table (
  id uuid,
  category text,
  description text,
  distance_meters double precision
)
language sql
stable
security definer
set search_path = public
as $$
  select
    m.id,
    m.category,
    m.description,
    st_distance(
      m.centroid,
      st_setsrid(st_makepoint(p_longitude, p_latitude), 4326)::geography
    ) as distance_meters
  from public.master_issues m
  where m.category = p_category
    and (p_exclude_id is null or m.id <> p_exclude_id)
    and st_dwithin(
      m.centroid,
      st_setsrid(st_makepoint(p_longitude, p_latitude), 4326)::geography,
      p_radius_meters
    )
  order by distance_meters asc
  limit 25;
$$;

revoke all on function public.find_nearby_master_issues(
  text,
  double precision,
  double precision,
  uuid,
  double precision
) from public;

grant execute on function public.find_nearby_master_issues(
  text,
  double precision,
  double precision,
  uuid,
  double precision
) to service_role;