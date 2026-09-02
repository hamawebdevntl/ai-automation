-- Pipeline integration layer.
--
-- Everything here exists so an automated pipeline can drive a production from
-- "idea approved" to "live on four platforms" without the browser ever gaining
-- a write path it should not have. The web app stays read-only over these
-- tables; only the service role writes them.

-- ---------------------------------------------------------------------------
-- 1. Step Functions callback tokens.
--
-- `productions.execution_arn` carries a comment stating the task token must
-- never reach the browser. This is how that is enforced: `config.toml` sets
-- `schemas = ["public", "graphql_public"]`, so nothing in `private` is
-- reachable through PostgREST at all.
--
-- The corollary is easy to miss: a *service-role* client cannot reach `private`
-- either, because the service key bypasses row-level security, not schema
-- exposure. So the pipeline goes through security-definer functions in
-- `public` that are granted to `service_role` alone.
-- ---------------------------------------------------------------------------
create schema if not exists private;
revoke all on schema private from public;

create table if not exists private.gate_tokens (
  production_id uuid primary key references public.productions (id) on delete cascade,
  task_token    text not null,
  claimed_at    timestamptz,
  created_at    timestamptz not null default now()
);

-- A decision can legitimately arrive before the token is registered: the owner
-- may act in the window between the row becoming visible and the state machine
-- reaching its callback state. Recording it here lets the callback state resume
-- itself immediately instead of waiting for a token nobody will ever redeem.
create table if not exists private.gate_decisions (
  production_id uuid primary key references public.productions (id) on delete cascade,
  decision      text not null check (decision in ('approved', 'rejected')),
  recorded_at   timestamptz not null default now()
);

create or replace function public.set_gate_token(p_production_id uuid, p_token text)
returns void
language plpgsql
security definer
set search_path = public, private
as $$
begin
  insert into private.gate_tokens (production_id, task_token)
  values (p_production_id, p_token)
  on conflict (production_id) do update
    set task_token = excluded.task_token,
        claimed_at = null,
        created_at = now();
end;
$$;

-- Lease rather than delete-then-send. If the send fails we must be able to try
-- again, but two concurrent deliveries must not both call SendTaskSuccess.
-- A null return means "no token, or someone else is mid-send".
create or replace function public.take_gate_token(p_production_id uuid)
returns text
language plpgsql
security definer
set search_path = public, private
as $$
declare
  v_token text;
begin
  update private.gate_tokens
     set claimed_at = now()
   where production_id = p_production_id
     and (claimed_at is null or claimed_at < now() - interval '60 seconds')
  returning task_token into v_token;
  return v_token;
end;
$$;

-- Called only after SendTaskSuccess is confirmed, including when it returns
-- TaskDoesNotExist or TaskTimedOut -- both mean the execution already moved on.
create or replace function public.release_gate_token(p_production_id uuid)
returns void
language plpgsql
security definer
set search_path = public, private
as $$
begin
  delete from private.gate_tokens where production_id = p_production_id;
end;
$$;

create or replace function public.record_gate_decision(p_production_id uuid, p_decision text)
returns void
language plpgsql
security definer
set search_path = public, private
as $$
begin
  insert into private.gate_decisions (production_id, decision)
  values (p_production_id, p_decision)
  on conflict (production_id) do nothing;
end;
$$;

create or replace function public.peek_gate_decision(p_production_id uuid)
returns text
language plpgsql
security definer
set search_path = public, private
as $$
declare
  v_decision text;
begin
  select decision into v_decision
    from private.gate_decisions
   where production_id = p_production_id;
  return v_decision;
end;
$$;

-- Rows awaiting a resume, for the gate reconciler. Supabase Database Webhooks
-- are pg_net: at-most-once, no retry, no dead-letter queue, and a timeout that
-- defaults to one second. A dropped Gate 2 decision is otherwise unrecoverable,
-- because `decide_production` refuses to act once the status has moved on. This
-- view is what makes the webhook an optimisation rather than a correctness
-- dependency.
-- A function rather than a view on purpose. A view in `public` inherits
-- Supabase's default ACLs, so revoking from the PUBLIC pseudo-role would not
-- necessarily remove `authenticated`'s access. A security-definer function
-- grants nothing until we say so.
create or replace function public.list_pending_gate_resumes()
returns table (
  production_id    uuid,
  status           text,
  token_created_at timestamptz,
  claimed_at       timestamptz
)
language sql
security definer
set search_path = public, private
as $$
  select t.production_id,
         p.status,
         t.created_at,
         t.claimed_at
    from private.gate_tokens t
    join public.productions p on p.id = t.production_id
   where p.status in ('approved', 'rejected')
     and (t.claimed_at is null or t.claimed_at < now() - interval '60 seconds');
$$;

revoke all on function public.set_gate_token(uuid, text)       from public;
revoke all on function public.take_gate_token(uuid)            from public;
revoke all on function public.release_gate_token(uuid)         from public;
revoke all on function public.record_gate_decision(uuid, text) from public;
revoke all on function public.peek_gate_decision(uuid)         from public;
revoke all on function public.list_pending_gate_resumes()      from public;

grant execute on function public.set_gate_token(uuid, text)       to service_role;
grant execute on function public.take_gate_token(uuid)            to service_role;
grant execute on function public.release_gate_token(uuid)         to service_role;
grant execute on function public.record_gate_decision(uuid, text) to service_role;
grant execute on function public.peek_gate_decision(uuid)         to service_role;
grant execute on function public.list_pending_gate_resumes()      to service_role;

-- ---------------------------------------------------------------------------
-- 2. `parked` production status.
--
-- The requirement is that a rejection or a quality-check failure parks the job
-- for a human rather than retrying. There was no way to say that: `failed`
-- means the pipeline broke, `rejected` means the owner said no. `parked` means
-- the pipeline stopped deliberately and needs a person.
--
-- The original check is an inline column constraint, so its name is generated.
-- Discover it rather than guessing.
-- ---------------------------------------------------------------------------
do $$
declare
  v_name text;
begin
  select conname into v_name
    from pg_constraint
   where conrelid = 'public.productions'::regclass
     and contype = 'c'
     and pg_get_constraintdef(oid) like '%awaiting_review%';
  if v_name is not null then
    execute format('alter table public.productions drop constraint %I', v_name);
  end if;
end;
$$;

alter table public.productions
  add constraint productions_status_check check (status in (
    'queued', 'running', 'qc_failed', 'awaiting_review',
    'approved', 'rejected', 'publishing', 'published', 'failed', 'parked'
  ));

-- ---------------------------------------------------------------------------
-- 3. `updated_at`, which every reconciler depends on.
--
-- Each sweeper asks "has this row not moved in N minutes?". With only
-- `created_at` they cannot tell a stalled job from a slow one, and would
-- re-touch healthy work in flight.
-- ---------------------------------------------------------------------------
alter table public.productions add column if not exists updated_at timestamptz not null default now();

create or replace function public.touch_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

drop trigger if exists productions_touch_updated_at on public.productions;
create trigger productions_touch_updated_at
  before update on public.productions
  for each row execute function public.touch_updated_at();

create index if not exists productions_status_updated_idx
  on public.productions (status, updated_at);

-- One live production per idea. A parked, failed or rejected production must
-- not block a re-run, so those are excluded.
create unique index if not exists productions_one_live_per_idea
  on public.productions (idea_id)
  where status not in ('rejected', 'failed', 'parked');

-- ---------------------------------------------------------------------------
-- 4. Per-platform publication rows.
--
-- One production fans out to four posts that succeed and fail independently,
-- and a single `productions.status` cannot express the result. This is not
-- hypothetical: YouTube's default Data API quota allows six uploads a day
-- against a target of ten, so posts seven through ten fail on YouTube every
-- day until the quota extension lands. Recording that as either `failed`
-- (three platforms did publish) or `published` (YouTube did not) would both
-- be lies.
-- ---------------------------------------------------------------------------
create table if not exists public.publications (
  id             uuid primary key default gen_random_uuid(),
  production_id  uuid not null references public.productions (id) on delete cascade,
  platform       text not null check (platform in ('instagram', 'tiktok', 'youtube', 'linkedin')),
  integration_id text,
  postiz_post_id text,
  state          text not null default 'pending'
                   check (state in ('pending', 'queued', 'published', 'error', 'skipped')),
  release_url    text,
  error          text,
  attempts       integer not null default 0,
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now(),
  unique (production_id, platform)
);

comment on column public.publications.postiz_post_id is
  'Deterministic id we supply as posts[].value[0].id. Postiz upserts on it, which is what makes a publish retry safe at the row level.';
comment on column public.publications.state is
  'A 200 from POST /public/v1/posts means queued, never published. Only the publish reconciler may set `published`.';

drop trigger if exists publications_touch_updated_at on public.publications;
create trigger publications_touch_updated_at
  before update on public.publications
  for each row execute function public.touch_updated_at();

create index if not exists publications_state_idx on public.publications (state, updated_at);

-- ---------------------------------------------------------------------------
-- 5. Analytics samples.
--
-- Postiz returns AnalyticsData[] as {label, data:[{total, date}]} where the
-- totals are strings and `percentageChange` is frequently a hardcoded
-- placeholder. Keep the raw string alongside a best-effort numeric.
-- ---------------------------------------------------------------------------
create table if not exists public.post_metrics (
  id             uuid primary key default gen_random_uuid(),
  publication_id uuid not null references public.publications (id) on delete cascade,
  captured_at    timestamptz not null default now(),
  label          text not null,
  value          numeric,
  raw_value      text,
  unique (publication_id, captured_at, label)
);

-- ---------------------------------------------------------------------------
-- 6. Which platforms we actually publish to, as data rather than code.
--
-- Both TikTok and YouTube are externally blocked right now, and encoding that
-- here means shipping with two platforms and enabling the others later without
-- touching the state machine.
-- ---------------------------------------------------------------------------
create table if not exists public.platform_targets (
  platform       text primary key check (platform in ('instagram', 'tiktok', 'youtube', 'linkedin')),
  enabled        boolean not null default true,
  daily_cap      integer,
  integration_id text,
  notes          text,
  updated_at     timestamptz not null default now()
);

insert into public.platform_targets (platform, enabled, daily_cap, notes) values
  ('instagram', true,  50,
   'Content Publishing API allows roughly 50 posts/24h. Needs an Instagram Business or Creator account linked to a Facebook Page. Licensed audio cannot be attached via API, so music must be baked into the render.'),
  ('linkedin',  true,  null,
   'Connect a LinkedIn *Page*, not a personal profile: the personal linkedin provider reports no analytics at all.'),
  ('youtube',   false, 6,
   'Disabled until the quota extension lands. Data API v3 grants 10,000 units/day and videos.insert costs 1,600, so the ceiling is 6 uploads/day and the 7th fails rather than queues.'),
  ('tiktok',    false, null,
   'Disabled until the app audit clears. Automated posting requires content_posting_method=DIRECT_POST, which unaudited apps cannot use; UPLOAD only drops media into the user inbox for manual completion within 24h.')
on conflict (platform) do nothing;

drop trigger if exists platform_targets_touch_updated_at on public.platform_targets;
create trigger platform_targets_touch_updated_at
  before update on public.platform_targets
  for each row execute function public.touch_updated_at();

-- ---------------------------------------------------------------------------
-- 7. Let the system write audit rows.
--
-- `approvals.actor_id` was `not null references profiles`, so no automated
-- transition could be recorded -- yet the reconcilers park jobs, expire ideas
-- and force-fail renders, which are exactly the decisions an audit trail
-- should show.
-- ---------------------------------------------------------------------------
alter table public.approvals add column if not exists source text not null default 'human';

alter table public.approvals drop constraint if exists approvals_source_check;
alter table public.approvals add constraint approvals_source_check
  check (source in ('human', 'system'));

alter table public.approvals alter column actor_id drop not null;

-- A human decision must still name its actor; only the system may omit one.
alter table public.approvals drop constraint if exists approvals_actor_required_for_human;
alter table public.approvals add constraint approvals_actor_required_for_human
  check (source <> 'human' or actor_id is not null);

-- ---------------------------------------------------------------------------
-- 8. Row-level security on the new tables.
--
-- Same shape as the existing tables: authenticated users read, nobody writes
-- from the browser. The pipeline uses the service role, which bypasses RLS.
-- ---------------------------------------------------------------------------
alter table public.publications      enable row level security;
alter table public.post_metrics      enable row level security;
alter table public.platform_targets  enable row level security;

drop policy if exists publications_read on public.publications;
create policy publications_read on public.publications
  for select to authenticated using (true);

drop policy if exists post_metrics_read on public.post_metrics;
create policy post_metrics_read on public.post_metrics
  for select to authenticated using (true);

drop policy if exists platform_targets_read on public.platform_targets;
create policy platform_targets_read on public.platform_targets
  for select to authenticated using (true);

-- ---------------------------------------------------------------------------
-- 9. Deactivate the presenter preset until its lane exists.
--
-- The seeded `heygen` preset is approvable at Gate 1 today, but the state
-- machine has only a MoneyPrinterTurbo path, so approving it would strand the
-- job. Re-enable this when the HeyGen lane is built.
-- ---------------------------------------------------------------------------
update public.style_presets set is_active = false where video_source = 'heygen';
