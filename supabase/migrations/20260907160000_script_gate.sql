-- =============================================================================
-- The script gate — a human writes the words before a cent is spent on them
-- =============================================================================
--
-- Until now `productions.script` was an *output*. Nobody could see the script
-- before the render, because on three of the four lanes there was no script
-- until the render was already running:
--
--   mpt / fal_visuals  MoneyPrinterTurbo wrote it inside the render and we
--                      read it back afterwards, at `fetch_and_qc`.
--   fal_full           written *after* `fal.submit()` — money already gone.
--   heygen             written before submit, and never shown to anyone.
--
-- So the one thing the README says a person is for — "decide *what we say*" —
-- was the one thing the pipeline decided by itself. This migration turns the
-- script into an *input*: the driver drafts it, stops, and does not submit a
-- render until an owner has approved the text.
--
-- The mechanism is deliberately the one Gate 2 already uses, and nothing more.
-- There is no token, no callback and no webhook: `claim_production` simply does
-- not return a row whose status is `awaiting_script`, and returns it again the
-- moment `approve_script` moves it. Waiting is free and cannot be dropped.
--
--   write_script  ->  open_script_gate  ->  await_script  ->  submit_render
--                                              ^
--                                              +-- the row rests here, for
--                                                  minutes or for weeks
--
-- The invariant this file exists to hold: **`task_id is not null` means a paid
-- render has been submitted, and every function here refuses to touch the
-- script once that is true.** Editing the words after the pictures have been
-- bought would be a lie about what the video says.

-- -----------------------------------------------------------------------------
-- 1. The new status
-- -----------------------------------------------------------------------------
--
-- `awaiting_script` sits alongside `awaiting_review`, and means the same kind
-- of thing: stopped, on purpose, waiting for a person. It is distinct from
-- `parked` (the machine stopped and something is wrong) because nothing is
-- wrong — and from `queued` because a queued row is one the driver will pick up
-- on its own.
alter table public.productions drop constraint if exists productions_status_check;
alter table public.productions add constraint productions_status_check
  check (status in (
    'queued', 'running', 'awaiting_script', 'qc_failed', 'awaiting_review',
    'approved', 'rejected', 'publishing', 'published', 'failed', 'parked',
    'cancelled'
  ));

-- -----------------------------------------------------------------------------
-- 2. Who approved the script, and when
-- -----------------------------------------------------------------------------
--
-- `script_approved_at` is the gate itself. `submit_render` is unreachable while
-- it is null, so it is not decoration: it is the flag the whole pipeline turns
-- on. Editing an approved script clears it again, which is what makes "saved"
-- and "approved" genuinely different actions rather than two words for one.
alter table public.productions add column if not exists script_approved_at timestamptz;
alter table public.productions add column if not exists script_approved_by uuid references public.profiles (id);
alter table public.productions add column if not exists script_updated_at  timestamptz;
alter table public.productions add column if not exists script_updated_by  uuid references public.profiles (id);

comment on column public.productions.script is
  'The narration, and the source of truth for it. Drafted by `write_script`, edited and approved by an owner, then passed into every render lane — `video_script` for MoneyPrinterTurbo, the script body for HeyGen, the TTS input for fal_full. No lane writes its own any more.';
comment on column public.productions.script_approved_at is
  'When an owner approved the script. Null means no render may be submitted: `claim_production` will not return this row at `await_script`, and `submit_render` refuses outright. Cleared by any later edit.';
comment on column public.productions.script_approved_by is
  'The owner who approved it. Null for a row that has not passed the gate.';
comment on column public.productions.script_updated_at is
  'When the script text last changed, by draft or by hand.';
comment on column public.productions.script_updated_by is
  'Who last changed the text. Null when the draft was written by the pipeline.';

-- An approved script must actually say something. Whitespace is not a script,
-- and a null one certainly is not.
alter table public.productions drop constraint if exists productions_approved_script_not_empty;
alter table public.productions add constraint productions_approved_script_not_empty
  check (script_approved_at is null or coalesce(btrim(script), '') <> '');

-- HeyGen's `POST /v3/videos` rejects a script over 5000 characters outright
-- rather than truncating it (see MAX_SCRIPT_CHARS in clients/heygen.py), and a
-- reel is forty seconds long. The cap is the lowest real limit across the four
-- lanes, applied to all of them so the same text is renderable by any style —
-- which matters because `rerun_production` can change the style afterwards.
alter table public.productions drop constraint if exists productions_script_length;
alter table public.productions add constraint productions_script_length
  check (script is null or length(script) <= 5000);

-- Approval names its approver, the way `approvals_actor_required_for_human`
-- does for a gate decision. Both are set together or neither is.
alter table public.productions drop constraint if exists productions_script_approval_is_attributed;
alter table public.productions add constraint productions_script_approval_is_attributed
  check ((script_approved_at is null) = (script_approved_by is null));

-- The requirement, as a constraint rather than as a convention.
--
-- `task_id` is set by `claim_render_slot` at the instant of submitting to a
-- render backend, so "this row has a task_id" and "this row has been paid for"
-- are the same statement. Requiring an approved script alongside it means the
-- database itself cannot hold a production that was rendered from words nobody
-- read -- whatever the driver, the graph or the browser believe.
--
-- This is the fourth and lowest place the rule is enforced, and the only one
-- that does not depend on running the right version of anything. It is what
-- closes the deployment window: between this migration landing and the worker
-- being redeployed, the old worker still thinks a fresh row starts at
-- `submit_render`, and this is what stops it spending. It fails loudly into the
-- driver's infrastructure backoff instead, costs nothing, and resolves itself
-- the moment the new worker starts.
--
-- Safe to add validated: no production in this database has ever had a
-- `task_id` and this constraint is checked against every existing row.
alter table public.productions drop constraint if exists productions_render_needs_approved_script;
alter table public.productions add constraint productions_render_needs_approved_script
  check (task_id is null or script_approved_at is not null);

-- -----------------------------------------------------------------------------
-- 3. The claim skips a row waiting for its script
-- -----------------------------------------------------------------------------
--
-- Two edits, both mirroring what is already there for Gate 2:
--
--   * `await_script` joins `await_gate2` in the exclusion, so a row waiting on
--     a person is never claimed however long it waits. Without this the driver
--     would spin on it forever.
--   * a separate arm admits it once `approve_script` has moved the status to
--     `running` — the decision *is* the row change, exactly as at Gate 2.
--
-- The default for a row with no step at all changes from `submit_render` to
-- `write_script`, because that is where `graph.START` now points. A row
-- inserted by `start_approved_productions` has no `run_state`, so this default
-- is what decides where a brand-new production begins.
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
              -- Work in flight. Both waiting steps are excluded here and
              -- admitted below.
              (status in ('queued', 'running', 'publishing')
               and coalesce(run_state ->> 'step', 'write_script')
                   not in ('await_gate2', 'await_script'))
              -- The script has been approved. The whole of the script gate:
              -- no token, no callback -- the row becomes claimable again.
              -- `script_approved_at` is checked as well as the status because
              -- it is the invariant `submit_render` depends on, and a status
              -- alone could be moved by a future function that forgets it.
           or (status = 'running'
               and run_state ->> 'step' = 'await_script'
               and script_approved_at is not null)
              -- A decision has been made at Gate 2.
           or (status in ('approved', 'rejected')
               and run_state ->> 'step' = 'await_gate2')
            )
        and paused_at is null
        and (lease_expires_at is null or lease_expires_at < now())
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

-- -----------------------------------------------------------------------------
-- 4. The helpers learn the two new steps
-- -----------------------------------------------------------------------------

-- Same as before, with the start step moved. `await_script` is not listed as a
-- terminal because it is not one.
create or replace function public.resume_step(p_run_state jsonb)
returns text
language sql
immutable
set search_path = public
as $$
  select case
           when coalesce(p_run_state ->> 'step', 'write_script')
                in ('parked', 'cancelled', 'published', 'rejected', 'publishing_disabled')
             then coalesce(p_run_state ->> 'previous_step', 'write_script')
           else coalesce(p_run_state ->> 'step', 'write_script')
         end;
$$;

-- `write_script` and `open_script_gate` are pre-render work, so a row resuming
-- there is `queued` until something is actually submitted -- same reasoning as
-- `submit_render`, and the same `p_has_task` test, which is what stops a row
-- that already has a render being called queued.
--
-- `await_script` is deliberately absent: `retry_production` rewrites that step
-- to `open_script_gate` before asking, for the same reason it rewrites
-- `await_gate2` to `open_gate2`.
create or replace function public.live_status_for_step(p_step text, p_has_task boolean)
returns text
language sql
immutable
set search_path = public
as $$
  select case
           when p_step in ('write_script', 'open_script_gate', 'submit_render')
                and not p_has_task                         then 'queued'
           when p_step in ('publish', 'poll_publish')      then 'publishing'
           else 'running'
         end;
$$;

revoke all on function public.resume_step(jsonb)                  from public, anon, authenticated;
revoke all on function public.live_status_for_step(text, boolean) from public, anon, authenticated;

-- -----------------------------------------------------------------------------
-- 5. Retry knows that a gate cannot be resumed into
-- -----------------------------------------------------------------------------
--
-- Replaced only to add the `await_script` arm. A row parked while it sat at the
-- script gate -- which `reconcile_leases` can do -- would otherwise be sent
-- back to `await_script` as `running` with `script_approved_at` still null, and
-- the claim admits that combination never. It would be stuck, and look retried.
-- Sending it to `open_script_gate` re-opens the gate exactly as the first pass
-- did. This is the same fix, for the same reason, as the `await_gate2` arm
-- directly below it.
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

  if v_step = 'await_script' then
    v_step := 'open_script_gate';
  end if;
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

revoke all on function public.retry_production(uuid, text) from public, anon;
grant execute on function public.retry_production(uuid, text) to authenticated;

-- -----------------------------------------------------------------------------
-- 6. Where the script may be touched at all
-- -----------------------------------------------------------------------------
--
-- One predicate, used by all three of the functions below, so "may I edit this
-- script?" has exactly one answer in this database rather than three that drift
-- apart.
--
-- `task_id is null` is the load-bearing half. It is set by `claim_render_slot`
-- at the instant of submitting to MoneyPrinterTurbo, fal or HeyGen, which makes
-- it precisely the "money may be moving" marker `cancel_production` already
-- relies on. Once it is set the render is being paid for against the script it
-- was given, and changing the text would only make the record wrong.
create or replace function public.script_is_editable(p_row public.productions)
returns boolean
language sql
immutable
set search_path = public
as $$
  select p_row.superseded_by is null
     and p_row.task_id is null
     and p_row.status in ('queued', 'running', 'awaiting_script', 'parked', 'failed')
     and coalesce(p_row.run_state ->> 'step', 'write_script')
         in ('write_script', 'open_script_gate', 'await_script');
$$;

revoke all on function public.script_is_editable(public.productions) from public, anon, authenticated;

-- Shared refusal, so every function gives the same reason for the same state.
create or replace function public.assert_script_editable(p_row public.productions)
returns void
language plpgsql
stable
set search_path = public
as $$
begin
  if p_row.superseded_by is not null then
    raise exception 'That production has been superseded by a re-run; edit the script on the one that replaced it'
      using errcode = 'P0002';
  end if;
  if p_row.task_id is not null then
    raise exception 'A render has already been submitted for this production, and it was paid for against the script as it stood. Re-run the idea to change the words.'
      using errcode = '22023';
  end if;
  if not public.script_is_editable(p_row) then
    raise exception 'This production is past the script gate (it is % at %), so its script is no longer an input to anything',
      p_row.status, coalesce(p_row.run_state ->> 'step', 'write_script')
      using errcode = '22023';
  end if;
end;
$$;

revoke all on function public.assert_script_editable(public.productions) from public, anon, authenticated;

-- Validation, in the one place that cannot be skipped by calling a different
-- client. The browser mirrors these rules for feedback; this is what holds.
create or replace function public.clean_script(p_script text)
returns text
language plpgsql
immutable
set search_path = public
as $$
declare
  v text := btrim(coalesce(p_script, ''));
begin
  if v = '' then
    raise exception 'A script cannot be empty' using errcode = '22023';
  end if;
  if length(v) > 5000 then
    raise exception 'That script is % characters; the limit is 5000, which is what HeyGen accepts', length(v)
      using errcode = '22023';
  end if;
  return v;
end;
$$;

revoke all on function public.clean_script(text) from public, anon, authenticated;

-- -----------------------------------------------------------------------------
-- 7. Save a draft, without opening the gate
-- -----------------------------------------------------------------------------
--
-- Saving and approving are separate actions on purpose. The brief was that
-- video generation must not start until the owner *explicitly* approves, and
-- an editor that starts a render on every keystroke-save would not be that.
-- So this persists the words and nothing else -- and, because an edit
-- invalidates any approval that came before it, it clears `script_approved_at`.
-- Editing an approved script and walking away therefore leaves the gate shut,
-- which is the safe direction to fail in.
create or replace function public.save_script(
  p_production_id uuid,
  p_script        text
)
returns public.productions
language plpgsql
security definer
set search_path = public
as $$
declare
  v_row  public.productions;
  v_text text;
begin
  if not public.is_owner() then
    raise exception 'Only an owner may edit a script' using errcode = '42501';
  end if;

  select * into v_row from public.productions where id = p_production_id;
  if v_row.id is null then
    raise exception 'Production % does not exist', p_production_id using errcode = 'P0002';
  end if;
  perform public.assert_script_editable(v_row);
  -- Not `assert_unleased`: the worker may hold this row while it drafts, and
  -- refusing an owner's edit for the two seconds an LLM call takes would be
  -- gratuitous. The write is a single UPDATE and the draft is written by a
  -- different statement, so the last writer wins -- and `write_script` will
  -- not overwrite a script that already exists.
  v_text := public.clean_script(p_script);

  update public.productions
     set script             = v_text,
         script_updated_at  = now(),
         script_updated_by  = auth.uid(),
         -- The edit is the un-approval. Both columns move together or the
         -- attribution constraint refuses the row.
         script_approved_at = null,
         script_approved_by = null
   where id = p_production_id
  returning * into v_row;

  perform public.record_control_event(
    p_production_id, 'script_saved',
    'Script edited and saved by an owner. Not yet approved, so no render can start.',
    null,
    jsonb_build_object('chars', length(v_text))
  );
  return v_row;
end;
$$;

revoke all on function public.save_script(uuid, text) from public, anon;
grant execute on function public.save_script(uuid, text) to authenticated;

-- -----------------------------------------------------------------------------
-- 8. Approve it, and let the render start
-- -----------------------------------------------------------------------------
--
-- This is the gate. It writes the text, the approval and the step marker in one
-- statement, which is the same lesson `open_gate2` records: two facts on one
-- row must be written together or there is a window in which the row is
-- decidable but not claimable, and it hangs there looking fine.
--
-- Takes the script text as well as the id so that "save then approve" is one
-- round trip and one transaction. An owner who edits and hits Approve does not
-- get to leave a version behind.
--
-- `open_script_gate` and `write_script` are accepted as well as `await_script`
-- so that a production parked because MoneyPrinterTurbo was unreachable is not
-- a dead end: the owner writes the script by hand and approves it, and the
-- pipeline carries on having never spoken to the drafting service at all.
create or replace function public.approve_script(
  p_production_id uuid,
  p_script        text,
  p_note          text default null
)
returns public.productions
language plpgsql
security definer
set search_path = public
as $$
declare
  v_row  public.productions;
  v_text text;
begin
  if not public.is_owner() then
    raise exception 'Only an owner may approve a script' using errcode = '42501';
  end if;

  select * into v_row from public.productions where id = p_production_id;
  if v_row.id is null then
    raise exception 'Production % does not exist', p_production_id using errcode = 'P0002';
  end if;
  perform public.assert_script_editable(v_row);
  -- Here the lease *does* matter, unlike `save_script`: this hands the row back
  -- to the driver, and doing that while a worker holds it would have two
  -- writers moving the same production at once.
  perform public.assert_unleased(v_row);

  v_text := public.clean_script(p_script);

  update public.productions
     set script             = v_text,
         script_updated_at  = now(),
         script_updated_by  = auth.uid(),
         script_approved_at = now(),
         script_approved_by = auth.uid(),
         error              = null,
         -- Claimable again. `await_script` + `running` + a non-null
         -- `script_approved_at` is the exact combination `claim_production`
         -- admits, and all three are set in this one statement.
         status             = 'running',
         stage              = 'script approved',
         run_state          = (coalesce(v_row.run_state, '{}'::jsonb)
                                - 'due_at' - 'ended_at' - 'error')
                              || jsonb_build_object(
                                   'step', 'await_script',
                                   'previous_step',
                                     coalesce(v_row.run_state ->> 'step', 'write_script'),
                                   'lease_expiries', 0
                                 )
   where id = p_production_id
  returning * into v_row;

  perform public.record_control_event(
    p_production_id, 'script_approved',
    'Script approved by an owner. The render may now be submitted against exactly these words.',
    p_note,
    jsonb_build_object('chars', length(v_text))
  );
  return v_row;
end;
$$;

revoke all on function public.approve_script(uuid, text, text) from public, anon;
grant execute on function public.approve_script(uuid, text, text) to authenticated;

-- -----------------------------------------------------------------------------
-- 9. Ask for another draft
-- -----------------------------------------------------------------------------
--
-- Sends the row back to `write_script` with a counter bumped. The counter, not
-- a boolean, because `write_script` is idempotent -- it keeps a script that
-- already exists, which is what stops a retry from throwing away an owner's
-- edits -- and it needs to tell "run again after a transient failure" from
-- "the owner asked for different words". A number that only ever goes up does
-- that without any state to reset.
--
-- Costs one LLM call and no video generation, which is why it needs no
-- confirmation dialog of its own beyond "this replaces what is there".
create or replace function public.request_script_redraft(
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
  v_n   int;
begin
  if not public.is_owner() then
    raise exception 'Only an owner may ask for another draft' using errcode = '42501';
  end if;

  select * into v_row from public.productions where id = p_production_id;
  if v_row.id is null then
    raise exception 'Production % does not exist', p_production_id using errcode = 'P0002';
  end if;
  perform public.assert_script_editable(v_row);
  perform public.assert_unleased(v_row);

  v_n := coalesce((v_row.run_state ->> 'redraft')::int, 0) + 1;

  update public.productions
     set status             = 'running',
         stage              = 'drafting the script',
         error              = null,
         paused_at          = null,
         -- A fresh draft is not an approved one.
         script_approved_at = null,
         script_approved_by = null,
         run_state          = (coalesce(v_row.run_state, '{}'::jsonb)
                                - 'due_at' - 'ended_at' - 'error')
                              || jsonb_build_object(
                                   'step', 'write_script',
                                   'previous_step',
                                     coalesce(v_row.run_state ->> 'step', 'write_script'),
                                   'redraft', v_n,
                                   'attempts',
                                     coalesce(v_row.run_state -> 'attempts', '{}'::jsonb)
                                       - 'write_script',
                                   'lease_expiries', 0
                                 )
   where id = p_production_id
  returning * into v_row;

  perform public.record_control_event(
    p_production_id, 'script_redraft',
    format('An owner asked for another draft (number %s). The previous text is replaced.', v_n),
    p_note,
    jsonb_build_object('redraft', v_n)
  );
  return v_row;
end;
$$;

revoke all on function public.request_script_redraft(uuid, text) from public, anon;
grant execute on function public.request_script_redraft(uuid, text) to authenticated;

-- -----------------------------------------------------------------------------
-- 10. Existing productions
-- -----------------------------------------------------------------------------
--
-- Anything already past `submit_render` keeps whatever script its render
-- produced and is deemed approved by the system, not by a person: those renders
-- are bought and there is nothing left to gate. Marking them rather than
-- leaving the column null keeps `script_approved_at is null` meaning exactly
-- one thing -- "no render may be submitted" -- instead of also meaning "this
-- row predates the gate".
--
-- `script_approved_by` stays null, which the attribution constraint would
-- normally refuse, so this is written as a system approval the same way
-- `approvals.source = 'system'` is: the constraint is checked, and these rows
-- would fail it. They are therefore given no approval timestamp either, and are
-- instead excluded by having a task_id -- which `submit_render` checks first.
-- Nothing to backfill. This block is kept as a note that the case was
-- considered, and asserts the assumption rather than trusting it.
do $$
declare
  v_orphans int;
begin
  select count(*) into v_orphans
    from public.productions
   where task_id is not null
     and status not in ('cancelled', 'rejected', 'failed', 'parked');
  if v_orphans > 0 then
    raise notice
      'script gate: % production(s) already have a render in flight or finished. They keep their existing script and are never re-gated, because submit_render only runs once per row.',
      v_orphans;
  end if;
end $$;
