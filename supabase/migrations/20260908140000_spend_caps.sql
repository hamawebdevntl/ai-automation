-- =============================================================================
-- Spend caps — a ceiling in dollars, per provider and per model, daily and
-- monthly, with the measured cost of every render behind it.
-- =============================================================================
--
-- Nothing capped spend before this. The three ceilings that existed --
-- `MPT_RENDER_BUDGET_SECONDS`, `FAL_POLL_BUDGET_SECONDS`,
-- `HEYGEN_POLL_BUDGET_SECONDS` -- are wall-clock budgets that stop a *stranded*
-- render, not a costly one, and HeyGen's 429 is a throttle rather than a
-- budget. Cost existed only as `style_presets.est_cost_min_usd` /
-- `est_cost_max_usd`: a figure shown to a human at Gate 1, with no row anywhere
-- recording what a render actually cost. `docs/GAPS.md` section C names the gap
-- directly.
--
-- The exposure is asymmetric, which is why this is a ceiling and not a warning.
-- From the README's own figures an all-presenter week runs $300-600/month
-- against $30-120 for stock, and the fal audio-native premium tiers were
-- rejected as a default precisely because they would cost $1,500-5,400/month at
-- ten reels a day.
--
-- Four objects carry the feature:
--
--   `render_spend`  the ledger. One row per billed thing, and the only record
--                   of what a render cost. `productions.cost_actual_usd` is
--                   kept as a projection of it by trigger, so the column can
--                   no longer drift from the rows behind it.
--   `spend_rates`   per-model unit pricing, for the providers that bill by the
--                   second or the character and therefore report no figure of
--                   their own.
--   `spend_caps`    the policy: a daily and a monthly limit for a provider, or
--                   for one model on it. Rows, not columns, so a new model's
--                   ceiling is an INSERT rather than a migration.
--   `spend_block_reason()`  the single verdict, in one sentence. Gate 1 and the
--                   render step both call it, which is what stops the browser
--                   and the pipeline from disagreeing about whether a style can
--                   be paid for.
--
-- Both windows are enforced, and that is deliberate: a monthly figure alone
-- lets a runaway loop burn the month's budget in an hour, and a daily figure
-- alone lets thirty ordinary days add up to a bill nobody agreed to.
-- =============================================================================


-- ---------------------------------------------------------------------------
-- The two windows
-- ---------------------------------------------------------------------------
--
-- Pinned to UTC rather than to the session's TimeZone. A cap that reset at
-- whatever midnight the reader's browser believed in would give two people
-- looking at the same Settings page two different answers, and would move the
-- boundary under the pipeline every time a worker was deployed somewhere else.
create or replace function public.spend_window_start(p_window text)
returns timestamptz
language sql
-- Stable, not immutable: it reads `now()`. Declaring it immutable would let
-- the planner fold today's boundary into a cached plan and go on comparing
-- against it tomorrow.
stable
as $$
  select case p_window
           when 'day'   then date_trunc('day',   now() at time zone 'utc') at time zone 'utc'
           when 'month' then date_trunc('month', now() at time zone 'utc') at time zone 'utc'
         end;
$$;

comment on function public.spend_window_start(text) is
  'Start of the current day or month in UTC. The boundary both caps are measured from.';


-- ---------------------------------------------------------------------------
-- The ledger
-- ---------------------------------------------------------------------------

create table if not exists public.render_spend (
  id            uuid        primary key default gen_random_uuid(),
  -- Cascading, like every other production-scoped table here. It is worth
  -- saying why, because a ledger keeping its rows forever is the more usual
  -- instinct: nothing in this system deletes a production -- a re-run
  -- supersedes and a cancel parks, and there is no DELETE policy on the table
  -- at all -- so the cascade is for a deliberate cleanup by hand, where a
  -- surviving charge would count against a cap with nothing left to explain it.
  production_id uuid        not null references public.productions (id) on delete cascade,
  -- 'heygen', 'fal', 'mpt'. Not a foreign key to anything: a provider is a
  -- fact about where money went, and it must stay recorded after the preset
  -- that chose it has been edited or deleted.
  provider      text        not null check (provider <> ''),
  -- Empty when the provider bills as a whole rather than per model.
  model         text        not null default '',
  -- What was billed. A fal end-to-end reel bills twice -- the video model and
  -- the TTS model -- so a production is not one charge.
  kind          text        not null default 'render'
                            check (kind in ('render', 'tts', 'transcribe', 'source')),
  amount_usd    numeric(12, 4) not null check (amount_usd >= 0),
  -- 'reported' is the provider's own figure and always wins; 'derived' is ours,
  -- computed from `spend_rates`. Recorded rather than inferred, because
  -- "$1.40, measured" and "$1.40, we think" are different claims and the
  -- second must not be able to masquerade as the first.
  source        text        not null check (source in ('reported', 'derived')),
  -- The provider's own identifier for the billed thing, where the provider
  -- has one and we need it to tell two charges apart. Empty otherwise, which
  -- is the ordinary case.
  --
  -- It exists for fal, and only for fal. Two of the three backends make a
  -- resubmit free: our MoneyPrinterTurbo fork accepts a caller-supplied task
  -- id and `POST /v3/videos` takes an `Idempotency-Key`, and both are given the
  -- production id, so however many times a submit is retried the account is
  -- billed once. fal has no idempotency key of any kind -- every successful
  -- `submit` is a fresh billed generation -- so one production can genuinely
  -- owe for two, and its `request_id` is the only thing that tells them apart.
  --
  -- A submit attempt number would not do. It is not durable: the driver strips
  -- `attempts` from the payload it hands an activity, and `retry_production`
  -- clears the counter for the step it is retrying, so the number would be
  -- zero on the retry that bills the second time.
  external_ref  text        not null default '',
  detail        jsonb       not null default '{}'::jsonb,
  spent_at      timestamptz not null default now(),
  created_at    timestamptz not null default now(),

  -- Idempotency, and the reason a retried poll cannot double-count. Every
  -- write from the pipeline is an upsert on this key, which is also how a
  -- derived figure is replaced by the provider's reported one later.
  constraint render_spend_once unique (production_id, provider, model, kind, external_ref)
);

comment on table public.render_spend is
  'One row per billed thing. The only record of what a render actually cost.';
comment on column public.render_spend.source is
  'reported = the provider''s own figure. derived = computed from spend_rates.';
comment on column public.render_spend.spent_at is
  'When the money moved, which is what the daily and monthly windows are measured against.';
comment on column public.render_spend.external_ref is
  'The provider''s id for this charge. Empty except on fal, whose request_id is what tells two billed generations apart.';

-- The window sums are the hot read: one per cap on every Gate 1 render and
-- every submit.
create index if not exists render_spend_window_idx
  on public.render_spend (provider, spent_at desc, model);
create index if not exists render_spend_production_idx
  on public.render_spend (production_id);


-- `cost_actual_usd` was on `productions` from the first migration and, as its
-- own type comment said, was "never written by the pipeline today ... so it is
-- reliably null". It is a projection of the ledger now rather than a second
-- place to write, so the total on a production cannot disagree with the rows
-- that make it up.
create or replace function public.sync_production_cost()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
declare
  v_production uuid := coalesce(new.production_id, old.production_id);
begin
  update public.productions
     set cost_actual_usd = (
           select coalesce(sum(amount_usd), 0)
             from public.render_spend
            where production_id = v_production
         )
   where id = v_production;
  return null;
end;
$$;

drop trigger if exists render_spend_sync_production_cost on public.render_spend;
create trigger render_spend_sync_production_cost
  after insert or update or delete on public.render_spend
  for each row execute function public.sync_production_cost();

comment on column public.productions.cost_actual_usd is
  'The sum of this production''s render_spend rows, maintained by trigger. Null until something is billed.';


-- ---------------------------------------------------------------------------
-- Rates
-- ---------------------------------------------------------------------------
--
-- HeyGen bills a wallet and reports a balance, so its figure can be read back.
-- fal bills per second of output and reports nothing, so its cost has to be
-- derived from a rate and a duration -- hence a table rather than a constant,
-- because a published price changes and a redeploy is the wrong way to follow
-- it.
create table if not exists public.spend_rates (
  provider   text not null check (provider <> ''),
  model      text not null default '',
  unit       text not null check (unit in ('second', 'character', 'clip', 'render')),
  rate_usd   numeric(12, 6) not null check (rate_usd >= 0),
  note       text,
  updated_at timestamptz not null default now(),
  updated_by uuid references auth.users (id) on delete set null,

  primary key (provider, model)
);

comment on table public.spend_rates is
  'Unit pricing per provider and model, for the providers that report no figure of their own.';
comment on column public.spend_rates.unit is
  'What rate_usd is per: a second of output, a character of narration, a generated clip, or a whole render.';

drop trigger if exists spend_rates_touch_updated_at on public.spend_rates;
create trigger spend_rates_touch_updated_at
  before update on public.spend_rates
  for each row execute function public.touch_updated_at();


-- ---------------------------------------------------------------------------
-- Caps
-- ---------------------------------------------------------------------------
--
-- Rows keyed by (provider, model) rather than columns on a settings singleton,
-- which is what makes "editable per provider and per model without a
-- migration" true: a new model's ceiling is an INSERT an owner can make from
-- Settings.
--
-- An empty `model` is the provider as a whole, and it counts spend across every
-- model on that provider. A model row is the tighter statement and is checked
-- first, so "fal is capped at $30/day, and the expensive model within that at
-- $10/day" is expressible as two rows.
create table if not exists public.spend_caps (
  provider          text not null check (provider <> ''),
  model             text not null default '',
  -- Null is "no ceiling on this window", not zero -- zero is a real and useful
  -- value meaning "spend nothing", which is how a provider is switched off
  -- without deleting the row that records what it was allowed.
  daily_limit_usd   numeric(10, 2) check (daily_limit_usd   >= 0),
  monthly_limit_usd numeric(10, 2) check (monthly_limit_usd >= 0),
  is_active         boolean     not null default true,
  note              text,
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now(),
  updated_by        uuid references auth.users (id) on delete set null,

  primary key (provider, model),

  -- A row with neither limit caps nothing and reads as though it did, which is
  -- the one state worth refusing outright.
  constraint spend_caps_needs_a_limit check (
    daily_limit_usd is not null or monthly_limit_usd is not null
  ),
  -- A daily ceiling above the monthly one can never bind, so it is a typo
  -- rather than a policy.
  constraint spend_caps_daily_within_monthly check (
    daily_limit_usd is null
    or monthly_limit_usd is null
    or daily_limit_usd <= monthly_limit_usd
  )
);

comment on table public.spend_caps is
  'Daily and monthly USD ceilings, per provider and per model. Rows, so a new ceiling needs no migration.';
comment on column public.spend_caps.model is
  'Empty means the provider as a whole, counting every model on it. A named model is the tighter cap and is reported first.';
comment on column public.spend_caps.is_active is
  'False keeps the numbers on record while letting spend through. Preferred over deleting a cap.';

drop trigger if exists spend_caps_touch_updated_at on public.spend_caps;
create trigger spend_caps_touch_updated_at
  before update on public.spend_caps
  for each row execute function public.touch_updated_at();


-- ---------------------------------------------------------------------------
-- What has been spent
-- ---------------------------------------------------------------------------
--
-- `security definer` so that the verdict is the same for everyone who can be
-- refused by it. What it exposes is an aggregate over rows an authenticated
-- reader can already select; what it avoids is a viewer being told a style is
-- available because a policy hid the spend that closed it.
create or replace function public.spend_since(
  p_provider text,
  p_model    text,
  p_from     timestamptz
)
returns numeric
language sql
stable
security definer
set search_path = public
as $$
  select coalesce(sum(amount_usd), 0)::numeric
    from public.render_spend
   where provider = p_provider
     and spent_at >= p_from
     -- An empty model is the provider as a whole: every model on it counts
     -- towards the provider's ceiling.
     and (coalesce(p_model, '') = '' or model = p_model);
$$;

comment on function public.spend_since(text, text, timestamptz) is
  'USD billed to a provider (or one of its models) since a moment. An empty model sums every model.';


-- Every cap with its current standing. The Settings card reads this directly;
-- `spend_block_reason` reads it to compose its sentence.
create or replace view public.spend_cap_status
with (security_invoker = true) as
select
  c.provider,
  c.model,
  c.daily_limit_usd,
  c.monthly_limit_usd,
  c.is_active,
  c.note,
  s.day_spent_usd,
  s.month_spent_usd,
  c.is_active
    and c.daily_limit_usd is not null
    and s.day_spent_usd >= c.daily_limit_usd                             as daily_reached,
  c.is_active
    and c.monthly_limit_usd is not null
    and s.month_spent_usd >= c.monthly_limit_usd                         as monthly_reached,
  public.spend_window_start('day')   + interval '1 day'                  as daily_resets_at,
  public.spend_window_start('month') + interval '1 month'                as monthly_resets_at,
  c.updated_at,
  c.updated_by
from public.spend_caps c
cross join lateral (
  select public.spend_since(c.provider, c.model, public.spend_window_start('day'))   as day_spent_usd,
         public.spend_since(c.provider, c.model, public.spend_window_start('month')) as month_spent_usd
) s;

comment on view public.spend_cap_status is
  'Every cap with what has been spent against it today and this month, and whether it is reached.';


-- ---------------------------------------------------------------------------
-- The verdict
-- ---------------------------------------------------------------------------
--
-- One function, called from both enforcement points. Gate 1 (`approve_idea`)
-- refuses a style whose cap is spent, and the render step refuses to submit --
-- and they must never disagree, which they would if each carried its own copy
-- of the arithmetic. The browser ships as a static bundle, so a guard written
-- in TypeScript is a guard an old tab can skip; this is the one that holds.
--
-- Returns null when there is nothing to say, and otherwise a sentence meant to
-- be shown verbatim: which cap, how much of it is gone, and when it comes back.
create or replace function public.spend_block_reason(
  p_provider text,
  p_model    text default ''
)
returns text
language plpgsql
stable
security definer
set search_path = public
as $$
declare
  v_cap   record;
  v_label text;
begin
  if coalesce(p_provider, '') = '' then
    return null;
  end if;

  for v_cap in
    select *
      from public.spend_cap_status
     where provider = p_provider
       and model in ('', coalesce(p_model, ''))
       and is_active
     -- The model's own cap first. It is the more specific statement about the
     -- render being attempted, and naming it is more use than naming the
     -- provider ceiling it happens to sit inside.
     order by (model <> '') desc
  loop
    v_label := case when v_cap.model = '' then v_cap.provider
                    else v_cap.provider || ' / ' || v_cap.model end;

    if v_cap.daily_reached then
      return format(
        'Daily spend cap reached for %s: $%s of $%s today. It resets %s.',
        v_label,
        to_char(v_cap.day_spent_usd,  'FM999999990.00'),
        to_char(v_cap.daily_limit_usd, 'FM999999990.00'),
        to_char(v_cap.daily_resets_at at time zone 'utc', 'YYYY-MM-DD HH24:MI" UTC"')
      );
    end if;

    if v_cap.monthly_reached then
      return format(
        'Monthly spend cap reached for %s: $%s of $%s this month. It resets %s.',
        v_label,
        to_char(v_cap.month_spent_usd,  'FM999999990.00'),
        to_char(v_cap.monthly_limit_usd, 'FM999999990.00'),
        to_char(v_cap.monthly_resets_at at time zone 'utc', 'YYYY-MM-DD HH24:MI" UTC"')
      );
    end if;
  end loop;

  return null;
end;
$$;

comment on function public.spend_block_reason(text, text) is
  'Null when a render may be paid for, otherwise the sentence saying which cap is spent and when it resets.';


-- ---------------------------------------------------------------------------
-- Which provider and model a style spends on
-- ---------------------------------------------------------------------------
--
-- The mapping lives here rather than in the pipeline and again in the browser.
-- `render_mode` decides the provider; the model is wherever that provider's
-- preset config names one. Getting this wrong would cap the wrong thing
-- silently, so it is one definition that both callers read.
create or replace function public.spend_provider(p_render_mode text)
returns text
language sql
immutable
as $$
  select case coalesce(p_render_mode, 'mpt')
           when 'heygen'      then 'heygen'
           when 'fal_visuals' then 'fal'
           when 'fal_full'    then 'fal'
           else                    'mpt'
         end;
$$;

comment on function public.spend_provider(text) is
  'The billing provider behind a style_presets.render_mode.';

create or replace function public.spend_model(
  p_render_mode  text,
  p_params       jsonb,
  p_video_source text
)
returns text
language sql
immutable
as $$
  select coalesce(
    nullif(
      case public.spend_provider(p_render_mode)
        -- The generation model, which is what fal bills by the second of.
        when 'fal'    then coalesce(p_params, '{}'::jsonb) -> 'fal' ->> 'model'
        -- HeyGen bills per second of finished video and its rate varies by
        -- engine, so the engine is the closest thing it has to a model. Most
        -- presets leave it unset, which is the account default and caps under
        -- the provider row.
        when 'heygen' then coalesce(p_params, '{}'::jsonb) -> 'heygen' ->> 'engine'
        -- MoneyPrinterTurbo is an assembler, not a billed model. What costs
        -- money on that lane is where the footage comes from: pexels, pixabay
        -- and coverr are free, wavespeed and volcengine_seedance bill per
        -- request.
        else               p_video_source
      end,
      ''
    ),
    ''
  );
$$;

comment on function public.spend_model(text, jsonb, text) is
  'The billed model behind a style preset: the fal model, the HeyGen engine, or the MPT footage source.';


-- Every style with the cap that governs it and what its renders have actually
-- cost. Gate 1 reads this: `block_reason` is what makes a style unpickable,
-- and `measured_avg_usd` is what replaces the estimate once real renders
-- exist. Until then `render_count` is zero and the preset's own range is all
-- there is -- the presenter lane's $1-2 is, in its own migration's words, "an
-- estimate and not yet a measurement".
create or replace view public.style_preset_spend
with (security_invoker = true) as
select
  p.id   as style_preset_id,
  p.slug,
  k.provider,
  k.model,
  public.spend_block_reason(k.provider, k.model) as block_reason,
  coalesce(m.render_count, 0)                    as render_count,
  m.measured_avg_usd,
  m.measured_min_usd,
  m.measured_max_usd
from public.style_presets p
cross join lateral (
  select public.spend_provider(p.render_mode)                             as provider,
         public.spend_model(p.render_mode, p.params, p.video_source)       as model
) k
left join lateral (
  select count(*)::int              as render_count,
         round(avg(total), 4)       as measured_avg_usd,
         min(total)                 as measured_min_usd,
         max(total)                 as measured_max_usd
    from (
      select rs.production_id, sum(rs.amount_usd) as total
        from public.render_spend rs
        join public.productions pr on pr.id = rs.production_id
       where pr.style_preset_id = p.id
         -- Only productions that produced a file.
         --
         -- A cap counts *committed* spend, which deliberately includes a
         -- render that was submitted and then failed -- over-counting stops
         -- renders that would have been affordable, and an owner can raise a
         -- ceiling, where under-counting bills for renders nobody authorised.
         -- What this figure answers is a different question: "what does a reel
         -- in this style cost?" A generation that was billed and delivered
         -- nothing is not an answer to that, and averaging it in would quietly
         -- make every lane look more expensive than it is.
         and pr.video_url is not null
       group by rs.production_id
    ) per_production
) m on true;

comment on view public.style_preset_spend is
  'Each style with the cap governing it and what its delivered renders were billed. Gate 1 reads block_reason.';


-- ---------------------------------------------------------------------------
-- Gate 1
-- ---------------------------------------------------------------------------
--
-- Re-created from 20260902170000 with one check added: a style whose provider
-- or model is at its ceiling cannot be approved. The Gate 1 UI disables it and
-- says which cap was hit, but this is what actually refuses -- for the same
-- reason every other rule here lives in Postgres. Nothing should enter the
-- pipeline that cannot be paid for.
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
  v_idea    public.ideas;
  v_blocked text;
begin
  if not public.is_owner() then
    raise exception 'Only an owner may approve an idea' using errcode = '42501';
  end if;

  if not exists (select 1 from public.style_presets where id = p_style_id and is_active) then
    raise exception 'Unknown or inactive style preset' using errcode = '22023';
  end if;

  -- Checked before the idea is moved, so a refusal leaves it pending and
  -- re-approvable under a different style rather than stranding it.
  select block_reason
    into v_blocked
    from public.style_preset_spend
   where style_preset_id = p_style_id;

  if v_blocked is not null then
    raise exception 'This style cannot be approved. %', v_blocked using errcode = '54000';
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


-- ---------------------------------------------------------------------------
-- Access
-- ---------------------------------------------------------------------------
--
-- The same split as `style_presets` and `trend_settings`: everyone signed in
-- can read, only an owner can change. Caps and rates get INSERT and DELETE as
-- well as UPDATE, because a ceiling for a model nobody has priced yet has to
-- be creatable without a migration -- that is the whole point of them being
-- rows.
alter table public.spend_caps  enable row level security;
alter table public.spend_rates enable row level security;
alter table public.render_spend enable row level security;

drop policy if exists spend_caps_read on public.spend_caps;
create policy spend_caps_read on public.spend_caps
  for select to authenticated using (true);

drop policy if exists spend_caps_insert on public.spend_caps;
create policy spend_caps_insert on public.spend_caps
  for insert to authenticated with check (public.is_owner());

drop policy if exists spend_caps_update on public.spend_caps;
create policy spend_caps_update on public.spend_caps
  for update to authenticated using (public.is_owner()) with check (public.is_owner());

drop policy if exists spend_caps_delete on public.spend_caps;
create policy spend_caps_delete on public.spend_caps
  for delete to authenticated using (public.is_owner());

drop policy if exists spend_rates_read on public.spend_rates;
create policy spend_rates_read on public.spend_rates
  for select to authenticated using (true);

drop policy if exists spend_rates_insert on public.spend_rates;
create policy spend_rates_insert on public.spend_rates
  for insert to authenticated with check (public.is_owner());

drop policy if exists spend_rates_update on public.spend_rates;
create policy spend_rates_update on public.spend_rates
  for update to authenticated using (public.is_owner()) with check (public.is_owner());

drop policy if exists spend_rates_delete on public.spend_rates;
create policy spend_rates_delete on public.spend_rates
  for delete to authenticated using (public.is_owner());

-- Read-only from the browser, exactly like `production_events`: the ledger is
-- written by the worker with the service-role key, and there is deliberately no
-- write policy, so no signed-in session can invent or erase a charge.
drop policy if exists render_spend_read on public.render_spend;
create policy render_spend_read on public.render_spend
  for select to authenticated using (true);

grant select on public.spend_caps, public.spend_rates, public.render_spend to authenticated;
grant insert, update, delete on public.spend_caps  to authenticated;
grant insert, update, delete on public.spend_rates to authenticated;
grant select on public.spend_cap_status, public.style_preset_spend to authenticated;

-- The verdict and its helpers. Executable by a signed-in session because the
-- views above call them, and revoked from `public` for the same reason the gate
-- functions are: EXECUTE to PUBLIC is the default, and a `security definer`
-- function left at the default is one anon can call.
revoke all on function public.spend_window_start(text) from public;
revoke all on function public.spend_since(text, text, timestamptz) from public;
revoke all on function public.spend_block_reason(text, text) from public;
revoke all on function public.spend_provider(text) from public;
revoke all on function public.spend_model(text, jsonb, text) from public;
revoke all on function public.sync_production_cost() from public;

grant execute on function public.spend_window_start(text) to authenticated, service_role;
grant execute on function public.spend_since(text, text, timestamptz) to authenticated, service_role;
grant execute on function public.spend_block_reason(text, text) to authenticated, service_role;
grant execute on function public.spend_provider(text) to authenticated, service_role;
grant execute on function public.spend_model(text, jsonb, text) to authenticated, service_role;


-- ---------------------------------------------------------------------------
-- Seeds
-- ---------------------------------------------------------------------------
--
-- Rates, from the published per-second and per-character prices the presets
-- were costed against. They are seeded rather than hardcoded because a price
-- changes and following it should not need a deploy -- and because the derived
-- figures they produce are placeholders for measurement, not the point. Once a
-- lane has really rendered, `style_preset_spend.measured_avg_usd` is the number
-- Gate 1 shows and these only fill the gap until then.
insert into public.spend_rates (provider, model, unit, rate_usd, note)
values
  -- 30 seconds of output at $0.04/s is $1.20, inside the $0.80-1.80 range the
  -- fal-generative preset carries.
  ('fal', 'fal-ai/ltx-2.3/text-to-video/fast', 'second', 0.040000,
   'Published per-second rate for the fast tier. The default generative lane.'),
  -- And $2.40 for the standard tier, inside its $1.60-3.20.
  ('fal', 'fal-ai/ltx-2.3/text-to-video', 'second', 0.080000,
   'Published per-second rate for the standard tier, used by the end-to-end lane.'),
  ('fal', 'fal-ai/elevenlabs/tts/turbo-v2.5', 'character', 0.000050,
   'Narration on the fal end-to-end lane, billed per character of script.'),
  ('fal', 'fal-ai/whisper', 'second', 0.000100,
   'Transcription, for burning captions on the end-to-end lane. Rounding error beside the video.'),
  -- HeyGen reports a wallet balance, so a presenter render's cost is measured
  -- from the delta and this rate is only the fallback for when the wallet
  -- cannot be read twice. 30 seconds at $0.05/s is $1.50, inside the preset's
  -- $1-2.
  ('heygen', '', 'second', 0.050000,
   'Fallback only: a presenter render is measured from the wallet delta when the balance can be read.'),
  -- The stock lane. Free footage and a free local TTS voice, so its measured
  -- cost is genuinely zero -- and recording a zero is what makes "a cost is
  -- recorded for every provider" true rather than "for the two that bill".
  ('mpt', 'pexels',   'render', 0.000000, 'Licensed stock, free at our volume.'),
  ('mpt', 'pixabay',  'render', 0.000000, 'Licensed stock, free at our volume.'),
  ('mpt', 'coverr',   'render', 0.000000, 'Licensed stock, free at our volume.'),
  ('mpt', 'local',    'render', 0.000000, 'Clips fal already generated and was already billed for.'),
  ('mpt', 'wavespeed', 'clip',  0.100000, 'Generative footage fetched by MoneyPrinterTurbo, billed per request.'),
  ('mpt', 'volcengine_seedance', 'clip', 0.200000,
   'Generative footage fetched by MoneyPrinterTurbo, billed per request.')
on conflict (provider, model) do update
  set unit     = excluded.unit,
      rate_usd = excluded.rate_usd,
      note     = excluded.note;


-- Caps. Seeded active, because "nothing caps spend" is the problem being
-- fixed and a feature that ships switched off fixes nothing. The monthly
-- figures are the top of the ranges the README already plans for -- $600 for
-- an all-presenter month, $120 for stock -- so the ceiling refuses the
-- runaway case rather than an ordinary week. The daily figures are roughly a
-- twentieth of each, which is more than ten reels a day can spend on one lane
-- and far less than a loop can.
--
-- Every one of these is editable in Settings, which is the point: these are a
-- starting position, not a judgement about the right budget.
insert into public.spend_caps (provider, model, daily_limit_usd, monthly_limit_usd, note)
values
  ('heygen', '', 30.00, 600.00,
   'The presenter lane. README plans $300-600/month for an all-presenter week.'),
  ('fal', '', 30.00, 600.00,
   'Every fal model together, narration and transcription included.'),
  ('mpt', '', 10.00, 120.00,
   'The stock lane. Free footage, so this only binds if a paid source is selected.'),
  -- The one model with a cap of its own. The end-to-end lane bills at twice
  -- the fast tier and ships inactive precisely because its output has never
  -- been compared with the others, so it gets a ceiling well below fal's own
  -- while it is being judged.
  ('fal', 'fal-ai/ltx-2.3/text-to-video', 10.00, 100.00,
   'The standard tier, twice the price of fast. Held low while the end-to-end lane is unproven.')
on conflict (provider, model) do nothing;
