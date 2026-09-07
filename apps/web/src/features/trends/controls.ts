import type {
  ApifyPlatform,
  TrendRejectionStage,
  TrendRunRow,
  TrendSettingsRow,
  TrendSource,
} from '@/lib/database.types';

/**
 * The bounds, the estimates and the wording for the scout settings.
 *
 * Pure functions on purpose. Three of the things this file works out -- when
 * the next run is, how long a run will take, and whether a value is allowed --
 * are each easy to get subtly wrong and impossible to notice from the page, so
 * they are separated from the components that render them.
 *
 * On the bounds in particular: these are a copy of the CHECK constraints in
 * `20260906150000_trend_scout_controls.sql`, and the copy is for feedback
 * only. This app is a static bundle with no server tier, so validation that
 * lives only here is validation an old tab or a stale cache can skip. Postgres
 * is what actually refuses a bad value; this is what stops the owner having to
 * discover that by being refused.
 */

export interface Bound {
  min: number;
  max: number;
  step: number;
  /** Shown after the input. */
  unit?: string;
}

export const TREND_BOUNDS = {
  schedule_hour_utc: { min: 0, max: 23, step: 1 },
  schedule_minute_utc: { min: 0, max: 59, step: 5 },
  max_video_age_days: { min: 1, max: 365, step: 1, unit: 'days' },
  min_plays: { min: 0, max: 100_000_000, step: 1000, unit: 'views' },
  // A different scale entirely, which is the whole reason it is a different
  // column. Google Trends reports 0-100 against a term's own peak, so a
  // sensible view-count floor here rejects every term that can exist.
  min_interest: { min: 0, max: 100, step: 1, unit: '/ 100' },
  // Stored as a fraction; the form shows and takes a percentage. Half is
  // already far past any real reel, so a value above it is a typo for a
  // fraction -- and the cost of accepting one is a run that rejects
  // everything for a reason nobody can see.
  min_engagement_rate: { min: 0, max: 0.5, step: 0.0001 },
  // Below 1.0 the filter admits videos doing worse than their own author's
  // median. Above 50 nothing can ever match, because that is where the
  // pipeline clamps the ratio.
  min_outlier_ratio: { min: 1, max: 50, step: 0.1, unit: '×' },
  videos_per_hashtag: { min: 5, max: 100, step: 1, unit: 'videos' },
  ideas_per_run: { min: 1, max: 25, step: 1, unit: 'ideas' },
  hashtags_per_run: { min: 1, max: 100, step: 1, unit: 'tags' },
  run_budget_minutes: { min: 5, max: 240, step: 5, unit: 'min' },
  baseline_sample_size: { min: 3, max: 30, step: 1, unit: 'videos' },
  // The floor is the one bound here that protects the account rather than the
  // run. Faster than this is not a quicker scrape; it is a flagged session,
  // and that lands on the whole system rather than on one run.
  pacing_min_seconds: { min: 1, max: 30, step: 0.5, unit: 's' },
  pacing_max_seconds: { min: 1, max: 60, step: 0.5, unit: 's' },
  dedup_window_days: { min: 1, max: 90, step: 1, unit: 'days' },
  idea_expiry_days: { min: 1, max: 90, step: 1, unit: 'days' },
} as const satisfies Record<string, Bound>;

export type BoundedField = keyof typeof TREND_BOUNDS;

export const BLOCKLIST_MAX_ENTRIES = 200;
export const BLOCKLIST_MIN_WORD_LENGTH = 2;
export const BLOCKLIST_MAX_WORD_LENGTH = 60;

/** 0 = Sunday, matching Postgres `extract(dow)` and `schedule_days`. */
export const DAY_LABELS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'] as const;

/**
 * How late a missed slot may still start.
 *
 * Mirrors `CATCHUP_MINUTES` in `pipeline/trends/schedule.py`, which is the
 * side that acts on it. Duplicated rather than derived because there is no
 * server tier to ask — the same arrangement, and the same hazard, as
 * `SCOUT_CADENCE_MINUTES` in this feature's api module.
 */
export const SCHEDULE_CATCHUP_MINUTES = 60;

export function boundsOf(field: BoundedField): Bound {
  return TREND_BOUNDS[field];
}

/** Whether a value would be accepted by the column's constraint. */
export function isWithinBounds(field: BoundedField, value: number): boolean {
  const { min, max } = TREND_BOUNDS[field];
  return Number.isFinite(value) && value >= min && value <= max;
}

/**
 * Which of these values the database would refuse.
 *
 * Scans whatever subset of the settings it is given, so one implementation
 * serves every card. Fields outside the bounds table -- the brief, the
 * hashtags, the blocklist, the provider -- are skipped: they have their own
 * rules and their own messages.
 *
 * Null is always allowed. For the two nullable settings it means the feature
 * is off, which is a legitimate value rather than an out-of-range one.
 */
export function outOfBounds(draft: Partial<Record<string, unknown>>): BoundedField[] {
  return (Object.keys(TREND_BOUNDS) as BoundedField[]).filter((field) => {
    if (!(field in draft)) return false;
    const value = draft[field];
    if (value === null || value === undefined) return false;
    return typeof value !== 'number' || !isWithinBounds(field, value);
  });
}

/** Those fields, said as one sentence for the Save button. */
export function describeOutOfBounds(fields: BoundedField[]): string | null {
  if (fields.length === 0) return null;
  const names = fields.map((f) => FIELD_LABELS[f] ?? f);
  const list = names.length === 1 ? names[0] : `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`;
  return `${list} ${names.length === 1 ? 'is' : 'are'} outside the allowed range.`;
}

const FIELD_LABELS: Record<BoundedField, string> = {
  schedule_hour_utc: 'The hour',
  schedule_minute_utc: 'The minute',
  max_video_age_days: 'The recency limit',
  min_plays: 'The minimum view count',
  min_interest: 'The minimum search interest',
  min_engagement_rate: 'The engagement rate',
  min_outlier_ratio: 'The outlier ratio',
  videos_per_hashtag: 'Videos per hashtag',
  ideas_per_run: 'Ideas per run',
  hashtags_per_run: 'Hashtags per run',
  run_budget_minutes: 'The time budget',
  baseline_sample_size: 'The baseline sample',
  pacing_min_seconds: 'The shortest delay',
  pacing_max_seconds: 'The longest delay',
  dedup_window_days: 'The duplicate window',
  idea_expiry_days: 'The expiry window',
};

/** The nearest allowed value. Used when a field is committed, not while typing. */
export function clampToBounds(field: BoundedField, value: number): number {
  const { min, max } = TREND_BOUNDS[field];
  if (!Number.isFinite(value)) return min;
  return Math.min(Math.max(value, min), max);
}

/**
 * Why this blocklist entry cannot be saved, or null.
 *
 * The short-entry rule is the one worth explaining rather than just enforcing:
 * matching is whole-word, but a one-character word still appears in a great
 * many captions, and the result would be a run that rejects everything and
 * attributes it to a blocklist the owner thought was narrow.
 */
export function blocklistEntryError(word: string): string | null {
  const clean = word.trim();
  if (clean.length < BLOCKLIST_MIN_WORD_LENGTH) return 'Too short — use at least two characters.';
  if (clean.length > BLOCKLIST_MAX_WORD_LENGTH) return 'Too long — 60 characters at most.';
  return null;
}

/** Lowercased and trimmed, which is how the scout matches and how it is stored. */
export function normaliseBlockedWord(word: string): string {
  return word.trim().toLowerCase();
}

// ---------------------------------------------------------------------------
// The schedule
// ---------------------------------------------------------------------------

type ScheduleFields = Pick<
  TrendSettingsRow,
  'schedule_enabled' | 'schedule_hour_utc' | 'schedule_minute_utc' | 'schedule_days'
>;

export function formatUtcTime(hour: number, minute: number): string {
  return `${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}`;
}

/**
 * When the next scheduled run will start.
 *
 * Mirrors `schedule.next_slot` in the pipeline, in UTC, and is the only way to
 * confirm a schedule change without waiting for it. Returns null while paused.
 *
 * Built from UTC components rather than by mutating a local Date, because the
 * browser is in whatever timezone it is in and 06:00 UTC has to stay 06:00 UTC
 * regardless.
 */
export function nextRunAt(settings: ScheduleFields, now: Date = new Date()): Date | null {
  if (!settings.schedule_enabled) return null;
  if (settings.schedule_days.length === 0) return null;

  const slot = new Date(
    Date.UTC(
      now.getUTCFullYear(),
      now.getUTCMonth(),
      now.getUTCDate(),
      settings.schedule_hour_utc,
      settings.schedule_minute_utc,
      0,
      0,
    ),
  );
  if (slot.getTime() <= now.getTime()) slot.setUTCDate(slot.getUTCDate() + 1);

  // At most a week: every reachable weekday is inside one, and the guard above
  // means there is always at least one enabled day to find.
  for (let i = 0; i < 7; i += 1) {
    if (settings.schedule_days.includes(slot.getUTCDay())) return slot;
    slot.setUTCDate(slot.getUTCDate() + 1);
  }
  return null;
}

/** "Every day at 06:00 UTC", or "Mon, Wed and Fri at 07:30 UTC". */
export function describeSchedule(settings: ScheduleFields): string {
  if (!settings.schedule_enabled) return 'Paused — the daily run is off.';

  const at = `at ${formatUtcTime(settings.schedule_hour_utc, settings.schedule_minute_utc)} UTC`;
  const days = [...settings.schedule_days].sort((a, b) => a - b);
  if (days.length === 0) return `No days selected, so nothing will run ${at}.`;
  if (days.length === 7) return `Every day ${at}.`;

  const names = days.map((d) => DAY_LABELS[d]);
  const list = names.length === 1 ? names[0] : `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`;
  return `${list} ${at}.`;
}

// ---------------------------------------------------------------------------
// What a run will cost
// ---------------------------------------------------------------------------

type CostFields = Pick<
  TrendSettingsRow,
  | 'hashtags'
  | 'hashtags_per_run'
  | 'videos_per_hashtag'
  | 'pacing_min_seconds'
  | 'pacing_max_seconds'
  | 'run_budget_minutes'
>;

/** How many tags a single run will actually scout, with rotation applied. */
export function tagsPerRun(settings: Pick<CostFields, 'hashtags' | 'hashtags_per_run'>): number {
  const total = settings.hashtags.length;
  if (settings.hashtags_per_run === null) return total;
  return Math.min(total, settings.hashtags_per_run);
}

/** How many runs it takes for the rotation to cover every tag once. */
export function runsToCoverEveryTag(settings: Pick<CostFields, 'hashtags' | 'hashtags_per_run'>): number {
  const per = tagsPerRun(settings);
  if (per === 0) return 0;
  return Math.ceil(settings.hashtags.length / per);
}

/**
 * Roughly how long the next run will take, in minutes.
 *
 * Not a guess at network time — almost all of a run is deliberate waiting. The
 * scout paces itself because TikTok-Api does none of its own, and it sleeps
 * around two things: fetching an author's baseline, which happens once per
 * author it has not seen, and each video that survives the free filters.
 *
 * So this is an upper bound, and is presented as one. It assumes every video
 * survives, which is what happens with the filters at their defaults; a
 * tightened age limit or view floor makes a real run shorter than this, and
 * that asymmetry is the right way round for a number the owner is using to
 * decide whether a session is too long.
 */
export function estimateRunMinutes(settings: CostFields): number {
  const averagePace = (settings.pacing_min_seconds + settings.pacing_max_seconds) / 2;
  // Two delays per video in the worst case: one for its author's baseline, one
  // for the video itself having survived.
  const perHashtagSeconds = settings.videos_per_hashtag * 2 * averagePace;
  const estimate = Math.round((tagsPerRun(settings) * perHashtagSeconds) / 60);

  // A budget is a ceiling on exactly this number, so quoting anything above it
  // would be quoting a run that cannot happen.
  if (settings.run_budget_minutes !== null) return Math.min(estimate, settings.run_budget_minutes);
  return estimate;
}

/** Past this, a session is long enough against a hostile platform to be worth a word. */
export const LONG_RUN_MINUTES = 90;

/** Engagement is stored as a fraction and shown as a percentage. */
export function toPercent(fraction: number): number {
  // Two decimal places: the stored column is numeric(5,4), so 0.0425 -> 4.25%
  // round-trips exactly and anything finer would not survive a save.
  return Math.round(fraction * 10000) / 100;
}

export function fromPercent(percent: number): number {
  return Math.round(percent * 100) / 10000;
}

// ---------------------------------------------------------------------------
// Reading a run's breakdown
// ---------------------------------------------------------------------------

export type RunDiagnosisKind = 'scraper' | 'filters' | 'duplicates' | 'quiet' | 'ok' | 'unknown';

export interface RunDiagnosis {
  kind: RunDiagnosisKind;
  headline: string;
  detail: string;
  /** The stage that rejected the most, when a filter is responsible. */
  culprit: TrendRejectionStage | null;
}

/**
 * Why this run produced what it produced.
 *
 * The whole reason the breakdown exists. Before it, `signals = 0` was the only
 * thing a run could say about an untouched queue, and it covered three
 * unrelated situations: a quiet week, filters set too tight, and a scraper
 * being served captchas. An owner cannot act on that, and the reasonable
 * conclusion — that the thing is broken — is wrong most of the time.
 *
 * `seen` is what separates them, and it can do so because of where it is
 * counted: before any filter. No setting on the page can reduce it, so
 * `seen === 0` is always the scrape and never the filters.
 */
export function diagnoseRun(
  run: Pick<TrendRunRow, 'rejections' | 'signals' | 'inserted' | 'suppressed'>,
): RunDiagnosis {
  const report = run.rejections;
  if (!report) {
    // A run from before this existed, or one that failed before scouting. The
    // honest answer is that we cannot tell, rather than a guess dressed up as
    // a diagnosis.
    return {
      kind: 'unknown',
      headline: 'No breakdown was recorded for this run',
      detail:
        'It ran before the per-filter counts existed, or it stopped before scouting began. The next run will record one.',
      culprit: null,
    };
  }

  if (report.seen === 0) {
    const refused = report.failed_hashtags.length;
    return {
      kind: 'scraper',
      headline: refused ? 'The scrape was refused' : 'The feeds returned nothing',
      detail: refused
        ? `${refused} of ${report.hashtags_scouted.length} hashtag feeds raised an error and none returned a video. This is not your filters — no setting can reduce the count below what the feeds hand over. It usually means the session was blocked.`
        : 'No videos came back from any feed, before any filter ran. No setting here can cause that, so this is the scrape itself rather than the bar being too high.',
      culprit: null,
    };
  }

  if ((run.inserted ?? 0) > 0) {
    return {
      kind: 'ok',
      headline: 'Ideas were added',
      detail: `${report.seen} videos looked at, ${report.surfaced} worth surfacing, ${run.inserted} added to the queue.`,
      culprit: null,
    };
  }

  const videoStages = report.stages.filter((s) => s.level === 'video');
  const worst = [...videoStages].sort((a, b) => b.dropped - a.dropped)[0];

  if (worst && worst.dropped > 0 && report.surfaced === 0) {
    return {
      kind: 'filters',
      headline: `Everything was rejected — mostly by “${worst.label.toLowerCase()}”`,
      detail: `${worst.dropped} of ${report.seen} videos were dropped there${
        worst.setting ? `, by ${settingLabel(worst.setting)}` : ''
      }. Nothing reached your queue.`,
      culprit: worst,
    };
  }

  const duplicates = report.stages.find((s) => s.key === 'duplicate')?.dropped ?? 0;
  if (duplicates > 0 && (run.inserted ?? 0) === 0) {
    return {
      kind: 'duplicates',
      headline: 'Everything drafted was a repeat',
      detail: `${duplicates} idea${duplicates === 1 ? ' was' : 's were'} too close to something already in the queue. Nothing here was rejected for being weak — it had all been seen recently.`,
      culprit: report.stages.find((s) => s.key === 'duplicate') ?? null,
    };
  }

  return {
    kind: 'quiet',
    headline: 'Nothing cleared the bar',
    detail: `${report.seen} videos were looked at and ${report.surfaced} were worth surfacing. A quiet week looks like this.`,
    culprit: null,
  };
}

/** The setting a stage names, in the words the settings page uses for it. */
export function settingLabel(setting: string): string {
  return SETTING_LABELS[setting] ?? setting;
}

const SETTING_LABELS: Record<string, string> = {
  max_video_age_days: 'your recency limit',
  min_plays: 'your minimum view count',
  min_interest: 'your minimum search interest',
  caption_blocklist: 'your blocked caption words',
  min_outlier_ratio: 'your minimum outlier ratio',
  min_engagement_rate: 'your minimum engagement rate',
  dedup_window_days: 'your duplicate window',
};

/** How a stage's recorded setting value should read on the breakdown. */
export function formatStageValue(stage: TrendRejectionStage): string | null {
  if (stage.value === null) return null;
  if (Array.isArray(stage.value)) {
    return stage.value.length === 0 ? 'none set' : `${stage.value.length} word${stage.value.length === 1 ? '' : 's'}`;
  }
  switch (stage.setting) {
    case 'min_engagement_rate':
      return `${toPercent(stage.value)}%`;
    case 'min_outlier_ratio':
      return `${stage.value}×`;
    case 'max_video_age_days':
    case 'dedup_window_days':
      return `${stage.value} days`;
    case 'min_plays':
      return `${stage.value.toLocaleString()} views`;
    case 'min_interest':
      return `${stage.value} / 100`;
    default:
      return String(stage.value);
  }
}

// ---------------------------------------------------------------------------
// Choosing how long one run should take
// ---------------------------------------------------------------------------

/**
 * A named search length, offered above the button.
 *
 * Presets rather than raw numbers because run length is the product of two
 * settings, and asking someone to reason about their interaction while they
 * are trying to press a button is the wrong moment for it. `hashtags` is the
 * lever that actually decides the cost; the budget is a ceiling so a preset
 * cannot overrun what it promised.
 *
 * `hashtags: null` means the whole list -- the Deep preset scouts everything,
 * which is what a run did before any of this existed.
 */
export interface SearchLengthPreset {
  key: 'quick' | 'standard' | 'deep';
  label: string;
  /** Tags to scout, or null for all of them. */
  hashtags: number | null;
  description: string;
}

export const SEARCH_LENGTH_PRESETS: readonly SearchLengthPreset[] = [
  {
    key: 'quick',
    label: 'Quick',
    hashtags: 4,
    description: 'A narrow look at a few rooms. Good when you want something to review now.',
  },
  {
    key: 'standard',
    label: 'Standard',
    hashtags: 8,
    description: 'Half the list, rotating, so consecutive runs still cover everything.',
  },
  {
    key: 'deep',
    label: 'Deep',
    hashtags: null,
    description: 'Every hashtag you have configured. The longest session, and the widest spread.',
  },
];

/** What a run of this shape will actually do. Everything here is derivable. */
export interface SearchLengthEstimate {
  /** Hashtags this run will scout. Exact. */
  hashtags: number;
  /** Hashtags configured in total, for saying "4 of 16". */
  configured: number;
  /** Videos the feeds will be asked for. Exact. */
  videos: number;
  /** Upper bound on run length, in minutes. */
  minutes: number;
  /** The ideas-per-run cap. A ceiling, never a forecast. */
  ideaCap: number;
  /** Runs needed for the rotation to cover every hashtag once. */
  runsToCoverList: number;
}

type EstimateSettings = CostFields & Pick<TrendSettingsRow, 'videos_per_hashtag' | 'ideas_per_run'>;

/**
 * What the chosen length means, in numbers that cannot be wrong.
 *
 * Hashtags and videos are arithmetic on the settings, so they are exact.
 * Minutes is an upper bound -- see `estimateRunMinutes`, which assumes every
 * video survives the filters. `ideaCap` is deliberately not a forecast: how
 * many ideas a run yields depends on what clears the filters and on how much
 * of it duplicates the queue, neither of which is knowable in advance. It is
 * the ceiling, and the UI says so in those words.
 */
export function estimateSearchLength(
  settings: EstimateSettings,
  override: { hashtagsPerRun?: number | null; budgetMinutes?: number | null } = {},
): SearchLengthEstimate {
  const effective: EstimateSettings = {
    ...settings,
    hashtags_per_run: override.hashtagsPerRun === undefined ? settings.hashtags_per_run : override.hashtagsPerRun,
    run_budget_minutes: override.budgetMinutes === undefined ? settings.run_budget_minutes : override.budgetMinutes,
  };
  const hashtags = tagsPerRun(effective);
  return {
    hashtags,
    configured: settings.hashtags.length,
    videos: hashtags * settings.videos_per_hashtag,
    minutes: estimateRunMinutes(effective),
    ideaCap: settings.ideas_per_run,
    runsToCoverList: runsToCoverEveryTag(effective),
  };
}

/**
 * The time ceiling a preset enforces: the estimate itself.
 *
 * No headroom, and that is the point. An earlier version added 50% so a run
 * could not be cut short -- but the estimate is already an upper bound, and
 * inflating it meant the control displayed one number and enforced another.
 * Choosing a "~14 min" search and finding a 21-minute cap on the row is the
 * kind of small dishonesty that makes every other number here suspect.
 *
 * The estimate can be spent without truncation because of what it assumes: a
 * paced delay for every video AND a fresh author baseline for every video. A
 * real run meets repeat authors, whose baseline is already known, and rejects
 * videos on the free filters before paying for either. Both make it shorter
 * than this, so the ceiling is reached only by a run that was going wrong.
 */
export function budgetForEstimate(minutes: number): number {
  return clampToBounds('run_budget_minutes', Math.ceil(minutes));
}

// ---------------------------------------------------------------------------
// Google Trends
// ---------------------------------------------------------------------------

/** Mirrors the `trend_settings_keywords` constraint. */
export const MAX_TREND_KEYWORDS = 50;

/**
 * Clean a search term for storage.
 *
 * Lowercased and space-collapsed, and that is all. Unlike a hashtag, a search
 * term is a phrase — stripping punctuation or joining words would change what
 * is being measured, and "excel alternative" is two words on purpose.
 */
export function normaliseKeyword(raw: string): string {
  return raw.trim().replace(/\s+/g, ' ').toLowerCase();
}

/**
 * Regions offered for Google Trends.
 *
 * A short list rather than every ISO code: interest is normalised within a
 * region, so the useful choice is the market you actually sell to, and a
 * two-hundred-entry dropdown makes that choice harder rather than easier.
 * Worldwide is first because it is the honest default — nothing in the
 * pipeline knows where this business sells.
 */
export const GEO_OPTIONS: ReadonlyArray<{ code: string; label: string }> = [
  { code: '', label: 'Worldwide' },
  { code: 'GB', label: 'United Kingdom' },
  { code: 'US', label: 'United States' },
  { code: 'IE', label: 'Ireland' },
  { code: 'CA', label: 'Canada' },
  { code: 'AU', label: 'Australia' },
  { code: 'DE', label: 'Germany' },
  { code: 'FR', label: 'France' },
  { code: 'NL', label: 'Netherlands' },
  { code: 'AE', label: 'United Arab Emirates' },
];

// ---------------------------------------------------------------------------
// Sources
// ---------------------------------------------------------------------------

/**
 * What each source is offered as, in the order the dropdown shows them.
 *
 * The descriptions are the point rather than decoration: the three sources
 * measure genuinely different things, and picking the wrong one produces a run
 * that completes, reports success and finds nothing useful. Google Trends
 * cannot tell you a hook; the other two cannot tell you search demand.
 *
 * Mirrors `pipeline.trends.controls.SOURCES` and the `trend_settings_source`
 * constraint. A source in one and not the others is either a dropdown entry
 * that cannot run or a scout nobody can select.
 */
export const SOURCE_OPTIONS: ReadonlyArray<{
  value: TrendSource;
  label: string;
  help: string;
  /**
   * Whether choosing this source requires a credential the pipeline holds.
   *
   * This app cannot check — it is a static bundle talking to Postgres, with no
   * server and no sight of the pipeline's environment. So the flag drives an
   * unconditional warning rather than a live status, which is the honest way
   * round: telling someone a key is needed when it is already set costs a
   * sentence, while staying quiet costs a run an hour until somebody notices.
   */
  needsCredential?: boolean;
  credentialHint?: string;
}> = [
  {
    value: 'google_trends',
    label: 'Google Trends',
    help: 'Measures what people are searching for, against each term’s own recent history. It says a subject is live; it cannot tell you which hook opens the video. Free, needs no key, and the default for both of those reasons.',
  },
  {
    value: 'apify',
    label: 'Apify (TikTok and Instagram)',
    help: 'Hosted scrapers for short-form video, scored against each author’s own median — the better signal for a hook. Billed per result, so run length is also run cost.',
    needsCredential: true,
    credentialHint:
      'APIFY_TOKEN must be in the pipeline secrets, from the Apify console under Settings → Integrations.',
  },
  {
    value: 'youtube',
    label: 'YouTube',
    help: 'The official YouTube API, scored against each channel’s own median. Documented and stable, but capped by a daily search quota — roughly a hundred terms a day across every run.',
    needsCredential: true,
    credentialHint:
      'YOUTUBE_API_KEY must be in the pipeline secrets, from a Google Cloud project with “YouTube Data API v3” enabled.',
  },
];

/** The platforms Apify can be pointed at. Mirrors `trend_settings_apify_platforms`. */
export const APIFY_PLATFORM_OPTIONS: ReadonlyArray<{ value: ApifyPlatform; label: string }> = [
  { value: 'tiktok', label: 'TikTok' },
  { value: 'instagram', label: 'Instagram' },
];

/**
 * Whether a source measures videos rather than search demand.
 *
 * A named helper rather than `=== 'google_trends'` at each site, because the
 * question is asked in several places and the negation stops being true the
 * moment a second search-demand source arrives. Everything that separates a
 * view count from an interest score keys off this: which filters apply, which
 * vocabulary is edited, and which half of the source card is shown.
 */
export function isVideoSource(source: TrendSource): boolean {
  return source === 'apify' || source === 'youtube';
}

/**
 * Whether a source takes hashtags rather than search terms.
 *
 * Not the same question as `isVideoSource`, which is why it is its own
 * function: YouTube measures videos but is searched with the words a buyer
 * would type, so it reads the keyword list alongside Google Trends. Only the
 * scrapers take hashtags.
 *
 * Accepts a loose string because it is also asked of a stored run's recorded
 * source, which can be absent on an old row or name a source since retired.
 * Either way the answer is "not hashtags", which is the safer way to be wrong:
 * a missing `#` reads as a term, a spurious one reads as a hashtag nobody set.
 */
export function usesHashtags(source: TrendSource | string | null | undefined): boolean {
  return source === 'apify';
}
