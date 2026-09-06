import { useQuery } from '@tanstack/react-query';
import { Label } from '@/components/ui/label';
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group';
import { trendSettingsQueryOptions } from '@/features/trends/api';
import { NumberSetting } from '@/features/trends/components/setting-field';
import {
  budgetForEstimate,
  estimateSearchLength,
  LONG_RUN_MINUTES,
  SEARCH_LENGTH_PRESETS,
  type SearchLengthEstimate,
  TREND_BOUNDS,
} from '@/features/trends/controls';
import { formatMinutes } from '@/lib/format';

/**
 * How long the next run should take, chosen at the moment of starting it.
 *
 * This is not the settings page in miniature. The settings decide how the
 * scheduled run behaves, unattended, every day; this decides one run that
 * somebody is about to press a button for, and their reason -- a quick look
 * before a meeting, a deep sweep because the queue is empty -- has nothing to
 * do with tomorrow morning. So the choice rides on the run row and the saved
 * settings are untouched.
 *
 * Only the two costs are offered. Neither changes what qualifies as a signal,
 * which is what keeps a quick run and a deep run comparable: the same bars,
 * fewer rooms.
 */
export type SearchLengthChoice = {
  key: 'quick' | 'standard' | 'deep' | 'custom';
  hashtagsPerRun: number | null;
  budgetMinutes: number | null;
};

export const DEFAULT_SEARCH_LENGTH: SearchLengthChoice = {
  key: 'standard',
  hashtagsPerRun: null,
  budgetMinutes: null,
};

export function SearchLength({
  value,
  onChange,
  disabled,
}: {
  value: SearchLengthChoice;
  onChange: (choice: SearchLengthChoice) => void;
  disabled?: boolean;
}) {
  const { data: settings } = useQuery(trendSettingsQueryOptions());

  // Nothing useful to say without the settings: every number below is derived
  // from the hashtag list, the per-hashtag depth and the pacing.
  if (!settings) return null;

  // Aliased after the guard because `pick` below is a hoisted function
  // declaration, and TypeScript stops trusting a narrowing that a hoisted
  // function could outlive.
  const cfg = settings;

  const estimate = estimateSearchLength(cfg, {
    hashtagsPerRun: value.hashtagsPerRun,
    budgetMinutes: value.budgetMinutes,
  });

  function pick(key: SearchLengthChoice['key']) {
    if (key === 'custom') {
      // Seed the custom fields from whatever is on screen, so switching to it
      // is a refinement rather than a blank slate.
      onChange({
        key: 'custom',
        hashtagsPerRun: estimate.hashtags,
        budgetMinutes: budgetForEstimate(estimate.minutes),
      });
      return;
    }
    const preset = SEARCH_LENGTH_PRESETS.find((p) => p.key === key);
    if (!preset) return;
    const projected = estimateSearchLength(cfg, { hashtagsPerRun: preset.hashtags, budgetMinutes: null });
    onChange({
      key,
      hashtagsPerRun: preset.hashtags,
      // A ceiling, so a preset cannot overrun the length it just promised.
      budgetMinutes: budgetForEstimate(projected.minutes),
    });
  }

  const chosen = SEARCH_LENGTH_PRESETS.find((p) => p.key === value.key);

  return (
    <div className="w-full space-y-2 sm:w-auto">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <Label htmlFor="search-length" className="text-xs font-normal">
          Search length
        </Label>
        <ToggleGroup
          id="search-length"
          // Named here rather than relying on the label's `htmlFor`: a label
          // does not name a role="group" for assistive tech, and this is the
          // only thing distinguishing the control from the toggles inside it.
          aria-label="Search length"
          type="single"
          size="sm"
          variant="outline"
          value={value.key}
          disabled={disabled}
          // A toggle group returns '' when the active item is clicked again.
          // Re-selecting the same length must not clear the choice.
          onValueChange={(next) => next && pick(next as SearchLengthChoice['key'])}
        >
          {SEARCH_LENGTH_PRESETS.map((preset) => (
            <ToggleGroupItem key={preset.key} value={preset.key} aria-label={preset.label}>
              {preset.label}
            </ToggleGroupItem>
          ))}
          <ToggleGroupItem value="custom" aria-label="Custom">
            Custom
          </ToggleGroupItem>
        </ToggleGroup>
      </div>

      {value.key === 'custom' && (
        <div className="grid gap-3 rounded-md border p-3 sm:grid-cols-2">
          <NumberSetting
            field="hashtags_per_run"
            label="Hashtags"
            value={value.hashtagsPerRun ?? cfg.hashtags.length}
            disabled={disabled}
            onChange={(v) => onChange({ ...value, key: 'custom', hashtagsPerRun: v })}
          />
          <NumberSetting
            field="run_budget_minutes"
            label="Stop after"
            value={value.budgetMinutes ?? budgetForEstimate(estimate.minutes)}
            disabled={disabled}
            onChange={(v) => onChange({ ...value, key: 'custom', budgetMinutes: v })}
          />
        </div>
      )}

      <Estimate estimate={estimate} hint={chosen?.description} />
    </div>
  );
}

/**
 * What that length actually buys.
 *
 * Hashtags and videos are arithmetic on the settings, so they are exact.
 * Minutes is an upper bound. The idea figure is deliberately "up to": how many
 * a run yields depends on what clears the filters and how much duplicates the
 * queue, and neither is knowable before the run. Printing a confident number
 * there would be inventing one.
 */
function Estimate({ estimate, hint }: { estimate: SearchLengthEstimate; hint?: string }) {
  const parts = [
    // The wording is the promise: this is the enforced ceiling, not a guess
    // the run may quietly exceed. The scout is stopped on this deadline even
    // if the session it is talking to has stopped responding.
    `stops by ${formatMinutes(estimate.minutes)}`,
    estimate.hashtags === estimate.configured
      ? `all ${estimate.configured} hashtags`
      : `${estimate.hashtags} of ${estimate.configured} hashtags`,
    `~${estimate.videos.toLocaleString()} videos`,
  ];

  return (
    <div className="space-y-0.5 text-xs sm:text-right">
      <p className="text-muted-foreground">
        {parts.join(' · ')} · <span className="text-foreground">up to {estimate.ideaCap} ideas</span>
      </p>
      <p className="text-muted-foreground">
        {estimate.hashtags < estimate.configured && estimate.runsToCoverList > 1 && (
          <>Covers the whole list every {estimate.runsToCoverList} runs. </>
        )}
        {estimate.minutes > LONG_RUN_MINUTES && 'That is a long session against a platform that fights scrapers. '}
        {hint}
      </p>
      <p className="text-muted-foreground">
        “Up to” is your ideas-per-run cap, not a forecast — how many arrive depends on what clears your filters.
      </p>
    </div>
  );
}

/** The bounds the custom fields are held to, re-exported for the tests. */
export const SEARCH_LENGTH_BOUNDS = {
  hashtags: TREND_BOUNDS.hashtags_per_run,
  minutes: TREND_BOUNDS.run_budget_minutes,
};
