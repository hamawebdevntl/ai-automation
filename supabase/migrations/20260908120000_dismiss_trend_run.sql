-- =============================================================================
-- Removing a search from the list.
-- =============================================================================
--
-- A described run stays visible in two places after it ends: the recent
-- searches under the box, and -- while it is the latest run -- the notice
-- above the queue. That is right for a run worth going back to and wrong for
-- one that is not: a search made by mistake, a description that was misread,
-- or the run a pre-feature worker claimed and scouted as the saved list. Its
-- notice cannot change (a finished run is a fact) and until now nothing could
-- move it out of the way except a newer run.
--
-- So a run can be dismissed. Not deleted: the row is the record of what was
-- asked and what came back, the ideas it drafted point at it, and a breakdown
-- that vanishes with its run is a breakdown nobody can learn from. Dismissing
-- sets a timestamp the app filters on, and nothing else about the run changes
-- -- except that a run still going is stopped first, because "remove" pressed
-- on a search that is running means "and stop spending on it", and a hidden
-- run that keeps scouting would be the worst of both.
-- =============================================================================

alter table public.trend_runs
  add column if not exists dismissed_at timestamptz,
  add column if not exists dismissed_by uuid references public.profiles (id) on delete set null;

comment on column public.trend_runs.dismissed_at is
  'When an owner removed this run from the app''s lists. The row stays; the app stops showing it.';

-- The app's two run lists both read "newest first, not dismissed".
create index if not exists trend_runs_visible_idx
  on public.trend_runs (requested_at desc)
  where dismissed_at is null;


-- ---------------------------------------------------------------------------
-- Removing a run.
-- ---------------------------------------------------------------------------
--
-- A function rather than an UPDATE policy, for the reasons `cancel_trend_run`
-- is one: it is the only place that can check the role, stop a run that is
-- still going in the same statement, and turn "already removed" into a
-- sentence rather than a silent update matching zero rows.
create or replace function public.dismiss_trend_run(p_run_id uuid)
returns public.trend_runs
language plpgsql
security definer
set search_path = public
as $$
declare
  v_run public.trend_runs;
begin
  if not public.is_owner() then
    raise exception 'Only an owner may remove a trend run' using errcode = '42501';
  end if;

  -- One statement, so a run that is stopped and hidden is stopped and hidden
  -- together. The in-flight columns are written exactly as `cancel_trend_run`
  -- writes them: the scout notices it has been stopped the same way, and the
  -- single in-flight lock frees itself the same way. Every `status` on the
  -- right-hand side is the row's status before this update.
  update public.trend_runs
     set dismissed_at = now(),
         dismissed_by = auth.uid(),
         cancelled_at = case when status in ('requested', 'running') then now() else cancelled_at end,
         cancelled_by = case when status in ('requested', 'running') then auth.uid() else cancelled_by end,
         finished_at  = coalesce(finished_at, now()),
         status       = case when status in ('requested', 'running') then 'cancelled' else status end
   where id = p_run_id
     and dismissed_at is null
  returning * into v_run;

  if not found then
    -- Already removed, or never existed. Not an error worth a stack trace: the
    -- usual cause is two tabs, or a list that has since moved on.
    raise exception 'That search has already been removed'
      using errcode = 'P0002';
  end if;

  return v_run;
end;
$$;

revoke all on function public.dismiss_trend_run(uuid) from public, anon;
grant execute on function public.dismiss_trend_run(uuid) to authenticated;

comment on function public.dismiss_trend_run(uuid) is
  'Remove a trend run from the app''s lists, stopping it first if it is still going. Owner-only. The row is kept.';
