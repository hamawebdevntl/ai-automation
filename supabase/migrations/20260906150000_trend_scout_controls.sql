-- =============================================================================
-- Trend scout controls -- timing, filters and pacing, as data.
-- =============================================================================
--
-- `trend_settings` already held the two inputs most worth tuning weekly. Every
-- other decision the scout makes was a constant in Python or a cron expression
-- in Terraform:
--
--   * when a run happens        cron(0 6 * * ? *) in infra/schedules.tf
--   * how many videos per tag   ScoutConfig(videos_per_hashtag=30)
--   * what counts as a signal   Signal.is_worth_surfacing -> ratio >= 1.5
--   * how many ideas            IDEAS_PER_RUN, a task environment variable
--   * how new a video must be   nothing at all -- see below
--   * pacing between requests   MIN_DELAY_S / MAX_DELAY_S, 2-5 seconds
--   * author baseline sample    BASELINE_SAMPLE = 12
--   * duplicate suppression     DEDUP_WINDOW_DAYS = 14
--   * idea expiry               expire_ideas(older_than_days=7)
--   * which model drafts        IDEA_LLM_PROVIDER, a Terraform variable
--
-- Changing any of them meant a deploy, which is the wrong shape for values
-- whose right setting is only discoverable by watching what reaches the queue.
-- They move here, with the pipeline reading the row and falling back to the
-- same constants when it cannot.
--
-- Every default below is today's behaviour, so applying this migration changes
-- nothing until something is edited -- with one deliberate exception. There
-- was no recency filter of any kind: `age_days` only fed the young-video ratio
-- extrapolation, so a two-year-old video with a good ratio was treated as a
-- trend. `max_video_age_days` therefore ships at 30 rather than unlimited.
-- That is a real behaviour change and the only one here. Thirty days sits well
-- past the seven-day extrapolation window, so it does not distort scoring.
--
-- Every column is bounded by a CHECK constraint rather than by the app. The
-- app is a static bundle with no server tier: validation shipped only in
-- JavaScript is validation that an old tab, a stale cache or a curl can skip,
-- and the two things these values can break are an hour-long run and the
-- standing of the account it runs against. Postgres is the only place a guard
-- actually holds. The pipeline clamps what it reads as well -- not because it
-- distrusts these constraints, but because it must also survive a row written
-- before they existed.
-- =============================================================================


-- ---------------------------------------------------------------------------
-- Timing.
-- ---------------------------------------------------------------------------
--
-- UTC throughout, deliberately. A stored IANA zone would keep 07:00 at 07:00
-- across a clock change, but it also puts a second interpretation between what
-- the page says and what the dispatcher does -- and the dispatcher's clock,
-- the run rows, the log timestamps and every other schedule in this system are
-- already UTC. One clock is worth an hour of drift twice a year.
alter table public.trend_settings
  add column if not exists schedule_enabled    boolean  not null default true,
  add column if not exists schedule_hour_utc   smallint not null default 6,
  add column if not exists schedule_minute_utc smallint not null default 0,
  -- 0 = Sunday .. 6 = Saturday, matching `extract(dow)`. Not Python's
  -- `weekday()`, which starts on Monday -- that conversion lives in exactly
  -- one place in the dispatcher and is named there.
  add column if not exists schedule_days       smallint[] not null default '{0,1,2,3,4,5,6}',
  -- The exception to defaults-unchanged. See the header.
  add column if not exists max_video_age_days  integer  not null default 30;

-- ---------------------------------------------------------------------------
-- Filters, in the order the scout now applies them.
-- ---------------------------------------------------------------------------
--
-- The order is the point. Age, plays and the blocklist are read straight off
-- the video the feed already handed us and cost nothing; the outlier ratio
-- needs the author's own median, which costs a paced request per new author.
-- Putting the free filters first is what makes tightening them speed a run up
-- rather than slow it down.
alter table public.trend_settings
  add column if not exists min_plays            integer      not null default 0,
  add column if not exists min_engagement_rate  numeric(5,4) not null default 0,
  add column if not exists min_outlier_ratio    numeric(5,2) not null default 1.5,
  add column if not exists caption_blocklist    text[]       not null default '{}',
  add column if not exists videos_per_hashtag   integer      not null default 30,
  add column if not exists ideas_per_run        integer      not null default 10;

-- ---------------------------------------------------------------------------
-- Run cost. The levers that decide how long a run takes and how hard it leans
-- on the platform.
-- ---------------------------------------------------------------------------
--
-- `hashtags_per_run` and `run_budget_minutes` are nullable, and null means off
-- -- which is today's behaviour, so both arrive inert. They are the two that
-- can shorten a run without changing what qualifies as a signal.
alter table public.trend_settings
  -- Scout this many tags per run, round-robin through the list. Null = all of
  -- them, every run.
  add column if not exists hashtags_per_run     integer,
  -- Where the next rotation starts. Written by the pipeline, not by the app;
  -- see the trigger below for why that does not count as an edit.
  add column if not exists hashtag_cursor       integer      not null default 0,
  -- Stop scouting after this long and draft from what we have. Null = no
  -- ceiling. A run cut short still produces ideas; a run killed by the
  -- three-hour write-off produces nothing at all.
  add column if not exists run_budget_minutes   integer,
  add column if not exists baseline_sample_size integer      not null default 12,
  -- What actually keeps the account in good standing. TikTok-Api does no
  -- pacing of its own and its iterators loop tightly, so this is the only
  -- thing between us and a request pattern no human produces. The floor below
  -- is the one guard here that exists to protect the account rather than the
  -- run.
  add column if not exists pacing_min_seconds   numeric(4,1) not null default 2.0,
  add column if not exists pacing_max_seconds   numeric(4,1) not null default 5.0,
  add column if not exists dedup_window_days    integer      not null default 14,
  add column if not exists idea_expiry_days     integer      not null default 7,
  add column if not exists idea_provider        text         not null default 'claude';

-- ---------------------------------------------------------------------------
-- The guards.
-- ---------------------------------------------------------------------------
--
-- Added separately from the columns so each one can be dropped and re-created
-- idempotently, and so the reasoning sits next to the bound rather than three
-- screens above it.

-- A CHECK constraint cannot contain a subquery, so a per-element rule on an
-- array needs a function. Immutable because the constraint has to be able to
-- re-verify a row at any time without the answer changing.
create or replace function public.trend_blocklist_is_sane(words text[])
returns boolean
language sql
immutable
parallel safe
as $$
  -- At most 200 entries, each 2-60 characters. The lower bound is the one that
  -- matters: matching is whole-word, but a single-character entry is still a
  -- word that appears in a great many captions, and the failure mode is a run
  -- that quietly rejects everything.
  select coalesce(array_length(words, 1), 0) <= 200
     and coalesce(bool_and(length(w) between 2 and 60), true)
  from unnest(coalesce(words, '{}'::text[])) as w;
$$;

comment on function public.trend_blocklist_is_sane(text[]) is
  'Bounds for trend_settings.caption_blocklist: <= 200 entries, each 2-60 chars.';

do $$
declare
  guard record;
begin
  for guard in
    select * from (values
      -- Timing.
      ('schedule_hour', 'schedule_hour_utc between 0 and 23'),
      ('schedule_minute', 'schedule_minute_utc between 0 and 59'),
      -- Containment covers the element range; the length bound stops an empty
      -- array, which would be a schedule that is enabled and never due --
      -- indistinguishable from a broken dispatcher, which is the exact
      -- confusion this whole change exists to remove. Pause is the way to mean
      -- "never".
      ('schedule_days', 'schedule_days <@ array[0,1,2,3,4,5,6]::smallint[]'
                     || ' and array_length(schedule_days, 1) between 1 and 7'),
      -- A year is the outer edge of a defensible claim that something is a
      -- trend. The floor is a day, not an hour: the feed's own freshness is
      -- coarser than that and a sub-day window would reject nearly everything.
      ('max_age', 'max_video_age_days between 1 and 365'),

      -- Filters.
      ('min_plays', 'min_plays between 0 and 100000000'),
      -- Half is already far past any real reel. A rate above that is a
      -- typo -- 40 meaning 40% rather than 0.40 -- and the cost of accepting
      -- it is a run that rejects every video for a reason nobody can see.
      ('min_engagement', 'min_engagement_rate between 0 and 0.5'),
      -- Below 1.0 the filter admits videos performing worse than their own
      -- author's median, which is not a signal in any sense. The ceiling is
      -- MAX_RATIO in velocity.py: ratios are clamped there, so a threshold
      -- above it can never be met by anything.
      ('min_ratio', 'min_outlier_ratio between 1.0 and 50.0'),
      ('blocklist', 'public.trend_blocklist_is_sane(caption_blocklist)'),
      -- Five is the fewest that can establish anything. A hundred is roughly
      -- thirteen minutes of paced requests on a single tag, which is as long
      -- as one room is worth.
      ('videos_per_hashtag', 'videos_per_hashtag between 5 and 100'),
      -- Twenty-five is already more than a day of review at ten reels a day.
      ('ideas_per_run', 'ideas_per_run between 1 and 25'),

      -- Run cost.
      ('hashtags_per_run', 'hashtags_per_run is null or hashtags_per_run between 1 and 100'),
      ('hashtag_cursor', 'hashtag_cursor >= 0'),
      -- Five minutes is enough for one tag to produce something. Past four
      -- hours the run outlives the three-hour write-off in reconcile.py and
      -- the budget stops meaning anything.
      ('run_budget', 'run_budget_minutes is null or run_budget_minutes between 5 and 240'),
      -- Three is the fewest samples `baseline()` will take a median of; below
      -- it the function falls back to the maximum and every ratio in the run
      -- is computed against a different kind of number.
      ('baseline_sample', 'baseline_sample_size between 3 and 30'),
      -- The floor exists to protect the account and nothing else. One second
      -- of jittered delay is already faster than a person; the reason it is
      -- not lower is that the next stop after "fast" is a captcha on every
      -- session, and that failure lands on the whole system rather than on
      -- one run.
      ('pacing_min', 'pacing_min_seconds between 1.0 and 30.0'),
      ('pacing_max', 'pacing_max_seconds between 1.0 and 60.0'),
      -- Jitter needs somewhere to sit. Equal is allowed and means a fixed
      -- delay, which is worse camouflage but not dangerous.
      ('pacing_order', 'pacing_max_seconds >= pacing_min_seconds'),
      ('dedup_window', 'dedup_window_days between 1 and 90'),
      ('idea_expiry', 'idea_expiry_days between 1 and 90'),
      ('idea_provider', 'idea_provider in (''claude'', ''gemini'')')
    ) as t(suffix, expr)
  loop
    execute format('alter table public.trend_settings drop constraint if exists %I',
                   'trend_settings_' || guard.suffix);
    execute format('alter table public.trend_settings add constraint %I check (%s)',
                   'trend_settings_' || guard.suffix, guard.expr);
  end loop;
end;
$$;

-- ---------------------------------------------------------------------------
-- Column comments. These are the tooltips of the database: the next person to
-- read this table in a console gets the reasoning, not just the type.
-- ---------------------------------------------------------------------------
comment on column public.trend_settings.schedule_enabled is
  'Whether the daily run happens on its own. False pauses the schedule only -- the button in the app still works.';
comment on column public.trend_settings.schedule_hour_utc is
  'Hour of the daily run, UTC. Was cron(0 6 * * ? *) in Terraform.';
comment on column public.trend_settings.schedule_days is
  'Days the schedule may fire, 0 = Sunday .. 6 = Saturday, matching extract(dow).';
comment on column public.trend_settings.max_video_age_days is
  'Reject a video older than this before it is scored. There was previously no age filter at all.';
comment on column public.trend_settings.min_outlier_ratio is
  'How far above its own author''s median a video must perform to count. The single strongest quality filter.';
comment on column public.trend_settings.caption_blocklist is
  'Reject a video whose caption contains one of these as a whole word, case-insensitively.';
comment on column public.trend_settings.hashtags_per_run is
  'Scout this many tags per run, rotating through the list. Null scouts all of them every run.';
comment on column public.trend_settings.hashtag_cursor is
  'Rotation position. Written by the pipeline; not an owner edit.';
comment on column public.trend_settings.run_budget_minutes is
  'Stop scouting after this long and draft from what was found. Null means no ceiling.';
comment on column public.trend_settings.pacing_min_seconds is
  'Jittered delay between requests. The floor is what keeps the account in good standing -- lower is not faster, it is flagged.';
comment on column public.trend_settings.idea_expiry_days is
  'When an unreviewed pending idea is marked expired.';

-- ---------------------------------------------------------------------------
-- The rotation cursor is not an edit.
-- ---------------------------------------------------------------------------
--
-- `updated_at` and `updated_by` answer "who last chose these settings, and
-- when" -- which the app shows, and which 20260906130000 relied on to decide
-- whether a default was still untouched. The pipeline advancing the rotation
-- cursor after a run would otherwise look exactly like the owner saving the
-- page, every single run.
--
-- The WHEN clause is precise rather than clever: the app never writes the
-- cursor and the pipeline writes nothing else, so "the cursor changed" and
-- "this was the pipeline" are the same statement.
drop trigger if exists trend_settings_touch_updated_at on public.trend_settings;
create trigger trend_settings_touch_updated_at
  before update on public.trend_settings
  for each row
  when (old.hashtag_cursor is not distinct from new.hashtag_cursor)
  execute function public.touch_updated_at();


-- =============================================================================
-- What a run rejected, and why.
-- =============================================================================
--
-- `trend_runs` recorded four counts: signals, drafted, inserted, suppressed.
-- All four are measured after scouting, so every way a run can come back empty
-- collapses into `signals = 0` -- a quiet week, filters set too tight, and a
-- scraper being served captchas are one number.
--
-- That was tolerable while the filters were constants nobody could change. It
-- is not tolerable now that they are a form: a filter the owner can tighten is
-- a filter the owner has to be able to see working, or the honest reading of
-- an empty queue becomes "the thing is broken".
--
-- So the counts are recorded per stage, in the order the scout applies them,
-- each naming the setting responsible. `scouted` is the one that separates the
-- two failures: filters can only ever reduce it, so `scouted = 0` is never a
-- filter and always the scraper.
alter table public.trend_runs
  -- Videos the feeds actually handed us, before any filter.
  add column if not exists scouted          integer,
  -- Which tags this run looked at. With rotation on, that is a subset of the
  -- list, and a report that did not say which subset would be unreadable.
  add column if not exists hashtags_scouted text[],
  -- Ordered stage counts. See `pipeline.trends.report` for the shape; JSONB
  -- rather than columns because the stages will change as the filters do, and
  -- a migration per filter is not a trade worth making for a display value.
  add column if not exists rejections       jsonb,
  -- How the run came to exist. The schedule is now a row like any other run,
  -- so without this a scheduled run is indistinguishable from someone having
  -- pressed the button at 06:00 every morning.
  -- Quoted throughout. TRIGGER is a non-reserved keyword, so bare `trigger`
  -- parses -- but "non-reserved" is a thing to look up rather than a thing to
  -- rely on in a migration that only gets one chance to run.
  add column if not exists "trigger"        text not null default 'manual',
  -- The schedule slot this run is for, and the idempotency key that stops the
  -- dispatcher starting it twice. Null on a manual run.
  add column if not exists scheduled_for    timestamptz;

do $$
begin
  alter table public.trend_runs drop constraint if exists trend_runs_trigger;
  alter table public.trend_runs add constraint trend_runs_trigger
    check ("trigger" in ('manual', 'schedule'));

  -- A scheduled run has a slot and a manual one does not. Keeps the two ideas
  -- from drifting apart -- a 'schedule' row with no slot would be invisible to
  -- the idempotency check below and would fire again the next minute.
  alter table public.trend_runs drop constraint if exists trend_runs_slot_matches_trigger;
  alter table public.trend_runs add constraint trend_runs_slot_matches_trigger
    check (("trigger" = 'schedule') = (scheduled_for is not null));
end;
$$;

-- One run per slot, forever, enforced by Postgres.
--
-- The dispatcher runs every minute and a slot stays due for a catch-up window
-- measured in tens of minutes, so "have we already done this one?" is asked
-- repeatedly about the same instant. Answering it with a read-then-insert
-- would start a second hour-long browser session the first time two ticks
-- overlap. This makes the second insert fail instead.
create unique index if not exists trend_runs_one_per_slot
  on public.trend_runs (scheduled_for)
  where scheduled_for is not null;

comment on column public.trend_runs.scouted is
  'Videos the feeds returned before any filter. Zero means the scraper found nothing -- no filter can produce it.';
comment on column public.trend_runs.rejections is
  'Ordered per-stage rejection counts, each naming the setting responsible.';
comment on column public.trend_runs."trigger" is
  'manual (the button) or schedule (the dispatcher, at scheduled_for).';
