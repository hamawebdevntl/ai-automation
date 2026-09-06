-- =============================================================================
-- Stopping a trend run.
-- =============================================================================
--
-- At most one run may be in flight, enforced by `trend_runs_single_in_flight`.
-- That is the right rule -- two concurrent hour-long browser sessions against a
-- platform that actively looks for exactly that is the thing worth preventing --
-- but it means a run that will never finish does not hold up one run. It holds
-- up every future run, and the button in the app stays shut for as long as the
-- row sits there.
--
-- Until now the only thing that could clear such a row was `dispatch_trend_runs`,
-- which writes off a request left unclaimed for ten minutes. That works whenever
-- the dispatcher is running, and is useless precisely when it is not -- which is
-- the most common way to end up stuck in the first place. An unapplied
-- `terraform apply`, a Lambda that cannot start, a broken image: each leaves a
-- `requested` row that nothing will ever claim and nothing will ever expire, and
-- the app correctly reports that nothing is coming to clear it.
--
-- So cancelling is a database function, and deliberately nothing more. It needs
-- no sweeper, no Lambda and no AWS call to take effect: it moves the row out of
-- the in-flight set, and the partial unique index frees itself. That is what
-- makes it work in the state it exists to get you out of.
--
-- Stopping the Fargate task, where there is one, is a separate concern handled
-- separately and twice over; see `task_stopped_at` below.
-- =============================================================================


-- ---------------------------------------------------------------------------
-- `cancelled` is its own status, not a flavour of `failed`.
-- ---------------------------------------------------------------------------
--
-- The temptation is to reuse `failed` with an explanatory error string, and
-- skip this constraint change. The reason not to is the banner: a failed run
-- shows as a red "The last run did not finish", and firing that every time
-- someone deliberately stops a run teaches them to ignore the one alarm that
-- reports a genuine breakage. A run you stopped is not a run that broke.
-- The original constraint was declared inline on the column, so its name was
-- generated rather than chosen. `drop constraint if exists` on a guessed name
-- is the dangerous shape here: if the guess is wrong it succeeds silently, the
-- old constraint stays, and every attempt to cancel a run is rejected by a
-- constraint nobody remembers writing. So it is found by what it does.
do $$
declare
  v_name text;
begin
  select con.conname into v_name
    from pg_constraint con
    join pg_class rel on rel.oid = con.conrelid
    join pg_namespace nsp on nsp.oid = rel.relnamespace
   where nsp.nspname = 'public'
     and rel.relname = 'trend_runs'
     and con.contype = 'c'
     and pg_get_constraintdef(con.oid) like '%requested%'
     and pg_get_constraintdef(con.oid) like '%succeeded%'
   limit 1;

  if v_name is not null then
    execute format('alter table public.trend_runs drop constraint %I', v_name);
  end if;

  -- Named this time, and dropped first, so re-running this migration is safe.
  alter table public.trend_runs drop constraint if exists trend_runs_status;
  alter table public.trend_runs
    add constraint trend_runs_status
    check (status in ('requested', 'running', 'succeeded', 'failed', 'cancelled'));
end;
$$;

alter table public.trend_runs
  add column if not exists cancelled_at timestamptz,
  add column if not exists cancelled_by uuid references public.profiles (id) on delete set null,
  -- When the dispatcher last *tried* to stop this run's ECS task -- an attempt,
  -- not a confirmation. It exists to bound the retry: without it, a task that
  -- cannot be stopped (already gone, permissions changed, a task ARN from a
  -- deleted cluster) would be retried every minute for the life of the table.
  -- Set whether the call succeeds or fails, for that reason.
  add column if not exists task_stopped_at timestamptz;

comment on column public.trend_runs.cancelled_at is
  'When an owner stopped this run from the app. The row leaves the in-flight set at the same instant.';
comment on column public.trend_runs.task_stopped_at is
  'When the dispatcher last attempted ecs:StopTask for this run. An attempt, not a confirmation.';

-- Cancelling frees the lock with no further action, because `cancelled` is not
-- one of the statuses `trend_runs_single_in_flight` indexes. Nothing to change
-- here -- but it is the whole mechanism, so it is worth saying out loud rather
-- than leaving to be rediscovered.


-- ---------------------------------------------------------------------------
-- Stopping a run.
-- ---------------------------------------------------------------------------
--
-- A function rather than an UPDATE policy, for the same reasons
-- `request_trend_run` is one: it is the only place that can check the role and
-- turn "there was nothing to stop" into a sentence rather than a silent
-- no-op update matching zero rows.
create or replace function public.cancel_trend_run(p_run_id uuid)
returns public.trend_runs
language plpgsql
security definer
set search_path = public
as $$
declare
  v_run public.trend_runs;
begin
  if not public.is_owner() then
    raise exception 'Only an owner may stop a trend run' using errcode = '42501';
  end if;

  -- The status filter is the concurrency control. Two owners pressing Stop at
  -- once, or a Stop racing the task's own completion, both resolve here: the
  -- first update matches, the second matches nothing and is told why.
  --
  -- `finished_at` is set alongside `cancelled_at` so that every terminal row
  -- has one, and anything reading run durations does not have to special-case
  -- this status.
  update public.trend_runs
     set status       = 'cancelled',
         cancelled_at = now(),
         cancelled_by = auth.uid(),
         finished_at  = now()
   where id = p_run_id
     and status in ('requested', 'running')
  returning * into v_run;

  if not found then
    -- Not an error worth a stack trace. The usual cause is a page showing a
    -- run that has since finished on its own, which is a good outcome
    -- described badly if it comes back as a failure.
    raise exception 'That run is no longer in flight'
      using errcode = 'P0002';
  end if;

  return v_run;
end;
$$;

revoke all on function public.cancel_trend_run(uuid) from public, anon;
grant execute on function public.cancel_trend_run(uuid) to authenticated;

comment on function public.cancel_trend_run(uuid) is
  'Stop an in-flight trend run. Owner-only. Frees the single in-flight lock immediately, with no dependency on the dispatcher.';
