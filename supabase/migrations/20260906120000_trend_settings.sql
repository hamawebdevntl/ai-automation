-- =============================================================================
-- Trend settings — the niche brief and the hashtags to scout, as data.
-- =============================================================================
--
-- These two were environment variables reaching the trend task through the
-- Secrets Manager bundle. That made them deploy-time constants: changing which
-- hashtags get scouted meant editing a secret and waiting for a cold start,
-- which is the wrong shape for the two values most likely to be tuned weekly.
--
-- They are also the two inputs that decide whether a trend run is worth
-- anything. The brief is what makes an idea relevant rather than generically
-- topical, and the hashtags decide what is even looked at. Both belong to
-- whoever reviews the queue, not to whoever last ran terraform.
--
-- Singleton by construction: `id` is a boolean primary key constrained to
-- true, so a second row cannot be inserted. That is deliberate rather than a
-- convention -- two rows of settings would give the trend task a choice it has
-- no basis to make.
--
-- The environment variables still work as a fallback when this row is empty,
-- which is what keeps a headless run (and the test suite) working without a
-- database round-trip. The row wins whenever it has a value.
-- =============================================================================

create table if not exists public.trend_settings (
  id          boolean primary key default true,
  niche_brief text        not null default '',
  hashtags    text[]      not null default '{}',
  updated_at  timestamptz not null default now(),
  updated_by  uuid references auth.users (id) on delete set null,

  constraint trend_settings_singleton check (id)
);

comment on table public.trend_settings is
  'Single row. The niche brief and hashtag list the trend task reads before scouting.';
comment on column public.trend_settings.niche_brief is
  'What we do, who we speak to, and what we must not claim. Trend research refuses to run without it.';
comment on column public.trend_settings.hashtags is
  'Hashtags to scout, without the leading #. Each one costs roughly a minute of scouting.';

-- ---------------------------------------------------------------------------
-- Defaults. Seeded, not hardcoded: every one of these can be edited or removed
-- in the app, which is the entire point of the table.
-- ---------------------------------------------------------------------------
insert into public.trend_settings (id, niche_brief, hashtags)
values (
  true,
  'We are a software and AI automation agency. We build custom software and '
  'automate business workflows -- internal tools, data pipelines, AI agents, and '
  'integrations between the systems a company already runs. '
  'We speak to operators at small and mid-sized businesses who are doing by hand '
  'what software could do, and to technical leads weighing build against buy. '
  'We must NOT claim: specific ROI, revenue or time-saved figures without a case '
  'study behind them; that any automation is hands-off, error-free or needs no '
  'maintenance; that we are partnered with, certified by or endorsed by any '
  'vendor whose tools we use; anything identifying a client, their data or their '
  'results without written permission. Medical, financial, legal and regulatory '
  'advice is never ours to give.',
  array[
    -- AI and automation
    'aiautomation', 'aiagents', 'aitools', 'automation', 'workflowautomation', 'nocode',
    -- Websites
    'webdevelopment', 'webdesign',
    -- Applications
    'appdevelopment', 'saas', 'softwaredevelopment',
    -- Systems and the trade generally
    'systemdesign', 'devops', 'internaltools', 'coding', 'techtok'
  ]
)
on conflict (id) do nothing;

drop trigger if exists trend_settings_touch_updated_at on public.trend_settings;
create trigger trend_settings_touch_updated_at
  before update on public.trend_settings
  for each row execute function public.touch_updated_at();

-- ---------------------------------------------------------------------------
-- Access. Same split as style_presets: everyone signed in can read, only an
-- owner can change. There is deliberately no INSERT or DELETE policy -- the
-- row is created here and is meant to be edited, never replaced.
-- ---------------------------------------------------------------------------
alter table public.trend_settings enable row level security;

drop policy if exists trend_settings_read on public.trend_settings;
create policy trend_settings_read on public.trend_settings
  for select to authenticated using (true);

drop policy if exists trend_settings_update on public.trend_settings;
create policy trend_settings_update on public.trend_settings
  for update to authenticated using (public.is_owner()) with check (public.is_owner());
