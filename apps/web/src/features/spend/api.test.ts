import { describe, expect, it } from 'vitest';
import { describeCapViolation, isSpendCapRefusal, spendForPreset } from '@/features/spend/api';
import { formatResetsAt } from '@/features/spend/components/spend-caps-card';
import type { StylePresetSpendRow } from '@/lib/database.types';

function row(overrides: Partial<StylePresetSpendRow> = {}): StylePresetSpendRow {
  return {
    style_preset_id: 'preset-a',
    slug: 'stock-broll',
    provider: 'mpt',
    model: 'pexels',
    block_reason: null,
    render_count: 0,
    measured_avg_usd: null,
    measured_min_usd: null,
    measured_max_usd: null,
    ...overrides,
  };
}

describe('spendForPreset', () => {
  it('finds the verdict for one style out of the list Gate 1 loads', () => {
    const rows = [row(), row({ style_preset_id: 'preset-b', provider: 'heygen' })];
    expect(spendForPreset(rows, 'preset-b')?.provider).toBe('heygen');
  });

  it('is undefined while the query is in flight, rather than guessing', () => {
    // The distinction matters: an unknown verdict must leave a style pickable,
    // because Postgres is what actually refuses. Reading "no row" as "capped"
    // would disable every style for as long as the query took.
    expect(spendForPreset(undefined, 'preset-a')).toBeUndefined();
  });

  it('is undefined for a preset with no row, not the first row it has', () => {
    expect(spendForPreset([row()], 'preset-missing')).toBeUndefined();
  });
});

describe('describeCapViolation', () => {
  /**
   * The bounds live in Postgres because this app ships as a static bundle and
   * a guard written in TypeScript is a guard an old tab can skip. The cost of
   * that is a 23514 whose message names a constraint rather than a field, so
   * these translations are what stand between an owner and
   * "new row violates check constraint spend_caps_needs_a_limit".
   */
  it('says what a cap with no figures in it is missing', () => {
    const said = describeCapViolation({
      code: '23514',
      message: 'new row for relation "spend_caps" violates check constraint "spend_caps_needs_a_limit"',
    });
    expect(said).toBe('A cap needs a daily figure, a monthly one, or both — otherwise it caps nothing.');
  });

  it('says why a daily figure above the monthly one is a typo', () => {
    const said = describeCapViolation({
      code: '23514',
      message: 'violates check constraint "spend_caps_daily_within_monthly"',
    });
    expect(said).toMatch(/at most the monthly one/);
  });

  it('still says something for a constraint it has not been taught', () => {
    const said = describeCapViolation({ code: '23514', message: 'violates check constraint "something_new"' });
    expect(said).toBe('One of these figures is outside the range the database will accept.');
  });

  it('declines anything that is not a check violation, so the real error survives', () => {
    expect(describeCapViolation({ code: '42501', message: 'Only an owner may do that' })).toBeNull();
    expect(describeCapViolation(new Error('offline'))).toBeNull();
  });
});

describe('isSpendCapRefusal', () => {
  /**
   * `approve_idea` raises `program_limit_exceeded` when the chosen style is at
   * its ceiling. It has to be distinguishable from the function's other
   * refusals, because the thing to do about it is different: top up, raise the
   * ceiling, or pick another style — not "ask an owner to promote you".
   */
  it('recognises the code the Gate 1 function raises on a spent cap', () => {
    expect(
      isSpendCapRefusal({ code: '54000', message: 'This style cannot be approved. Daily spend cap reached' }),
    ).toBe(true);
  });

  it('does not mistake the function’s other refusals for a cap', () => {
    // 42501 a viewer, 22023 an unknown preset, P0002 an idea no longer pending.
    for (const code of ['42501', '22023', 'P0002']) {
      expect(isSpendCapRefusal({ code, message: 'no' })).toBe(false);
    }
    expect(isSpendCapRefusal(new Error('offline'))).toBe(false);
  });
});

describe('formatResetsAt', () => {
  /**
   * Both windows are measured from a UTC boundary, deliberately: two people
   * looking at this page should see the same figures, and the pipeline's idea
   * of "today" should not move when a worker is deployed somewhere else. A
   * reset rendered in the viewer's own zone would undo that quietly, and would
   * disagree with the sentence `spend_block_reason()` shows at Gate 1, which
   * names UTC outright.
   */
  it('renders the boundary in UTC whatever zone the viewer is in', () => {
    // Deliberately not asserting on the whole string: the am/pm marker and the
    // separators are the runner's locale's business. The date and the hour are
    // the invariant -- in a zone behind UTC this instant is the 8th, and in one
    // ahead of it the hour is not midnight.
    const said = formatResetsAt('2026-09-09T00:00:00Z');
    expect(said).toMatch(/Sep 9, 2026/);
    expect(said).toMatch(/12:00/);
  });

  it('says which zone it means, so it cannot be read as local time', () => {
    expect(formatResetsAt('2026-10-01T00:00:00Z')).toMatch(/UTC/);
  });

  it('does not throw on the options it asks Intl for', () => {
    // `dateStyle`/`timeStyle` cannot be combined with `timeZoneName`: Intl
    // raises `TypeError: Invalid option`, which would take the whole card down
    // rather than merely dropping the label.
    expect(() => formatResetsAt('2026-10-01T00:00:00Z')).not.toThrow();
  });
});
