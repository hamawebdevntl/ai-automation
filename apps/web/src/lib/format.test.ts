import { describe, expect, it } from 'vitest';
import {
  formatCostRange,
  formatDuration,
  formatMeasuredCost,
  formatMinutes,
  formatUsd,
  parseQcReport,
  titleCase,
} from '@/lib/format';

describe('formatUsd', () => {
  it('renders an em dash rather than $0.00 for missing costs', () => {
    expect(formatUsd(null)).toBe('—');
    expect(formatUsd(undefined)).toBe('—');
    expect(formatUsd(0)).toBe('$0.00');
  });
});

describe('formatCostRange', () => {
  it('shows a range when the estimate has width', () => {
    expect(formatCostRange({ est_cost_min_usd: 0.1, est_cost_max_usd: 0.4 })).toBe('$0.10 – $0.40');
  });

  it('collapses to a single figure when the bounds match', () => {
    expect(formatCostRange({ est_cost_min_usd: 1.5, est_cost_max_usd: 1.5 })).toBe('$1.50');
  });
});

describe('formatDuration', () => {
  it('pads the seconds so times do not jump around', () => {
    expect(formatDuration(38.4)).toBe('0:38');
    expect(formatDuration(65)).toBe('1:05');
    expect(formatDuration(null)).toBe('—');
  });
});

describe('formatMinutes', () => {
  it('switches to hours past sixty minutes', () => {
    expect(formatMinutes(6)).toBe('~6 min');
    expect(formatMinutes(60)).toBe('~1 h');
    expect(formatMinutes(95)).toBe('~1 h 35 min');
  });
});

describe('parseQcReport', () => {
  it('returns null for a job that has not reached the quality check', () => {
    expect(parseQcReport({})).toBeNull();
    expect(parseQcReport(null)).toBeNull();
    expect(parseQcReport('not an object')).toBeNull();
  });

  it('keeps only well-formed checks', () => {
    const report = parseQcReport({
      passed: false,
      slideshow_risk: 0.81,
      checks: [
        { key: 'audio_levels', label: 'Audio levels', status: 'warn', detail: '-21.8 LUFS' },
        { key: 'broken', label: 'Missing status' },
        { key: 'bad_status', label: 'Unknown status', status: 'exploded' },
        'not an object',
      ],
    });

    expect(report).not.toBeNull();
    expect(report?.passed).toBe(false);
    expect(report?.slideshow_risk).toBe(0.81);
    expect(report?.checks).toHaveLength(1);
    expect(report?.checks[0]?.key).toBe('audio_levels');
  });

  it('treats a missing `passed` flag as not passed', () => {
    expect(parseQcReport({ checks: [] })?.passed).toBe(false);
  });
});

describe('titleCase', () => {
  it('handles the separators that appear in platform and status values', () => {
    expect(titleCase('instagram')).toBe('Instagram');
    expect(titleCase('awaiting_review')).toBe('Awaiting Review');
    expect(titleCase('youtube-shorts')).toBe('Youtube Shorts');
  });
});

describe('formatMeasuredCost', () => {
  it('reads as a measurement rather than a figure, because that is the point of it', () => {
    expect(formatMeasuredCost({ render_count: 4, measured_avg_usd: 1.42 })).toBe('$1.42 over 4 renders');
  });

  it('says "1 render" rather than "1 renders"', () => {
    expect(formatMeasuredCost({ render_count: 1, measured_avg_usd: 0.4 })).toBe('$0.40 over 1 render');
  });

  it('is null before anything has been billed, so the estimate is what shows', () => {
    // "No renders yet" and "renders that cost nothing" are different facts and
    // only the second is worth showing as a measurement.
    expect(formatMeasuredCost({ render_count: 0, measured_avg_usd: null })).toBeNull();
    expect(formatMeasuredCost(undefined)).toBeNull();
  });

  it('shows a genuine zero, which is what the stock lane measures at', () => {
    expect(formatMeasuredCost({ render_count: 12, measured_avg_usd: 0 })).toBe('$0.00 over 12 renders');
  });
});
