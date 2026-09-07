-- The worker loop's state, replacing AWS Step Functions.
--
-- Step Functions carried the payload between states in the execution itself:
-- `submit_render` returned a dict, the state machine held it, and `poll_render`
-- received it as its event. Nothing of that was ever written down. A worker
-- loop has no such place to keep it, so it goes here.
--
-- That is not bookkeeping. Four of those keys -- `fal_request_id`,
-- `fal_status_url`, `fal_response_url` and `heygen_video_id` -- are the only
-- handle on a generation that has already been billed. Losing them to a
-- process restart strands a paid render with nothing able to collect it, and
-- `reconcile_renders` then misreports it as "render state lost -- MPT restarted
-- without Redis", which on a fal render is confidently wrong. Everything else
-- in the payload is either derivable (`task_id` is the production id,
-- `storage_key` is "{id}/final.mp4") or already a column.

alter table public.productions
  add column if not exists run_state        jsonb not null default '{}'::jsonb,
  add column if not exists leased_by        text,
  add column if not exists lease_expires_at timestamptz;

comment on column public.productions.run_state is
  'Driver state: {step, due_at, attempts{}, lease_expiries, opened_at} merged with the payload the activities pass between steps. Readable by the browser on purpose -- see the migration for why.';
comment on column public.productions.leased_by is
  'Which worker currently holds this row. Advisory only; `lease_expires_at` is what actually excludes.';
comment on column public.productions.lease_expires_at is
  'When the claim lapses. An expired lease is the ordinary recovery path, not an error: the next claim picks the row up.';

-- Deliberately NOT revoked from `authenticated`.
--
-- The web app does `select('*')` on productions in two places, and PostgREST
-- answers a column-level REVOKE with 403 on the whole request rather than a
-- filtered row -- so revoking `run_state` would break the review queue outright.
-- That is acceptable because nothing in here is a credential: fal status URLs
-- are opaque and short-lived, a HeyGen video id is not secret, and the storage
-- key is derivable from a production id the browser already holds.
--
-- This is exactly the distinction that put `gate_tokens` in the `private`
-- schema instead: that table held an AWS capability -- anyone with the token
-- could resume the execution. This holds references to work we have already
-- paid for and can already see.

-- ---------------------------------------------------------------------------
-- The claim.
--
-- A function rather than a chained update because PostgREST cannot express
-- `for update skip locked`, and that is the whole mechanism: it is what lets
-- two workers claim two different rows concurrently without either blocking or
-- colliding.
--
-- Note the idiom this deliberately does NOT copy. `claim_trend_run` issues
-- `update(...).eq("status","requested")` and takes `rows[0]` -- which updates
-- *every* requested row and then runs one of them. That is survivable there
-- because a second in-flight trend run is refused by a unique index. Here it
-- would hand the same production to every worker at once.
-- ---------------------------------------------------------------------------
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

-- The `'submit_render'` default above is correct for exactly one case: a row
-- just inserted by `start_approved_productions`, which has no step yet. It is
-- also why a terminal step must write a terminal marker rather than clearing
-- `run_state` to '{}' -- a cleared row would default straight back to
-- `submit_render` and re-run the entire production, paying for it again.

create index if not exists productions_claimable_idx
  on public.productions (lease_expires_at, ((run_state ->> 'due_at')))
  where status in ('queued', 'running', 'publishing', 'approved', 'rejected');

-- ---------------------------------------------------------------------------
-- Gate 1, without a webhook.
--
-- Approving an idea used to fire a Supabase Database Webhook into API Gateway,
-- onto SQS, into a Lambda, which read the idea, inserted the production and
-- called StartExecution. All of that was carrying one insert.
--
-- Two things deduplicated it. The first was a read-then-insert in the Lambda,
-- backed by the `productions_one_live_per_idea` unique index -- and that index
-- is still here, so the guarantee is unchanged and is now enforced in the same
-- statement rather than across a read and a write. The second was Step
-- Functions refusing a duplicate execution named `prod-<id>`; that protected
-- against two executions of one production, which is now what the lease does.
-- ---------------------------------------------------------------------------
create or replace function public.start_approved_productions()
returns setof public.productions
language sql
security definer
set search_path = public
as $$
  insert into public.productions (idea_id, style_preset_id, status, stage, cost_estimate_usd)
  select i.id,
         i.approved_style_id,
         'queued',
         'queued',
         -- The owner commits money at Gate 1 against the preset's advertised
         -- range, so that range is the estimate of record.
         sp.est_cost_max_usd
    from public.ideas i
    join public.style_presets sp on sp.id = i.approved_style_id
   where i.status = 'approved'
     and not exists (
       select 1
         from public.productions p
        where p.idea_id = i.id
          and p.status not in ('rejected', 'failed', 'parked')
     )
  on conflict do nothing
  returning *;
$$;

-- Supabase grants EXECUTE on new public functions to `anon` and
-- `authenticated` by default, which is how an anonymous caller once got a 200
-- from `take_gate_token`. Both of these are service-role tools: one hands out
-- work, the other spends money.
revoke all on function public.claim_production(text, int)      from public, anon, authenticated;
revoke all on function public.start_approved_productions()     from public, anon, authenticated;
grant execute on function public.claim_production(text, int)   to service_role;
grant execute on function public.start_approved_productions()  to service_role;
