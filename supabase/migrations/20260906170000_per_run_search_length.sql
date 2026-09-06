-- =============================================================================
-- How long *this* run should take.
-- =============================================================================
--
-- `trend_settings` decides how a run behaves, and that is right for the
-- scheduled run: it happens unattended and should behave the same way every
-- day. It is wrong for the button. Someone pressing "Generate more ideas" has
-- a reason in mind that the schedule knows nothing about -- a quick look
-- before a meeting, or a deep sweep because the queue is empty -- and neither
-- of those should quietly become tomorrow morning's behaviour.
--
-- So the two settings that decide how long a run takes can be overridden for
-- one run, and only for that run. They ride on the `trend_runs` row, which is
-- already the record of what was asked for, and the runner merges them over
-- the stored controls when it starts.
--
-- Null means "use the saved setting", which is what every scheduled run
-- inserts and what the button sends when nothing has been chosen. That keeps
-- the default path byte-identical to the one before this migration.
--
-- Only these two are overridable, deliberately. They change what a run costs,
-- not what qualifies as a signal: a shorter run scouts fewer hashtags and
-- looks at fewer videos, but every filter it applies is still the one the
-- owner configured. Letting the button also loosen `min_outlier_ratio` would
-- make two runs incomparable, and the queue would stop meaning one thing.
-- =============================================================================

alter table public.trend_runs
  -- Both null on every scheduled run, and on any manual run started without a
  -- choice. See the header for why null rather than a copy of the setting:
  -- a copy would freeze the saved value at request time, so editing Settings
  -- while a run sat in the queue would be silently ignored.
  add column if not exists override_run_budget_minutes integer,
  add column if not exists override_hashtags_per_run   integer;

do $$
begin
  -- The same bounds as `trend_settings`, because they end up in the same place.
  -- A value the settings page would refuse must not become reachable just
  -- because it arrived through a different door.
  alter table public.trend_runs drop constraint if exists trend_runs_override_budget;
  alter table public.trend_runs add constraint trend_runs_override_budget
    check (override_run_budget_minutes is null
           or override_run_budget_minutes between 5 and 240);

  alter table public.trend_runs drop constraint if exists trend_runs_override_hashtags;
  alter table public.trend_runs add constraint trend_runs_override_hashtags
    check (override_hashtags_per_run is null
           or override_hashtags_per_run between 1 and 100);
end;
$$;

comment on column public.trend_runs.override_run_budget_minutes is
  'Time ceiling for this run only. Null uses trend_settings.run_budget_minutes.';
comment on column public.trend_runs.override_hashtags_per_run is
  'Hashtags to scout for this run only. Null uses trend_settings.hashtags_per_run.';


-- ---------------------------------------------------------------------------
-- Requesting a run, with an optional length.
-- ---------------------------------------------------------------------------
--
-- Dropped and recreated rather than `create or replace`, because the argument
-- list is changing. Replacing in place would leave the old zero-argument
-- version alongside the new one, and a call with no arguments would then match
-- both -- PostgREST would get an "is not unique" error on the one path the
-- whole button depends on.
drop function if exists public.request_trend_run();
drop function if exists public.request_trend_run(integer, integer);

create function public.request_trend_run(
  p_budget_minutes   integer default null,
  p_hashtags_per_run integer default null
)
returns public.trend_runs
language plpgsql
security definer
set search_path = public
as $$
declare
  v_run public.trend_runs;
begin
  if not public.is_owner() then
    raise exception 'Only an owner may start a trend run' using errcode = '42501';
  end if;

  begin
    insert into public.trend_runs (
      requested_by, override_run_budget_minutes, override_hashtags_per_run
    )
    values (auth.uid(), p_budget_minutes, p_hashtags_per_run)
    returning * into v_run;
  exception
    when unique_violation then
      -- Not an error worth a stack trace: the owner pressed the button twice,
      -- or two of them pressed it at once. 55006 is object_in_use, which the
      -- app maps to a plain sentence rather than a failure.
      raise exception 'A trend run is already in progress'
        using errcode = '55006';
    when check_violation then
      -- A length outside the bounds above. Reachable only from a stale tab or
      -- a hand-rolled call, since the control clamps -- but it should still
      -- come back as a sentence rather than a constraint name.
      raise exception 'That search length is outside the allowed range'
        using errcode = '22023';
  end;

  return v_run;
end;
$$;

revoke all on function public.request_trend_run(integer, integer) from public, anon;
grant execute on function public.request_trend_run(integer, integer) to authenticated;

comment on function public.request_trend_run(integer, integer) is
  'Start a trend run. Both arguments override trend_settings for this run only; null uses the saved value.';
