-- Make the post-approval pipeline visible and controllable.
--
-- Until now a production recorded where it *is* and never where it has *been*:
-- `run_state` is one mutable jsonb that every step overwrites, and `approvals`
-- holds gate decisions only. Between Gate 1 and Gate 2 the web app showed
-- nothing at all, because nothing was written down to show.
--
-- Three things change here:
--   1. `production_events` -- an append-only per-step log, the thing that did
--      not exist.
--   2. Six owner-gated control functions, so an owner can retry, pause, resume,
--      cancel and re-make a production from the app instead of from psql.
--   3. Realtime on both tables, so the UI advances as the worker writes rather
--      than on a poll.
--
-- The control functions live here rather than in the driver for the same reason
-- the gate functions do: there is no server tier, so Postgres is what keeps
-- "change the row" and "write the audit row" from coming apart, and what
-- refuses a viewer.
--
-- `..._one_production_per_approval.sql` already stopped the poller reopening a
-- parked production, and noted that a deliberate re-run then meant deleting the
-- parked row by hand. `rerun_production` below is that path, done properly:
-- the old production keeps its cut and its history instead of being destroyed.

-- =============================================================================
-- The step log
-- =============================================================================
create table if not exists public.production_events (
  id            uuid primary key default gen_random_uuid(),
  production_id uuid not null references public.productions (id) on delete cascade,
  step          text not null,
  outcome       text not null check (outcome in (
                  'started', 'progress', 'succeeded', 'retrying', 'infra_retry',
                  'waiting', 'failed', 'parked', 'terminal', 'control')),
  detail        text,
  error         text,
  attempt       integer,
  payload       jsonb not null default '{}'::jsonb,
  actor_id      uuid references public.profiles (id),
  created_at    timestamptz not null default now()
);

comment on table public.production_events is
  'Append-only record of every step a production took. The driver writes one row per transition; the control functions below write one per human action. Never updated, never deleted except by the production cascade.';
comment on column public.production_events.step is
  'The driver step this happened at -- a key of GRAPH in apps/pipeline/pipeline/driver/graph.py -- or ''control'' for a human action.';
comment on column public.production_events.payload is
  'Technical detail for this event: provider ids, render progress, QC numbers, which platforms got copy. Shown in the UI behind a disclosure.';
comment on column public.production_events.actor_id is
  'Set only on ''control'' events. A driver event has no actor, which is what distinguishes the two in the UI.';

create index if not exists production_events_production_idx
  on public.production_events (production_id, created_at);

alter table public.production_events enable row level security;

drop policy if exists production_events_read on public.production_events;
create policy production_events_read on public.production_events
  for select to authenticated using (true);

-- No INSERT/UPDATE/DELETE policy, deliberately, exactly as `productions` has
-- none: the only writers are the service-role worker and the security-definer
-- functions below. An append-only log a client could append to is not one.

-- =============================================================================
-- New production columns
-- =============================================================================
alter table public.productions
  add column if not exists paused_at     timestamptz,
  add column if not exists superseded_by uuid;

-- Deferrable on purpose. `rerun_production` has to mark the old row superseded
-- *before* inserting its replacement -- otherwise the two collide on
-- `productions_one_live_per_idea`, since a `published` row is not excluded by
-- status -- but the replacement does not exist yet at that moment. Deferring the
-- check to commit is what lets both orderings be satisfied at once.
alter table public.productions drop constraint if exists productions_superseded_by_fkey;
alter table public.productions
  add constraint productions_superseded_by_fkey
  foreign key (superseded_by) references public.productions (id)
  deferrable initially deferred;

comment on column public.productions.paused_at is
  'Set by pause_production. This is the whole of pause: `claim_production` does not return a paused row, so the driver simply never picks it up again. An in-flight step finishes first -- pause lands at the next step boundary.';
comment on column public.productions.superseded_by is
  'The production that replaced this one, set by rerun_production. A superseded row keeps its cut, its events and its history; it is only excluded from the one-live-per-idea guarantee.';

-- `cancelled` is a new terminal. It is distinct from `rejected` (a human said
-- no to a finished cut) and from `parked` (the machine stopped and wants a
-- person): it means a human stopped this production before it finished.
alter table public.productions drop constraint if exists productions_status_check;
alter table public.productions add constraint productions_status_check
  check (status in (
    'queued', 'running', 'qc_failed', 'awaiting_review', 'approved',
    'rejected', 'publishing', 'published', 'failed', 'parked', 'cancelled'
  ));

-- The one-live-per-idea guarantee has to admit two new cases: a cancelled row
-- must stop blocking, and a superseded row must not collide with the row that
-- replaced it.
drop index if exists public.productions_one_live_per_idea;
create unique index productions_one_live_per_idea
  on public.productions (idea_id)
  where status not in ('rejected', 'failed', 'parked', 'cancelled')
    and superseded_by is null;

-- =============================================================================
-- The claim skips paused rows
-- =============================================================================
create or replace function public.claim_production(p_worker text, p_lease_seconds int)
returns public.productions
language plpgsql
security definer
set search_path = public
as $$
declare
  v_row public.productions;
begin
  update public.productions p
     set leased_by        = p_worker,
         lease_expires_at = now() + make_interval(secs => p_lease_seconds)
   where p.id = (
     select id
       from public.productions
      where (
              -- Work in flight. `await_gate2` is excluded here and admitted
              -- below: a row waiting on a human must not be claimed, however
              -- long it waits, or the driver would spin on it forever.
              (status in ('queued', 'running', 'publishing')
               and coalesce(run_state ->> 'step', 'submit_render') <> 'await_gate2')
              -- A decision has been made. This is the whole of Gate 2 now:
              -- no task token, no callback, no webhook -- the row simply
              -- becomes claimable again.
           or (status in ('approved', 'rejected')
               and run_state ->> 'step' = 'await_gate2')
            )
        -- Pause, in one line. Same mechanism as the gate above: a paused row is
        -- one the claim does not return.
        and paused_at is null
        and (lease_expires_at is null or lease_expires_at < now())
        -- Every Wait state, every retry interval and every poll-again arc in
        -- the old state machine is this one column.
        and coalesce((run_state ->> 'due_at')::timestamptz, '-infinity') <= now()
      order by coalesce((run_state ->> 'due_at')::timestamptz, created_at)
      for update skip locked
      limit 1
   )
  returning p.* into v_row;
  return v_row;
end;
$$;

revoke all on function public.claim_production(text, int) from public, anon, authenticated;
grant execute on function public.claim_production(text, int) to service_role;

-- =============================================================================
-- Helpers for the control functions
-- =============================================================================

-- Where a production should resume from.
--
-- A production parked by the driver has `run_state.step = 'parked'` -- the
-- failing step is overwritten by the terminal marker -- so the engine now also
-- writes `previous_step` before it enters a terminal. A production parked by a
-- reconciler never went through the engine's routing at all, so its `step` is
-- still the real one. This handles both.
create or replace function public.resume_step(p_run_state jsonb)
returns text
language sql
immutable
set search_path = public
as $$
  select case
           when coalesce(p_run_state ->> 'step', 'submit_render')
                in ('parked', 'cancelled', 'published', 'rejected', 'publishing_disabled')
             then coalesce(p_run_state ->> 'previous_step', 'submit_render')
           else coalesce(p_run_state ->> 'step', 'submit_render')
         end;
$$;

-- The status a row must carry for `claim_production` to return it at a step.
create or replace function public.live_status_for_step(p_step text, p_has_task boolean)
returns text
language sql
immutable
set search_path = public
as $$
  select case
           when p_step = 'submit_render' and not p_has_task then 'queued'
           when p_step in ('publish', 'poll_publish')        then 'publishing'
           else 'running'
         end;
$$;

-- One audit row per human action, so a control is as traceable as a step.
create or replace function public.record_control_event(
  p_production_id uuid,
  p_action        text,
  p_detail        text,
  p_note          text,
  p_payload       jsonb default '{}'::jsonb
)
returns void
language sql
security definer
set search_path = public
as $$
  insert into public.production_events
    (production_id, step, outcome, detail, payload, actor_id)
  values
    (p_production_id, 'control', 'control', p_detail,
     coalesce(p_payload, '{}'::jsonb)
       || jsonb_build_object('action', p_action)
       || case when coalesce(btrim(p_note), '') = ''
               then '{}'::jsonb
               else jsonb_build_object('note', p_note) end,
     auth.uid());
$$;

-- Every control below refuses a leased row rather than racing the driver for
-- `run_state`. `save_run_state` nulls the lease in the same statement as every
-- write, so a production is unleased for almost all of its life and this is
-- rarely felt -- but when it is felt, the honest answer is "the worker is
-- mid-step", not a lost update.
create or replace function public.assert_unleased(p_row public.productions)
returns void
language plpgsql
stable
set search_path = public
as $$
begin
  if p_row.lease_expires_at is not null and p_row.lease_expires_at > now() then
    raise exception 'The worker is running a step on this production right now — try again in a moment'
      using errcode = '55006';
  end if;
end;
$$;

revoke all on function public.resume_step(jsonb)                                 from public, anon, authenticated;
revoke all on function public.live_status_for_step(text, boolean)                from public, anon, authenticated;
revoke all on function public.record_control_event(uuid, text, text, text, jsonb) from public, anon, authenticated;
revoke all on function public.assert_unleased(public.productions)                from public, anon, authenticated;

-- =============================================================================
-- The controls
-- =============================================================================

-- Hold a production where it is.
--
-- Note what this deliberately does NOT do: stop a render that is already
-- running at MoneyPrinterTurbo, fal or HeyGen. Those keep going and are
-- collected when the row resumes. The UI says so rather than implying a pause
-- saves money.
create or replace function public.pause_production(
  p_production_id uuid,
  p_note          text default null
)
returns public.productions
language plpgsql
security definer
set search_path = public
as $$
declare
  v_row public.productions;
begin
  if not public.is_owner() then
    raise exception 'Only an owner may pause a production' using errcode = '42501';
  end if;

  select * into v_row from public.productions where id = p_production_id;
  if v_row.id is null then
    raise exception 'Production % does not exist', p_production_id using errcode = 'P0002';
  end if;
  if v_row.paused_at is not null then
    raise exception 'That production is already paused' using errcode = 'P0002';
  end if;
  if v_row.status in ('published', 'rejected', 'cancelled', 'failed') then
    raise exception 'A finished production cannot be paused' using errcode = 'P0002';
  end if;

  update public.productions
     set paused_at = now()
   where id = p_production_id
  returning * into v_row;

  perform public.record_control_event(
    p_production_id, 'pause',
    'Paused by an owner. The current step finishes, then nothing else runs until it is resumed.',
    p_note
  );
  return v_row;
end;
$$;

-- Let it go again.
--
-- The render budget has to move with it. `poll_render` enforces its wall clock
-- as `time.time() - started_at > budget`, so a production paused for longer
-- than the budget would blow it the instant it came back and park immediately.
-- Restarting the clock here is the same thing `_handoff_to_mpt` does when it
-- hands a fal render to MoneyPrinterTurbo: the budget applies to the render,
-- not to the row.
create or replace function public.resume_production(
  p_production_id uuid,
  p_note          text default null
)
returns public.productions
language plpgsql
security definer
set search_path = public
as $$
declare
  v_row  public.productions;
  v_step text;
begin
  if not public.is_owner() then
    raise exception 'Only an owner may resume a production' using errcode = '42501';
  end if;

  select * into v_row from public.productions where id = p_production_id;
  if v_row.id is null then
    raise exception 'Production % does not exist', p_production_id using errcode = 'P0002';
  end if;
  if v_row.paused_at is null then
    raise exception 'That production is not paused' using errcode = 'P0002';
  end if;
  perform public.assert_unleased(v_row);

  v_step := public.resume_step(v_row.run_state);

  update public.productions
     set paused_at = null,
         run_state = case
           when v_step in ('poll_render', 'fetch_and_qc')
             then (v_row.run_state - 'due_at')
                  || jsonb_build_object('started_at', extract(epoch from now()))
           else v_row.run_state - 'due_at'
         end
   where id = p_production_id
  returning * into v_row;

  perform public.record_control_event(
    p_production_id, 'resume',
    format('Resumed by an owner at %s.', v_step),
    p_note,
    jsonb_build_object('step', v_step)
  );
  return v_row;
end;
$$;

-- Send a parked production back in at the step it stopped on.
--
-- The one thing this must never do is clear `task_id`. `claim_render_slot` is a
-- conditional update that only succeeds while `task_id is null`, and it is the
-- sole guard against paying for the same render twice -- MoneyPrinterTurbo has
-- no idempotency of its own. A retry that nulled it would let `submit_render`
-- mint a second billed render. So the render handles (`task_id`,
-- `fal_request_id`, `heygen_video_id`, `storage_key`) are left exactly as they
-- are and the row resumes polling the render it already has.
create or replace function public.retry_production(
  p_production_id uuid,
  p_note          text default null
)
returns public.productions
language plpgsql
security definer
set search_path = public
as $$
declare
  v_row    public.productions;
  v_step   text;
  v_status text;
begin
  if not public.is_owner() then
    raise exception 'Only an owner may retry a production' using errcode = '42501';
  end if;

  select * into v_row from public.productions where id = p_production_id;
  if v_row.id is null then
    raise exception 'Production % does not exist', p_production_id using errcode = 'P0002';
  end if;
  if v_row.status not in ('parked', 'failed') then
    raise exception 'Only a parked production can be retried; this one is %', v_row.status
      using errcode = 'P0002';
  end if;
  perform public.assert_unleased(v_row);

  v_step := public.resume_step(v_row.run_state);

  -- A row parked while it was sitting at Gate 2 -- which `reconcile_leases`
  -- can do -- would otherwise be sent back to `await_gate2` as `running`, and
  -- `claim_production` admits that step only for a row whose status is
  -- `approved` or `rejected`. It would be unclaimable: stuck, and looking
  -- retried. Send it to `open_gate2` instead, which re-opens the gate and
  -- works out `awaiting_review` against `qc_failed` the way it did the first
  -- time.
  if v_step = 'await_gate2' then
    v_step := 'open_gate2';
  end if;

  v_status := public.live_status_for_step(v_step, v_row.task_id is not null);

  update public.productions
     set status    = v_status,
         error     = null,
         paused_at = null,
         run_state = (v_row.run_state - 'due_at' - 'ended_at' - 'error' - 'previous_step')
                     || jsonb_build_object(
                          'step', v_step,
                          -- Only this step's attempts are forgiven. A retry is
                          -- a fresh budget for the step that failed, not an
                          -- amnesty for every step before it.
                          'attempts', coalesce(v_row.run_state -> 'attempts', '{}'::jsonb) - v_step,
                          'lease_expiries', 0
                        )
                     || case
                          when v_step in ('poll_render', 'fetch_and_qc')
                            then jsonb_build_object('started_at', extract(epoch from now()))
                          else '{}'::jsonb
                        end
   where id = p_production_id
  returning * into v_row;

  perform public.record_control_event(
    p_production_id, 'retry',
    format('Retried by an owner from %s.', v_step),
    p_note,
    jsonb_build_object('step', v_step, 'status', v_status, 'task_id', v_row.task_id)
  );
  return v_row;
end;
$$;

-- Stop a production for good.
--
-- Refused while a paid render may still be in flight. `task_id` is set by
-- `claim_render_slot` at the moment of submitting, so it is precisely the
-- "money may be moving" marker: cancelling then would abandon a render we have
-- already been billed for while telling the owner it was stopped. Wait for it
-- to finish or park, then cancel. Pause is available in the meantime.
create or replace function public.cancel_production(
  p_production_id uuid,
  p_note          text default null
)
returns public.productions
language plpgsql
security definer
set search_path = public
as $$
declare
  v_row  public.productions;
  v_step text;
begin
  if not public.is_owner() then
    raise exception 'Only an owner may cancel a production' using errcode = '42501';
  end if;

  select * into v_row from public.productions where id = p_production_id;
  if v_row.id is null then
    raise exception 'Production % does not exist', p_production_id using errcode = 'P0002';
  end if;
  if v_row.status in ('published', 'rejected', 'cancelled') then
    raise exception 'That production has already finished' using errcode = 'P0002';
  end if;
  perform public.assert_unleased(v_row);

  v_step := coalesce(v_row.run_state ->> 'step', 'submit_render');
  if v_row.task_id is not null
     and v_step in ('submit_render', 'poll_render', 'fetch_and_qc') then
    raise exception 'A render is still running for this production, and cancelling now would abandon work already billed. Wait for it to finish or park, or pause it instead.'
      using errcode = '22023';
  end if;

  update public.productions
     set status       = 'cancelled',
         paused_at    = null,
         completed_at = now(),
         error        = null,
         run_state    = v_row.run_state
                        || jsonb_build_object(
                             'step', 'cancelled',
                             'previous_step', v_step,
                             'ended_at', to_jsonb(now())
                           )
   where id = p_production_id
  returning * into v_row;

  perform public.record_control_event(
    p_production_id, 'cancel',
    format('Cancelled by an owner at %s.', v_step),
    p_note,
    jsonb_build_object('step', v_step)
  );
  return v_row;
end;
$$;

-- Make it again.
--
-- A fresh row, because a re-make has no task_id and no video and would
-- otherwise overwrite the cut the owner is comparing against. The old row keeps
-- its render, its QC report and its whole event log; it is only marked
-- superseded so the one-live-per-idea index lets the replacement exist.
create or replace function public.rerun_production(
  p_production_id uuid,
  p_style_id      uuid default null,
  p_note          text default null
)
returns public.productions
language plpgsql
security definer
set search_path = public
as $$
declare
  v_old   public.productions;
  v_new   public.productions;
  v_style uuid;
  v_id    uuid := gen_random_uuid();
begin
  if not public.is_owner() then
    raise exception 'Only an owner may re-run a production' using errcode = '42501';
  end if;

  select * into v_old from public.productions where id = p_production_id;
  if v_old.id is null then
    raise exception 'Production % does not exist', p_production_id using errcode = 'P0002';
  end if;
  if v_old.superseded_by is not null then
    raise exception 'That production has already been re-run' using errcode = 'P0002';
  end if;
  if not (
       v_old.status in ('parked', 'cancelled', 'rejected', 'published', 'failed')
    or (v_old.status = 'approved' and v_old.run_state ->> 'step' = 'publishing_disabled')
  ) then
    raise exception 'This production is still running. Cancel it first, or wait for it to finish.'
      using errcode = 'P0002';
  end if;
  perform public.assert_unleased(v_old);

  v_style := coalesce(p_style_id, v_old.style_preset_id);
  if not exists (select 1 from public.style_presets where id = v_style and is_active) then
    raise exception 'Unknown or inactive style preset' using errcode = '22023';
  end if;

  -- Mark the old row first: a `published` row is not excluded by status, so
  -- without this the insert below collides with the unique index. The foreign
  -- key is deferred, which is what makes this ordering legal.
  update public.productions set superseded_by = v_id where id = p_production_id;

  insert into public.productions
    (id, idea_id, style_preset_id, status, stage, cost_estimate_usd, run_state)
  select v_id, v_old.idea_id, v_style, 'queued', 'queued', sp.est_cost_max_usd,
         jsonb_build_object('rerun_of', p_production_id::text)
    from public.style_presets sp
   where sp.id = v_style
  returning * into v_new;

  perform public.record_control_event(
    p_production_id, 'rerun',
    'Re-run by an owner. This production was superseded by a new one.',
    p_note,
    jsonb_build_object('superseded_by', v_id, 'style_preset_id', v_style)
  );
  perform public.record_control_event(
    v_id, 'rerun',
    'Opened by an owner as a re-run of an earlier production.',
    p_note,
    jsonb_build_object('rerun_of', p_production_id, 'style_preset_id', v_style)
  );
  return v_new;
end;
$$;

-- Redo a later step on the same row.
--
-- The counterpart to `rerun_production`, and separate from it because the two
-- cannot be one function: a fresh row has no render to re-check and no video to
-- write copy against, so it can only ever start at `submit_render`. Redoing
-- `fetch_and_qc`, `generate_copy` or `open_gate2` needs *this* row's artifacts,
-- so it acts in place. `submit_render` is deliberately not reachable here --
-- that is a re-run, and it costs money.
create or replace function public.rewind_production(
  p_production_id uuid,
  p_step          text,
  p_note          text default null
)
returns public.productions
language plpgsql
security definer
set search_path = public
as $$
declare
  v_row public.productions;
begin
  if not public.is_owner() then
    raise exception 'Only an owner may rewind a production' using errcode = '42501';
  end if;

  if p_step not in ('fetch_and_qc', 'generate_copy', 'open_gate2') then
    raise exception 'A production can only be rewound to fetch_and_qc, generate_copy or open_gate2'
      using errcode = '22023';
  end if;

  select * into v_row from public.productions where id = p_production_id;
  if v_row.id is null then
    raise exception 'Production % does not exist', p_production_id using errcode = 'P0002';
  end if;
  if v_row.superseded_by is not null then
    raise exception 'That production has been superseded by a re-run' using errcode = 'P0002';
  end if;
  perform public.assert_unleased(v_row);

  if p_step = 'fetch_and_qc' and v_row.task_id is null then
    raise exception 'There is no render to re-check on this production' using errcode = 'P0002';
  end if;
  if p_step in ('generate_copy', 'open_gate2') and v_row.video_url is null then
    raise exception 'There is no finished cut on this production yet' using errcode = 'P0002';
  end if;

  update public.productions
     set status        = 'running',
         error         = null,
         paused_at     = null,
         completed_at  = null,
         -- Re-opening Gate 2 means the earlier decision no longer stands.
         decided_by    = case when p_step = 'open_gate2' then null else v_row.decided_by end,
         decided_at    = case when p_step = 'open_gate2' then null else v_row.decided_at end,
         decision_note = case when p_step = 'open_gate2' then null else v_row.decision_note end,
         run_state     = (v_row.run_state - 'due_at' - 'ended_at' - 'error' - 'previous_step')
                         || jsonb_build_object(
                              'step', p_step,
                              'attempts', coalesce(v_row.run_state -> 'attempts', '{}'::jsonb) - p_step,
                              'lease_expiries', 0
                            )
                         || case
                              when p_step = 'fetch_and_qc'
                                then jsonb_build_object('started_at', extract(epoch from now()))
                              else '{}'::jsonb
                            end
   where id = p_production_id
  returning * into v_row;

  perform public.record_control_event(
    p_production_id, 'rewind',
    format('Rewound by an owner to %s.', p_step),
    p_note,
    jsonb_build_object('step', p_step)
  );
  return v_row;
end;
$$;

revoke all on function public.pause_production(uuid, text)        from public, anon;
revoke all on function public.resume_production(uuid, text)       from public, anon;
revoke all on function public.retry_production(uuid, text)        from public, anon;
revoke all on function public.cancel_production(uuid, text)       from public, anon;
revoke all on function public.rerun_production(uuid, uuid, text)  from public, anon;
revoke all on function public.rewind_production(uuid, text, text) from public, anon;

grant execute on function public.pause_production(uuid, text)        to authenticated;
grant execute on function public.resume_production(uuid, text)       to authenticated;
grant execute on function public.retry_production(uuid, text)        to authenticated;
grant execute on function public.cancel_production(uuid, text)       to authenticated;
grant execute on function public.rerun_production(uuid, uuid, text)  to authenticated;
grant execute on function public.rewind_production(uuid, text, text) to authenticated;

-- =============================================================================
-- Realtime
-- =============================================================================
--
-- The first tables in this project to broadcast. `config.toml` has always
-- started the Realtime service, but no table was ever added to the publication,
-- so progress was a poll. RLS still applies to a subscription and both tables
-- read `to authenticated using (true)`, so there is no policy work here.
do $$
begin
  if exists (select 1 from pg_publication where pubname = 'supabase_realtime') then
    if not exists (
      select 1 from pg_publication_tables
       where pubname = 'supabase_realtime' and schemaname = 'public' and tablename = 'productions'
    ) then
      execute 'alter publication supabase_realtime add table public.productions';
    end if;
    if not exists (
      select 1 from pg_publication_tables
       where pubname = 'supabase_realtime' and schemaname = 'public' and tablename = 'production_events'
    ) then
      execute 'alter publication supabase_realtime add table public.production_events';
    end if;
  end if;
end $$;

-- An UPDATE payload carries only the replica identity by default, and the UI
-- renders `run_state`, `stage` and `status` straight from the payload rather
-- than refetching. `production_events` is insert-only, so it needs nothing.
alter table public.productions replica identity full;
