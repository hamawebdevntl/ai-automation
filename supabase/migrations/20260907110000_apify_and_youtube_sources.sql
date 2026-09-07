-- =============================================================================
-- Two sources that are somebody else's problem to keep working.
-- =============================================================================
--
-- 20260907090000 made the trend source a setting and said what the next one
-- would cost: "a new module and a new enum value, not another day like this
-- one." This is that, twice over, and it also retires the source that prompted
-- the whole exercise.
--
-- `apify` scrapes TikTok and Instagram through hosted actors. It answers the
-- question TikTok-Api used to answer -- which *format* is working, with
-- engagement as proof -- which is the half Google Trends structurally cannot
-- give: someone searching "invoice software" tells you the subject is live and
-- nothing at all about how to open a video about it. The difference from last
-- time is who maintains the scraper. We rent it, we pay per result, and when
-- TikTok changes its defences it is Apify's weekend rather than ours.
--
-- `youtube` is the official Data API: documented, versioned, supported. The
-- price of that is a quota instead of an uncertainty -- roughly a hundred
-- searches a day, which is why the scout rotates its keyword list and stops
-- well short of the allowance. `docs/GAPS.md` has been saying for a while that
-- stage 1 is "really YouTube + Google Trends"; this makes that true rather
-- than aspirational.
--
-- Instagram is the quiet win here. `README.md` said "Instagram has no trend
-- source at all and should not be assumed", and now it has one.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- Retiring `tiktok`, carefully.
-- ---------------------------------------------------------------------------
--
-- Order matters: tightening the constraint while a row still says 'tiktok'
-- fails the migration. So the data moves first.
--
-- It moves to 'google_trends', NOT to 'apify', and that is deliberate despite
-- Apify being the direct replacement for what TikTok-Api did. An Apify run
-- bills per result. Migrating an install onto a metered API because a column
-- happened to hold a now-invalid value would be spending someone's money on
-- their behalf, on a schedule, without anyone choosing it -- which is a larger
-- version of exactly the mistake 20260906180000 was written about.
--
-- Nothing is lost by being cautious. The hashtag list is untouched and still
-- means what it meant, so choosing Apify afterwards is one dropdown away and
-- the scout picks up where TikTok left off. And an install that never migrates
-- lands in the same place by accident, in the good way:
-- `controls._source()` already degrades an unknown source to the default and
-- says so in the log.
--
-- ---------------------------------------------------------------------------
-- This install: Google Trends, explicitly.
-- ---------------------------------------------------------------------------
--
-- Unconditional rather than guarded on 'tiktok', and unlike 20260906180000
-- this is not a migration overruling an owner -- it is the owner's own
-- instruction, given while adding the two sources above.
--
-- The reason is the same one that makes it the column default: neither new
-- source has a credential yet. `APIFY_TOKEN` and `YOUTUBE_API_KEY` are unset,
-- and `sources._apify_ready` / `_youtube_ready` refuse a run at its first
-- second when the selected source has no key. So a row naming either of them
-- today is not an ambitious setting, it is a scheduled failure every hour
-- until somebody notices.
--
-- Google Trends is the one source that needs no key and costs nothing per run,
-- which is why it is where an install sits until an owner has deliberately
-- provisioned something else. Switching is one dropdown once the key exists.
update public.trend_settings
   set trend_source = 'google_trends'
 where id
   and trend_source <> 'google_trends';

-- ---------------------------------------------------------------------------
-- Which platforms Apify scrapes.
-- ---------------------------------------------------------------------------
--
-- Both by default: they are the same question asked of different audiences,
-- and an owner who wants only one can say so in a click. The cost of both is
-- two actor runs per hashtag instead of one, which is visible in the Apify
-- bill rather than hidden.
--
-- This is its own column rather than a flag on `trend_source` because it is a
-- property of one source. 20260907100000 recorded the lesson the hard way: a
-- value whose meaning depends on context needs its own column, not a shared
-- one and a comment explaining the difference.
alter table public.trend_settings
  add column if not exists apify_platforms text[] not null default '{tiktok,instagram}';

do $$
begin
  alter table public.trend_settings drop constraint if exists trend_settings_source;
  alter table public.trend_settings add constraint trend_settings_source
    check (trend_source in ('apify', 'google_trends', 'youtube'));

  -- Both bounds matter, and the lower one is the interesting half. An empty
  -- list would be a source that is selected and can never scout, which is
  -- indistinguishable from a runner that has stopped working -- the same
  -- confusion `schedule_days` refuses for the same reason. "Neither" is said
  -- by choosing a different source.
  alter table public.trend_settings drop constraint if exists trend_settings_apify_platforms;
  alter table public.trend_settings add constraint trend_settings_apify_platforms
    check (
      apify_platforms <@ array['tiktok', 'instagram']::text[]
      and coalesce(array_length(apify_platforms, 1), 0) between 1 and 2
    );
end;
$$;

comment on column public.trend_settings.trend_source is
  'Where signals come from: apify (TikTok and Instagram formats, metered), google_trends (search demand, free) or youtube (formats via the official API, quota''d).';
comment on column public.trend_settings.apify_platforms is
  'Which platforms the Apify source scrapes. At least one; both by default. Ignored by the other sources.';
