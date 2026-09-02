-- =============================================================================
-- Reels approval queue — Gate 1 (idea + style) and Gate 2 (finished cut)
-- =============================================================================
--
-- This app is a client-only SPA: there is no server tier holding a service-role
-- key, so *every* rule that matters lives here. Row-level security is the
-- access control, and the gate decisions are Postgres functions so that
-- "record the decision" and "write the audit row" cannot come apart.
--
-- Apply via the Supabase dashboard SQL editor, or `supabase db push`.
-- =============================================================================

create extension if not exists "pgcrypto";

-- -----------------------------------------------------------------------------
-- profiles — who may approve
-- -----------------------------------------------------------------------------
create table if not exists public.profiles (
  id           uuid primary key references auth.users (id) on delete cascade,
  email        text not null,
  display_name text,
  avatar_url   text,
  role         text not null default 'viewer' check (role in ('owner', 'viewer')),
  created_at   timestamptz not null default now()
);

comment on column public.profiles.role is
  'owner may pass either gate; viewer has read-only access to the queue.';

-- New auth users land as viewers. Promote deliberately:
--   update public.profiles set role = 'owner' where email = '...';
create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  insert into public.profiles (id, email, display_name, avatar_url)
  values (
    new.id,
    new.email,
    coalesce(new.raw_user_meta_data ->> 'full_name', new.raw_user_meta_data ->> 'name'),
    coalesce(new.raw_user_meta_data ->> 'avatar_url', new.raw_user_meta_data ->> 'picture')
  )
  on conflict (id) do nothing;
  return new;
end;
$$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function public.handle_new_user();

create or replace function public.is_owner()
returns boolean
language sql
stable
security definer
set search_path = public
as $$
  select exists (
    select 1 from public.profiles
    where id = auth.uid() and role = 'owner'
  );
$$;

-- -----------------------------------------------------------------------------
-- style_presets — the Gate 1 choices, each with the cost and ETA shown
--                 at the moment of choosing
-- -----------------------------------------------------------------------------
create table if not exists public.style_presets (
  id               uuid primary key default gen_random_uuid(),
  slug             text not null unique,
  name             text not null,
  description      text,
  lane             text not null check (lane in ('stock', 'generative', 'presenter')),
  video_source     text not null,
  est_cost_min_usd numeric(10, 4) not null,
  est_cost_max_usd numeric(10, 4) not null,
  est_minutes      integer not null,
  params           jsonb not null default '{}'::jsonb,
  is_active        boolean not null default true,
  sort_order       integer not null default 0,
  created_at       timestamptz not null default now(),
  constraint style_presets_cost_range check (est_cost_max_usd >= est_cost_min_usd)
);

comment on column public.style_presets.params is
  'MoneyPrinterTurbo VideoParams overrides for this preset (voice, subtitle styling, concat mode, ...).';

-- -----------------------------------------------------------------------------
-- ideas — stage 2 output, Gate 1 input
-- -----------------------------------------------------------------------------
create table if not exists public.ideas (
  id                uuid primary key default gen_random_uuid(),
  title             text not null,
  hook              text,
  angle             text,
  rationale         text,
  source            text,
  source_url        text,
  trend_keyword     text,
  velocity_ratio    numeric(6, 2),
  velocity_label    text check (velocity_label in ('breakout', 'rising', 'steady', 'declining')),
  target_platforms  text[] not null default '{}',
  status            text not null default 'pending'
                      check (status in ('pending', 'approved', 'rejected', 'expired')),
  approved_style_id uuid references public.style_presets (id),
  decided_by        uuid references public.profiles (id),
  decided_at        timestamptz,
  decision_note     text,
  created_at        timestamptz not null default now()
);

comment on column public.ideas.velocity_ratio is
  'Baseline-relative trend velocity: current window vs. the channel''s own baseline. >= 3.0 breakout, >= 1.5 rising, >= 0.8 steady.';

create index if not exists ideas_status_created_idx
  on public.ideas (status, created_at desc);

-- An approved idea must name the style it was approved with, and a pending one
-- must not have a style yet. `decided_by` is deliberately not required: rows
-- backfilled by the pipeline have no profile to attribute the decision to.
alter table public.ideas drop constraint if exists ideas_style_required_when_approved;
alter table public.ideas add constraint ideas_style_required_when_approved check (
  case status
    when 'approved' then approved_style_id is not null
    when 'pending'  then approved_style_id is null
    else true
  end
);

-- -----------------------------------------------------------------------------
-- productions — stages 4-9, Gate 2 input
-- -----------------------------------------------------------------------------
create table if not exists public.productions (
  id                uuid primary key default gen_random_uuid(),
  idea_id           uuid not null references public.ideas (id) on delete cascade,
  style_preset_id   uuid not null references public.style_presets (id),
  status            text not null default 'queued' check (status in (
                      'queued', 'running', 'qc_failed', 'awaiting_review',
                      'approved', 'rejected', 'publishing', 'published', 'failed'
                    )),
  stage             text,
  task_id           text,
  execution_arn     text,
  script            text,
  video_url         text,
  thumbnail_url     text,
  duration_seconds  numeric(6, 2),
  qc                jsonb not null default '{}'::jsonb,
  platform_copy     jsonb not null default '{}'::jsonb,
  cost_estimate_usd numeric(10, 4),
  cost_actual_usd   numeric(10, 4),
  error             text,
  decided_by        uuid references public.profiles (id),
  decided_at        timestamptz,
  decision_note     text,
  created_at        timestamptz not null default now(),
  completed_at      timestamptz
);

comment on column public.productions.qc is
  'Automated quality check: {"passed": bool, "slideshow_risk": num, "checks": [{"key","label","status","detail"}]}. Written by the QC service before the job reaches Gate 2.';
comment on column public.productions.platform_copy is
  'Per-platform copy variants: {"instagram": {"caption": "..."}, "youtube": {"title": "...", "description": "..."}, ...}.';
comment on column public.productions.execution_arn is
  'Step Functions execution this job belongs to. The task token itself is never stored here — it must not reach the browser.';

create index if not exists productions_status_created_idx
  on public.productions (status, created_at desc);
create index if not exists productions_idea_idx
  on public.productions (idea_id);

-- -----------------------------------------------------------------------------
-- approvals — one immutable audit row per gate decision
-- -----------------------------------------------------------------------------
create table if not exists public.approvals (
  id              uuid primary key default gen_random_uuid(),
  gate            smallint not null check (gate in (1, 2)),
  subject_type    text not null check (subject_type in ('idea', 'production')),
  subject_id      uuid not null,
  decision        text not null check (decision in ('approved', 'rejected')),
  note            text,
  style_preset_id uuid references public.style_presets (id),
  actor_id        uuid not null references public.profiles (id),
  created_at      timestamptz not null default now()
);

create index if not exists approvals_subject_idx
  on public.approvals (subject_type, subject_id, created_at desc);

-- =============================================================================
-- Row-level security
-- =============================================================================
alter table public.profiles      enable row level security;
alter table public.style_presets enable row level security;
alter table public.ideas         enable row level security;
alter table public.productions   enable row level security;
alter table public.approvals     enable row level security;

drop policy if exists profiles_read on public.profiles;
create policy profiles_read on public.profiles
  for select to authenticated using (true);

drop policy if exists profiles_update_own on public.profiles;
create policy profiles_update_own on public.profiles
  for update to authenticated using (id = auth.uid()) with check (id = auth.uid());

-- The policy above lets a user edit their own display name and avatar. It must
-- not let them promote themselves to owner, and a policy predicate cannot
-- compare against the pre-update row, so the guard is a trigger.
create or replace function public.guard_profile_role()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  if new.role is distinct from old.role and not public.is_owner() then
    raise exception 'Only an owner may change a role' using errcode = '42501';
  end if;
  return new;
end;
$$;

drop trigger if exists profiles_guard_role on public.profiles;
create trigger profiles_guard_role
  before update on public.profiles
  for each row execute function public.guard_profile_role();

drop policy if exists style_presets_read on public.style_presets;
create policy style_presets_read on public.style_presets
  for select to authenticated using (true);

drop policy if exists style_presets_write on public.style_presets;
create policy style_presets_write on public.style_presets
  for all to authenticated using (public.is_owner()) with check (public.is_owner());

drop policy if exists ideas_read on public.ideas;
create policy ideas_read on public.ideas
  for select to authenticated using (true);

drop policy if exists productions_read on public.productions;
create policy productions_read on public.productions
  for select to authenticated using (true);

drop policy if exists approvals_read on public.approvals;
create policy approvals_read on public.approvals
  for select to authenticated using (true);

-- Note there is deliberately no INSERT/UPDATE policy for `ideas`,
-- `productions` or `approvals`. The browser must not be able to move a job
-- through the pipeline by hand; it may only call the two gate functions below,
-- which run as the table owner and enforce the rules themselves. The pipeline
-- writes to these tables from AWS with the service-role key, which bypasses RLS.

-- =============================================================================
-- Gate decisions
-- =============================================================================

-- Gate 1: approve an idea and commit to a production style.
create or replace function public.approve_idea(
  p_idea_id  uuid,
  p_style_id uuid,
  p_note     text default null
)
returns public.ideas
language plpgsql
security definer
set search_path = public
as $$
declare
  v_idea public.ideas;
begin
  if not public.is_owner() then
    raise exception 'Only an owner may approve an idea' using errcode = '42501';
  end if;

  if not exists (select 1 from public.style_presets where id = p_style_id and is_active) then
    raise exception 'Unknown or inactive style preset' using errcode = '22023';
  end if;

  update public.ideas
     set status            = 'approved',
         approved_style_id = p_style_id,
         decided_by        = auth.uid(),
         decided_at        = now(),
         decision_note     = p_note
   where id = p_idea_id
     and status = 'pending'
  returning * into v_idea;

  if v_idea.id is null then
    raise exception 'Idea % is not pending', p_idea_id using errcode = 'P0002';
  end if;

  insert into public.approvals (gate, subject_type, subject_id, decision, note, style_preset_id, actor_id)
  values (1, 'idea', p_idea_id, 'approved', p_note, p_style_id, auth.uid());

  return v_idea;
end;
$$;

-- Gate 1: reject an idea. Nothing is spent.
create or replace function public.reject_idea(
  p_idea_id uuid,
  p_note    text default null
)
returns public.ideas
language plpgsql
security definer
set search_path = public
as $$
declare
  v_idea public.ideas;
begin
  if not public.is_owner() then
    raise exception 'Only an owner may reject an idea' using errcode = '42501';
  end if;

  update public.ideas
     set status        = 'rejected',
         decided_by    = auth.uid(),
         decided_at    = now(),
         decision_note = p_note
   where id = p_idea_id
     and status = 'pending'
  returning * into v_idea;

  if v_idea.id is null then
    raise exception 'Idea % is not pending', p_idea_id using errcode = 'P0002';
  end if;

  insert into public.approvals (gate, subject_type, subject_id, decision, note, actor_id)
  values (1, 'idea', p_idea_id, 'rejected', p_note, auth.uid());

  return v_idea;
end;
$$;

-- Gate 2: sign off (or reject) a finished cut.
create or replace function public.decide_production(
  p_production_id uuid,
  p_decision      text,
  p_note          text default null
)
returns public.productions
language plpgsql
security definer
set search_path = public
as $$
declare
  v_production public.productions;
begin
  if not public.is_owner() then
    raise exception 'Only an owner may sign off a cut' using errcode = '42501';
  end if;

  if p_decision not in ('approved', 'rejected') then
    raise exception 'decision must be approved or rejected' using errcode = '22023';
  end if;

  if p_decision = 'rejected' and coalesce(btrim(p_note), '') = '' then
    raise exception 'A rejection must say why' using errcode = '22023';
  end if;

  update public.productions
     set status        = p_decision,
         decided_by    = auth.uid(),
         decided_at    = now(),
         decision_note = p_note
   where id = p_production_id
     and status in ('awaiting_review', 'qc_failed')
  returning * into v_production;

  if v_production.id is null then
    raise exception 'Production % is not awaiting review', p_production_id using errcode = 'P0002';
  end if;

  insert into public.approvals (gate, subject_type, subject_id, decision, note, actor_id)
  values (2, 'production', p_production_id, p_decision, p_note, auth.uid());

  return v_production;
end;
$$;

revoke all on function public.approve_idea(uuid, uuid, text) from public;
revoke all on function public.reject_idea(uuid, text) from public;
revoke all on function public.decide_production(uuid, text, text) from public;
grant execute on function public.approve_idea(uuid, uuid, text) to authenticated;
grant execute on function public.reject_idea(uuid, text) to authenticated;
grant execute on function public.decide_production(uuid, text, text) to authenticated;
