-- =============================================================================
-- Google Trends as a trend source, and the source as a setting.
-- =============================================================================
--
-- TikTok-Api stopped working. Not intermittently -- refused on every feed, from
-- a datacenter IP and from a residential one, headless and headful, with a
-- token and without. It is pinned at 7.3.3 because that is the latest release,
-- and that release is five months old against a platform that changes its
-- defences continuously. `docs/STACK.md` predicted exactly this: "1 author --
-- expect it to break."
--
-- So the scout needs a source that is not a scraping arms race. Two columns
-- here, and the first is the more important one long-term.
--
-- `trend_source` makes the source a setting rather than an import. The scout
-- was written as though TikTok were the only possible input -- `tiktok.scout`
-- called directly, its config built inline -- which is why replacing it is a
-- migration rather than a checkbox. Making it data now means the next source
-- (YouTube's official API is the obvious one, and unlike both of these it is
-- documented and supported) is a new module and a new enum value, not another
-- day like this one.
--
-- `trend_keywords` exists because a hashtag is not a search term. `#exceltips`
-- is how a video is filed; "bookkeeping software" is what someone types when
-- they have had enough of doing it by hand. The hashtag list stays for TikTok
-- and for whatever video source comes next; this is its counterpart for
-- anything measuring search demand.
--
-- Worth being clear about what changes in the ideas themselves. TikTok told us
-- which *format* was working, with engagement as proof. Google Trends tells us
-- what people are *searching for* and cannot say anything about a hook. For
-- reels whose job is to start conversations with buyers that is arguably the
-- better half to keep -- someone searching "invoice software" is closer to
-- in-market than someone who merely watched a video about invoicing -- but the
-- hook now rests entirely on the brief and the model. If the ideas get worse,
-- that is the reason, and the fix is a video source rather than more keywords.
-- =============================================================================

alter table public.trend_settings
  add column if not exists trend_source text not null default 'google_trends',
  -- Buyer language, not ours. See the seed below.
  add column if not exists trend_keywords text[] not null default '{}',
  -- Google Trends is regional and an empty string means worldwide, which is
  -- the honest default: nothing here knows where this business sells.
  add column if not exists trend_geo text not null default '';

do $$
begin
  alter table public.trend_settings drop constraint if exists trend_settings_source;
  alter table public.trend_settings add constraint trend_settings_source
    check (trend_source in ('google_trends', 'tiktok'));

  -- Two letters, or empty for worldwide. Google's own geo codes are ISO-3166
  -- alpha-2, optionally with a subdivision ("GB-ENG"), so this is deliberately
  -- loose about the tail and strict about the shape.
  alter table public.trend_settings drop constraint if exists trend_settings_geo;
  alter table public.trend_settings add constraint trend_settings_geo
    check (trend_geo = '' or trend_geo ~ '^[A-Z]{2}(-[A-Z0-9]{1,3})?$');

  -- Same shape of guard as the hashtags: enough to work with, few enough that
  -- a run stays inside a rate limit that is not ours to raise.
  alter table public.trend_settings drop constraint if exists trend_settings_keywords;
  alter table public.trend_settings add constraint trend_settings_keywords
    check (coalesce(array_length(trend_keywords, 1), 0) <= 50);
end;
$$;

comment on column public.trend_settings.trend_source is
  'Where signals come from: google_trends (search demand) or tiktok (video formats, currently broken).';
comment on column public.trend_settings.trend_keywords is
  'Search terms to measure, in the words a buyer would type. Not hashtags.';
comment on column public.trend_settings.trend_geo is
  'ISO-3166 region for Google Trends, or empty for worldwide.';

-- ---------------------------------------------------------------------------
-- Seed keywords.
-- ---------------------------------------------------------------------------
--
-- Chosen on the same principle as the hashtags in 20260906130000: grouped by
-- who is typing them, not by what we find interesting. Every one of these is
-- something an operator searches when a manual process has finally cost them
-- an afternoon -- which is the moment this business exists to be found in.
--
-- Deliberately NOT here: "workflow orchestration", "systems integration",
-- "RPA", "custom software development". Nobody outside the trade types those,
-- and a keyword nobody searches returns a flat line that scores as no signal.
--
-- Only if nobody has set any. A list edited in the app is a choice.
update public.trend_settings set
  trend_keywords = array[
    -- The tool they are looking to buy or replace.
    'invoice software', 'crm software', 'bookkeeping software',
    'quoting software', 'job management software', 'scheduling software',
    'inventory management software',
    -- The job, in their words.
    'automate invoicing', 'automate data entry', 'automate reports',
    -- The moment the spreadsheet stops being enough.
    'excel alternative', 'spreadsheet to database', 'zapier alternative',
    -- The category, for the build-vs-buy audience.
    'ai automation', 'business process automation'
  ]
where id and coalesce(array_length(trend_keywords, 1), 0) = 0;
