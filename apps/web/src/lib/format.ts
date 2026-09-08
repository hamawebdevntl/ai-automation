import type { QcReport, StylePresetRow } from '@/lib/database.types';

const usd = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

export function formatUsd(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—';
  return usd.format(value);
}

/** "$0.10 – $0.40", or a single figure when the range has no width. */
export function formatCostRange(preset: Pick<StylePresetRow, 'est_cost_min_usd' | 'est_cost_max_usd'>): string {
  const { est_cost_min_usd: min, est_cost_max_usd: max } = preset;
  return min === max ? formatUsd(min) : `${formatUsd(min)} – ${formatUsd(max)}`;
}

/**
 * "$1.42 over 7 renders", or null when nothing has been billed on this style.
 *
 * The figure that replaces the estimate range once real renders exist. Null
 * rather than a zero-render average, because "no renders yet" and "renders
 * that cost nothing" are different facts and only the second is worth
 * showing as a measurement.
 */
export function formatMeasuredCost(
  spend: { render_count: number; measured_avg_usd: number | null } | null | undefined,
): string | null {
  if (!spend || spend.render_count <= 0 || spend.measured_avg_usd === null) return null;
  const renders = spend.render_count === 1 ? '1 render' : `${spend.render_count} renders`;
  return `${formatUsd(spend.measured_avg_usd)} over ${renders}`;
}

export function formatMinutes(minutes: number | null | undefined): string {
  if (minutes === null || minutes === undefined) return '—';
  if (minutes < 60) return `~${minutes} min`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest === 0 ? `~${hours} h` : `~${hours} h ${rest} min`;
}

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return '—';
  const whole = Math.round(seconds);
  const mins = Math.floor(whole / 60);
  const secs = whole % 60;
  return mins > 0 ? `${mins}:${String(secs).padStart(2, '0')}` : `0:${String(secs).padStart(2, '0')}`;
}

const relative = new Intl.RelativeTimeFormat('en', { numeric: 'auto' });

const DIVISIONS: Array<{ amount: number; unit: Intl.RelativeTimeFormatUnit }> = [
  { amount: 60, unit: 'second' },
  { amount: 60, unit: 'minute' },
  { amount: 24, unit: 'hour' },
  { amount: 7, unit: 'day' },
  { amount: 4.34524, unit: 'week' },
  { amount: 12, unit: 'month' },
  { amount: Number.POSITIVE_INFINITY, unit: 'year' },
];

/** "12 minutes ago" from a Postgres timestamptz. */
export function formatRelative(timestamp: string | null | undefined): string {
  if (!timestamp) return '—';
  let duration = (new Date(timestamp).getTime() - Date.now()) / 1000;
  for (const division of DIVISIONS) {
    if (Math.abs(duration) < division.amount) {
      return relative.format(Math.round(duration), division.unit);
    }
    duration /= division.amount;
  }
  return '—';
}

/**
 * Narrow the `qc` jsonb column into something renderable. A job that has not
 * reached the quality check yet has `{}`, which is not a failure.
 */
export function parseQcReport(value: unknown): QcReport | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const record = value as Record<string, unknown>;
  if (!Array.isArray(record.checks)) return null;
  return {
    passed: record.passed === true,
    slideshow_risk: typeof record.slideshow_risk === 'number' ? record.slideshow_risk : undefined,
    checks: record.checks.flatMap((check) => {
      if (!check || typeof check !== 'object') return [];
      const item = check as Record<string, unknown>;
      if (typeof item.key !== 'string' || typeof item.label !== 'string') return [];
      const status = item.status;
      if (status !== 'pass' && status !== 'warn' && status !== 'fail') return [];
      return [
        {
          key: item.key,
          label: item.label,
          status,
          detail: typeof item.detail === 'string' ? item.detail : undefined,
        },
      ];
    }),
  };
}

export function titleCase(value: string): string {
  return value
    .split(/[\s_-]+/)
    .filter(Boolean)
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(' ');
}
