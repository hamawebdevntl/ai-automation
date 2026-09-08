import { describe, expect, it } from 'vitest';
import {
  blocklistEntryError,
  clampToBounds,
  DESCRIBED_SEARCH_PRESETS,
  describeSchedule,
  diagnoseRun,
  estimateDescribedSearch,
  estimateRunMinutes,
  formatStageValue,
  fromPercent,
  isWithinBounds,
  nextRunAt,
  runsToCoverEveryTag,
  TREND_BOUNDS,
  tagsPerRun,
  toPercent,
} from '@/features/trends/controls';
import { SEARCH_TERMS_DEFAULT } from '@/features/trends/search';
import { trendRejections, trendRun, trendSettings } from '@/features/trends/test-fixtures';

/**
 * The bounds here are a copy of the CHECK constraints, kept for the form's own
 * feedback. Postgres is what actually refuses a bad value, so these tests are
 * about the copy being useful, not about it being the guard.
 */
describe('bounds', () => {
  it('accepts a value inside the range and rejects one outside', () => {
    expect(isWithinBounds('videos_per_hashtag', 30)).toBe(true);
    expect(isWithinBounds('videos_per_hashtag', 4)).toBe(false);
    expect(isWithinBounds('videos_per_hashtag', 101)).toBe(false);
  });

  it('rejects a blank input rather than treating it as zero', () => {
    // An emptied number input reads as NaN, which must not silently become the
    // minimum in a field where the minimum is a meaningful setting.
    expect(isWithinBounds('min_plays', Number.NaN)).toBe(false);
  });

  it('clamps to the nearest allowed value', () => {
    expect(clampToBounds('ideas_per_run', 900)).toBe(25);
    expect(clampToBounds('ideas_per_run', 0)).toBe(1);
  });

  it('keeps the pacing floor above zero', () => {
    // The one bound that protects the account rather than the run. Faster than
    // this is not a quicker scrape, it is a blocked session.
    expect(TREND_BOUNDS.pacing_min_seconds.min).toBe(1);
    expect(isWithinBounds('pacing_min_seconds', 0)).toBe(false);
  });

  it('caps the outlier ratio where the pipeline clamps it', () => {
    // Above 50 nothing could ever match, because that is MAX_RATIO.
    expect(isWithinBounds('min_outlier_ratio', 51)).toBe(false);
  });
});

describe('the blocklist', () => {
  it('refuses a single character with the reason', () => {
    expect(blocklistEntryError('a')).toMatch(/at least two/i);
    expect(blocklistEntryError('ok')).toBeNull();
  });

  it('ignores surrounding whitespace when judging length', () => {
    expect(blocklistEntryError('  a  ')).toMatch(/at least two/i);
  });
});

describe('engagement rate', () => {
  it('round-trips through the percentage the form shows', () => {
    // Stored as numeric(5,4), so 4.25% has to survive a save unchanged.
    expect(toPercent(0.0425)).toBe(4.25);
    expect(fromPercent(4.25)).toBe(0.0425);
  });
});

describe('the schedule', () => {
  const at = (iso: string) => new Date(iso);

  it('finds tomorrow once today has passed', () => {
    const settings = trendSettings({ schedule_hour_utc: 6 });
    expect(nextRunAt(settings, at('2026-09-08T06:01:00Z'))?.toISOString()).toBe('2026-09-09T06:00:00.000Z');
  });

  it('finds today when it has not', () => {
    const settings = trendSettings({ schedule_hour_utc: 6 });
    expect(nextRunAt(settings, at('2026-09-08T05:59:00Z'))?.toISOString()).toBe('2026-09-08T06:00:00.000Z');
  });

  it('skips to the next enabled day', () => {
    // Weekdays only, asked on Friday evening. The answer is Monday.
    const settings = trendSettings({ schedule_hour_utc: 6, schedule_days: [1, 2, 3, 4, 5] });
    expect(nextRunAt(settings, at('2026-09-11T20:00:00Z'))?.toISOString()).toBe('2026-09-14T06:00:00.000Z');
  });

  it('is null while paused', () => {
    expect(nextRunAt(trendSettings({ schedule_enabled: false }), at('2026-09-08T05:00:00Z'))).toBeNull();
  });

  it('stays in UTC regardless of the browser', () => {
    // The setting is UTC and has to mean the same instant wherever it is read.
    const answer = nextRunAt(trendSettings({ schedule_hour_utc: 23 }), at('2026-09-08T22:00:00Z'));
    expect(answer?.getUTCHours()).toBe(23);
  });

  it('describes itself in words', () => {
    expect(describeSchedule(trendSettings())).toBe('Every day at 06:00 UTC.');
    expect(describeSchedule(trendSettings({ schedule_days: [1, 3, 5], schedule_minute_utc: 30 }))).toBe(
      'Mon, Wed and Fri at 06:30 UTC.',
    );
    expect(describeSchedule(trendSettings({ schedule_enabled: false }))).toMatch(/paused/i);
  });
});

describe('what a run will cost', () => {
  const sixteen = Array.from({ length: 16 }, (_, i) => `tag${i}`);

  it('scouts every tag when rotation is off', () => {
    const settings = trendSettings({ hashtags: sixteen, hashtags_per_run: null });
    expect(tagsPerRun(settings)).toBe(16);
    expect(runsToCoverEveryTag(settings)).toBe(1);
  });

  it('scouts a slice when rotation is on, and still covers the list', () => {
    const settings = trendSettings({ hashtags: sixteen, hashtags_per_run: 6 });
    expect(tagsPerRun(settings)).toBe(6);
    expect(runsToCoverEveryTag(settings)).toBe(3);
  });

  it('never claims to scout more tags than exist', () => {
    expect(tagsPerRun(trendSettings({ hashtags: ['a', 'b'], hashtags_per_run: 20 }))).toBe(2);
  });

  it('matches the four-minutes-per-hashtag the page used to quote', () => {
    // 30 videos, two paced waits each, averaging 3.5s: about 3.5 minutes.
    expect(estimateRunMinutes(trendSettings({ hashtags: ['one'] }))).toBe(4);
  });

  it('shortens when the list is rotated', () => {
    const all = trendSettings({ hashtags: sixteen, hashtags_per_run: null });
    const rotated = trendSettings({ hashtags: sixteen, hashtags_per_run: 6 });
    expect(estimateRunMinutes(rotated)).toBeLessThan(estimateRunMinutes(all));
  });

  it('never quotes longer than the budget, which is a ceiling on exactly this', () => {
    const settings = trendSettings({ hashtags: sixteen, run_budget_minutes: 30 });
    expect(estimateRunMinutes(settings)).toBe(30);
  });

  it('grows with the pacing, which is where the time in a run actually goes', () => {
    const slow = trendSettings({ hashtags: ['one'], pacing_min_seconds: 8, pacing_max_seconds: 12 });
    expect(estimateRunMinutes(slow)).toBeGreaterThan(estimateRunMinutes(trendSettings({ hashtags: ['one'] })));
  });
});

/**
 * The distinction the breakdown exists for. Three unrelated situations used to
 * arrive as `signals = 0`, and the reasonable conclusion from that -- that the
 * scraper is broken -- is wrong most of the time.
 */
describe('diagnosing a run', () => {
  it('blames the scrape when nothing was even seen', () => {
    const run = trendRun({
      inserted: 0,
      rejections: trendRejections({
        seen: 0,
        surfaced: 0,
        inserted: 0,
        failed_hashtags: [{ hashtag: 'crm', error: 'blocked' }],
      }),
    });
    const diagnosis = diagnoseRun(run);
    expect(diagnosis.kind).toBe('scraper');
    expect(diagnosis.detail).toMatch(/not your filters/i);
  });

  it('still blames the scrape when the feeds simply returned nothing', () => {
    // No errors, no videos. A filter cannot produce this either.
    const run = trendRun({ inserted: 0, rejections: trendRejections({ seen: 0, surfaced: 0, inserted: 0 }) });
    expect(diagnoseRun(run).kind).toBe('scraper');
  });

  it('names the filter responsible when videos were seen and all rejected', () => {
    const run = trendRun({ inserted: 0, rejections: trendRejections({ surfaced: 0, inserted: 0 }) });
    const diagnosis = diagnoseRun(run);
    expect(diagnosis.kind).toBe('filters');
    expect(diagnosis.culprit?.key).toBe('too_old');
  });

  it('separates repeats from weakness', () => {
    const stages = trendRejections().stages.map((s) => ({ ...s, dropped: s.key === 'duplicate' ? 3 : 0 }));
    const run = trendRun({
      inserted: 0,
      rejections: trendRejections({ seen: 200, surfaced: 9, drafted: 3, inserted: 0, stages }),
    });
    expect(diagnoseRun(run).kind).toBe('duplicates');
  });

  it('calls a run that added ideas a success', () => {
    const run = trendRun({ inserted: 5, rejections: trendRejections({ surfaced: 12, drafted: 5, inserted: 5 }) });
    expect(diagnoseRun(run).kind).toBe('ok');
  });

  it('admits when there is no breakdown rather than guessing', () => {
    expect(diagnoseRun(trendRun({ rejections: null, inserted: 0 })).kind).toBe('unknown');
  });
});

describe('showing a stage', () => {
  it('renders each setting in its own units', () => {
    const stage = (setting: string, value: number | string[] | null) => ({
      key: 'k',
      label: 'l',
      level: 'video' as const,
      dropped: 0,
      setting,
      value,
    });
    expect(formatStageValue(stage('min_engagement_rate', 0.04))).toBe('4%');
    expect(formatStageValue(stage('min_outlier_ratio', 2.5))).toBe('2.5×');
    expect(formatStageValue(stage('min_plays', 50000))).toBe('50,000 views');
    expect(formatStageValue(stage('max_video_age_days', 30))).toBe('30 days');
    expect(formatStageValue(stage('caption_blocklist', ['a', 'b']))).toBe('2 words');
    expect(formatStageValue(stage('caption_blocklist', []))).toBe('none set');
    expect(formatStageValue(stage(null as unknown as string, null))).toBeNull();
  });
});

/**
 * A described search's length counts terms the worker may read, not a slice of
 * the saved list, and its minutes follow the source: per term on Google Trends,
 * per video elsewhere. The numbers below are the ones the control shows.
 */
describe('the length of a described search', () => {
  const settings = trendSettings();

  it('defaults to the number of terms the worker uses, and counts terms rather than the list', () => {
    const estimate = estimateDescribedSearch(settings);
    expect(estimate.terms).toBe(SEARCH_TERMS_DEFAULT);
    expect(estimate.videos).toBeNull();
    expect(estimate.minutes).toBe(3); // six terms at thirty seconds each
    expect(estimate.ideaCap).toBe(10);
  });

  it('pays per video on a video source', () => {
    const estimate = estimateDescribedSearch({ ...settings, trend_source: 'apify' }, { terms: 4 });
    expect(estimate.videos).toBe(120); // 4 terms x 30 videos
    expect(estimate.minutes).toBe(14); // 120 videos x 2 delays x 3.5s
  });

  it('is held to the bound of the column it rides in', () => {
    expect(estimateDescribedSearch(settings, { terms: 500 }).terms).toBe(100);
    expect(estimateDescribedSearch(settings, { terms: 0 }).terms).toBe(1);
  });

  it('never quotes a run longer than its budget', () => {
    const estimate = estimateDescribedSearch({ ...settings, trend_source: 'apify' }, { terms: 12, budgetMinutes: 10 });
    expect(estimate.minutes).toBe(10);
  });

  it('names the worker default as Standard', () => {
    expect(DESCRIBED_SEARCH_PRESETS.find((p) => p.key === 'standard')?.terms).toBe(SEARCH_TERMS_DEFAULT);
  });
});
