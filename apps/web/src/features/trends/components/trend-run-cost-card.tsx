import { useQuery } from '@tanstack/react-query';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Label } from '@/components/ui/label';
import { NativeSelect, NativeSelectOption } from '@/components/ui/native-select';
import { useOwner } from '@/features/auth/use-owner';
import { trendSettingsQueryOptions } from '@/features/trends/api';
import { NumberSetting, OptionalNumberSetting, SaveRow } from '@/features/trends/components/setting-field';
import { estimateRunMinutes, LONG_RUN_MINUTES, runsToCoverEveryTag } from '@/features/trends/controls';
import { useTrendDraft } from '@/features/trends/use-trend-draft';
import type { IdeaProvider } from '@/lib/database.types';
import { formatMinutes } from '@/lib/format';

const FIELDS = [
  'hashtags_per_run',
  'run_budget_minutes',
  'baseline_sample_size',
  'pacing_min_seconds',
  'pacing_max_seconds',
  'dedup_window_days',
  'idea_expiry_days',
  'idea_provider',
] as const;

/**
 * How long a run takes, how hard it leans on the platform, and what happens to
 * what it produces.
 *
 * Separate from the filters card because these do not change what qualifies as
 * a signal -- they change what a run costs. The two get confused otherwise:
 * "fewer hashtags per run" and "a higher outlier ratio" both shorten a run,
 * but only one of them changes what ends up in the queue.
 */
export function TrendRunCostCard() {
  const { isOwner } = useOwner();
  const { draft, set, dirty, isPending, error, isSaving, blockedReason, onSave } = useTrendDraft(FIELDS);
  // The estimate needs the hashtag list and per-hashtag depth, which are edited
  // on other cards. Read from the stored row, not from a draft, because those
  // are the values the next run will actually use.
  const { data: stored } = useQuery(trendSettingsQueryOptions());

  const pacingInverted = draft ? draft.pacing_max_seconds < draft.pacing_min_seconds : false;
  const estimate = stored && draft ? estimateRunMinutes({ ...stored, ...draft }) : null;
  const rounds = stored && draft ? runsToCoverEveryTag({ ...stored, ...draft }) : 0;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Run length and pacing</CardTitle>
        <CardDescription>
          What a run costs, rather than what it accepts. Almost all of a run is deliberate waiting — the scout paces
          itself because the library it uses does not.
        </CardDescription>
      </CardHeader>

      <CardContent className="space-y-6">
        {error && <p className="text-destructive text-sm">Could not load settings: {error.message}</p>}
        {isPending && <p className="text-muted-foreground text-sm">Loading…</p>}

        {draft && (
          <>
            <OptionalNumberSetting
              field="hashtags_per_run"
              label="Rotate hashtags"
              offLabel="Off — every run scouts the whole list."
              value={draft.hashtags_per_run}
              fallback={6}
              disabled={!isOwner || isSaving}
              onChange={(v) => set({ hashtags_per_run: v })}
              help={
                <>
                  Scout this many tags per run and pick up where it left off next time. Run length is roughly linear in
                  the number of tags, and you review one batch a day either way.
                  {draft.hashtags_per_run !== null && rounds > 1 && (
                    <> The whole list still gets covered every {rounds} runs.</>
                  )}
                </>
              }
            />

            <OptionalNumberSetting
              field="run_budget_minutes"
              label="Stop scouting after"
              offLabel="Off — a run scouts everything it was given, however long that takes."
              value={draft.run_budget_minutes}
              fallback={45}
              disabled={!isOwner || isSaving}
              onChange={(v) => set({ run_budget_minutes: v })}
              help="A run that hits this stops scouting and drafts ideas from what it has already found. That is a much better ending than the three-hour write-off, which produces nothing at all."
            />

            <NumberSetting
              field="baseline_sample_size"
              label="Videos sampled per author"
              value={draft.baseline_sample_size}
              disabled={!isOwner || isSaving}
              onChange={(v) => set({ baseline_sample_size: v })}
              help="How many recent videos establish an author's median, which every outlier ratio is measured against. Lowering it makes runs quicker and the median noisier."
            />

            <div className="space-y-2">
              <p className="text-sm font-medium">Delay between requests</p>
              <div className="grid gap-4 sm:grid-cols-2">
                <NumberSetting
                  field="pacing_min_seconds"
                  label="At least"
                  value={draft.pacing_min_seconds}
                  disabled={!isOwner || isSaving}
                  onChange={(v) => set({ pacing_min_seconds: v })}
                />
                <NumberSetting
                  field="pacing_max_seconds"
                  label="At most"
                  value={draft.pacing_max_seconds}
                  disabled={!isOwner || isSaving}
                  onChange={(v) => set({ pacing_max_seconds: v })}
                />
              </div>
              <p className="text-muted-foreground text-xs">
                A random wait in this range after each request, because a request every N seconds exactly is itself a
                signal that nobody is holding the phone. This is the setting that keeps the account in good standing:
                below a second is not a faster scrape, it is a blocked one, which is why the floor is where it is.
              </p>
              {pacingInverted && <p className="text-destructive text-xs">“At most” has to be at least “at least”.</p>}
            </div>

            <NumberSetting
              field="dedup_window_days"
              label="Suppress ideas repeated within"
              value={draft.dedup_window_days}
              disabled={!isOwner || isSaving}
              onChange={(v) => set({ dedup_window_days: v })}
              help="A new idea whose title is near-identical to one from this window is dropped rather than queued twice. Widen it if the same idea keeps coming back; narrow it if a format worth reusing is being suppressed."
            />

            <NumberSetting
              field="idea_expiry_days"
              label="Expire unreviewed ideas after"
              value={draft.idea_expiry_days}
              disabled={!isOwner || isSaving}
              onChange={(v) => set({ idea_expiry_days: v })}
              help="An idea nobody approved by then is marked expired, so it cannot later be produced against a trend that has passed."
            />

            <div className="space-y-2">
              <Label htmlFor="idea-provider">Model that drafts the ideas</Label>
              <NativeSelect
                id="idea-provider"
                className="w-40"
                value={draft.idea_provider}
                disabled={!isOwner || isSaving}
                onChange={(e) => set({ idea_provider: e.target.value as IdeaProvider })}
              >
                <NativeSelectOption value="claude">Claude</NativeSelectOption>
                <NativeSelectOption value="gemini">Gemini</NativeSelectOption>
              </NativeSelect>
              <p className="text-muted-foreground text-xs">
                The prompt, the output schema and the row mapping are shared between them, so this changes which model
                answers and nothing else — which is the only reason ideas from the two are comparable. Whichever you
                pick, its API key has to be in the deployed secret bundle; a run refuses at the start rather than after
                an hour of scouting if it is not.
              </p>
            </div>

            {estimate !== null && (
              <p className="text-muted-foreground text-xs">
                With these settings, the next run is at most {formatMinutes(estimate)}.
                {estimate > LONG_RUN_MINUTES && ' That is a long session against a platform that fights scrapers.'}
              </p>
            )}

            <SaveRow
              isOwner={isOwner}
              dirty={dirty}
              isSaving={isSaving}
              blocked={pacingInverted ? 'The longest delay has to be at least the shortest.' : blockedReason}
              onSave={onSave}
            />
          </>
        )}
      </CardContent>
    </Card>
  );
}
