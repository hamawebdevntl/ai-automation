import type { TrendRejections, TrendRunRow, TrendSettingsRow } from '@/lib/database.types';

/**
 * Trend rows for tests, built from the seeded defaults.
 *
 * Shared rather than repeated per test file. `trend_settings` alone carries
 * twenty-four columns now, and a literal in each test means every new setting
 * breaks every trend test for a reason that has nothing to do with what those
 * tests are about.
 *
 * The values are the migration's own defaults, so a test that overrides
 * nothing is a test of the behaviour a new install actually gets.
 */
export function trendSettings(over: Partial<TrendSettingsRow> = {}): TrendSettingsRow {
  return {
    id: true,
    niche_brief: 'We are a software and AI automation agency.',
    hashtags: ['aiautomation', 'webdesign', 'devops'],
    updated_at: '2026-09-06T00:00:00Z',
    updated_by: null,

    schedule_enabled: true,
    schedule_hour_utc: 6,
    schedule_minute_utc: 0,
    schedule_days: [0, 1, 2, 3, 4, 5, 6],

    max_video_age_days: 30,
    min_plays: 0,
    min_engagement_rate: 0,
    min_outlier_ratio: 1.5,
    caption_blocklist: [],
    videos_per_hashtag: 30,
    ideas_per_run: 10,

    hashtags_per_run: null,
    hashtag_cursor: 0,
    run_budget_minutes: null,
    baseline_sample_size: 12,
    pacing_min_seconds: 2,
    pacing_max_seconds: 5,
    dedup_window_days: 14,
    idea_expiry_days: 7,
    idea_provider: 'claude',
    trend_source: 'google_trends',
    trend_keywords: ['invoice software', 'crm software', 'bookkeeping software'],
    trend_geo: '',
    ...over,
  };
}

export function trendRun(over: Partial<TrendRunRow> = {}): TrendRunRow {
  return {
    id: 'run-1',
    status: 'succeeded',
    requested_by: null,
    requested_at: '2026-09-06T10:00:00Z',
    started_at: '2026-09-06T10:01:00Z',
    finished_at: '2026-09-06T11:00:00Z',
    task_arn: null,
    signals: 40,
    drafted: 8,
    inserted: 8,
    suppressed: 0,
    error: null,
    scouted: 480,
    hashtags_scouted: ['aiautomation', 'webdesign', 'devops'],
    rejections: null,
    trigger: 'manual',
    scheduled_for: null,
    cancelled_at: null,
    cancelled_by: null,
    task_stopped_at: null,
    override_run_budget_minutes: null,
    override_hashtags_per_run: null,
    ...over,
  };
}

/**
 * A breakdown in which every stage is present, most at zero.
 *
 * Mirrors what `pipeline.trends.report.payload` produces, including the zeroes:
 * a report that lists only what fired reads as an accusation, and the zeroes
 * are how the owner sees that a filter they were about to loosen was not the
 * one rejecting anything.
 */
export function trendRejections(over: Partial<TrendRejections> = {}): TrendRejections {
  const stage = (key: string, label: string, dropped: number, setting: string | null, value: number | null) => ({
    key,
    label,
    level: 'video' as const,
    dropped,
    setting,
    value,
  });
  return {
    seen: 480,
    stages: [
      stage('too_old', 'Older than your recency limit', 310, 'max_video_age_days', 30),
      stage('too_few_plays', 'Under your minimum view count', 140, 'min_plays', 50000),
      stage('blocked_caption', 'Caption contained a blocked word', 0, 'caption_blocklist', null),
      stage('no_baseline', "Author's own history could not be read", 0, null, null),
      stage('below_ratio', "Did not outperform its author's median enough", 30, 'min_outlier_ratio', 1.5),
      stage('below_engagement', 'Under your minimum engagement rate', 0, 'min_engagement_rate', 0),
      {
        ...stage('duplicate', 'Too close to an idea already in the queue', 0, 'dedup_window_days', 14),
        level: 'idea' as const,
      },
    ],
    surfaced: 0,
    drafted: 0,
    inserted: 0,
    failed_hashtags: [],
    budget_exhausted: false,
    cancelled: false,
    hashtags_skipped: 0,
    hashtags_scouted: ['aiautomation', 'webdesign', 'devops'],
    hashtags_configured: 3,
    ...over,
  };
}
