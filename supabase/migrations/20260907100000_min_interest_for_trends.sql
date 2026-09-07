-- =============================================================================
-- A floor for search interest, separate from the one for view counts.
-- =============================================================================
--
-- `min_plays` was reused for Google Trends on the grounds that both it and a
-- view count answer "is anyone actually looking at this". The question is
-- shared; the scales are not, and that is the part that matters.
--
-- A view count is unbounded -- 198000 is a reasonable floor for a video, and
-- is what this install had set. Google Trends interest is 0-100, normalised
-- against the term's own three-month peak. Comparing the second against the
-- first rejects every term that will ever exist, which is exactly what
-- happened: the first live Trends run measured fifteen terms, Google answered
-- all fifteen without a single refusal, and twelve were dropped for being
-- under 198000 on a scale whose maximum is 100.
--
-- The run reported this correctly and immediately -- `too_few_plays: 12,
-- setting=min_plays, value=198000` -- which is the only reason it took one
-- query rather than an afternoon. It is also the second time in this feature
-- that a setting meant for one source has silently broken another, after
-- `idea_provider` defaulting to Claude over a deployment running Gemini. The
-- lesson is the same both times: a value whose meaning depends on context
-- needs its own column, not a shared one and a comment explaining the
-- difference.
-- =============================================================================

alter table public.trend_settings
  -- 0-100, because that is the only scale Google Trends reports on. Zero means
  -- no floor, which is the right default: a term's *rise* is what this source
  -- is for, and its absolute level is a weaker signal than the ratio already
  -- being checked.
  add column if not exists min_interest smallint not null default 0;

do $$
begin
  alter table public.trend_settings drop constraint if exists trend_settings_min_interest;
  alter table public.trend_settings add constraint trend_settings_min_interest
    check (min_interest between 0 and 100);
end;
$$;

comment on column public.trend_settings.min_interest is
  'Google Trends only. Minimum current interest as a percentage of the term''s own 3-month peak, 0-100. Not comparable to min_plays.';
comment on column public.trend_settings.min_plays is
  'Video sources only. Minimum absolute view count. Not comparable to min_interest.';
