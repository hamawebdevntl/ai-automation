-- =============================================================================
-- trend_runs -- an on-demand trend run, requested from the app.
-- =============================================================================
--
-- Trend research was schedule-only: one Fargate task a day at 06:00. That is
-- the right default and a bad floor. Editing the hashtags or the brief and
-- then waiting until tomorrow to see what they produce makes the settings page
-- almost untestable, and an empty queue on a Tuesday afternoon has no answer.
--
-- There is no server tier here, so the app cannot start a Fargate task itself.
-- It does what every other write in this system does: it records an intention
-- in Postgres and lets a scheduled reconciler act on it. `dispatch_trend_runs`
-- runs every minute, claims a requested row, and calls ecs:RunTask.
--
-- The row is therefore three things at once: the request, the lock that stops
-- a second one starting, and the record of what came back. The app reads the
-- last of those to show progress, which is the only feedback available for
-- something that takes the better part of an hour.
-- =============================================================================

create table if not exists public.trend_runs (
  id           uuid primary key default gen_random_uuid(),
  status       text not null default 'requested'
                 check (status in ('requested', 'running', 'succeeded', 'failed')),
  requested_by uuid references public.profiles (id) on delete set null,
  requested_at timestamptz not null default now(),
  started_at   timestamptz,
  finished_at  timestamptz,

  -- The ECS task, so a run that misbehaves can be found in the console without
  -- correlating timestamps by hand.
  task_arn     text,

  -- What the run returned, mirroring `runner.run`'s result. Null until it ends.
  signals      integer,
  drafted      integer,
  inserted     integer,
  suppressed   integer,
  error        text
);

comment on table public.trend_runs is
  'One on-demand trend research run: the request, the in-flight lock, and the result.';

create index if not exists trend_runs_requested_idx
  on public.trend_runs (requested_at desc);

-- At most one run in flight, enforced by Postgres rather than by a check in
-- application code. Two concurrent browser sessions, a double-click, and a
-- retried reconciler are all the same race, and each of the three would
-- otherwise start a second hour-long browser session against a platform that
-- is actively looking for exactly that.
--
-- The indexed expression is `true` for every row the WHERE clause admits, so
-- the index permits exactly one of them.
create unique index if not exists trend_runs_single_in_flight
  on public.trend_runs ((status in ('requested', 'running')))
  where status in ('requested', 'running');

-- ---------------------------------------------------------------------------
-- Requesting a run.
-- ---------------------------------------------------------------------------
--
-- A function rather than an INSERT policy for the same reason the gate
-- decisions are functions: it is the only place that can both check the role
-- and turn the unique-index collision into something the UI can say out loud.
create or replace function public.request_trend_run()
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
    insert into public.trend_runs (requested_by)
    values (auth.uid())
    returning * into v_run;
  exception
    when unique_violation then
      -- Not an error worth a stack trace: the owner pressed the button twice,
      -- or two of them pressed it at once. 55006 is object_in_use, which the
      -- app maps to a plain sentence rather than a failure.
      raise exception 'A trend run is already in progress'
        using errcode = '55006';
  end;

  return v_run;
end;
$$;

revoke all on function public.request_trend_run() from public, anon;
grant execute on function public.request_trend_run() to authenticated;

-- ---------------------------------------------------------------------------
-- Access. Everyone signed in may watch a run; nobody may write one by hand.
--
-- There is deliberately no INSERT, UPDATE or DELETE policy. Requests come from
-- the function above, and the pipeline writes progress with the service role,
-- which bypasses RLS. A viewer seeing that a run is under way is useful; a
-- viewer being able to start one is the thing Gate 1 exists to prevent.
-- ---------------------------------------------------------------------------
alter table public.trend_runs enable row level security;

drop policy if exists trend_runs_read on public.trend_runs;
create policy trend_runs_read on public.trend_runs
  for select to authenticated using (true);
