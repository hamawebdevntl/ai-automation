import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Label } from '@/components/ui/label';
import { NativeSelect, NativeSelectOption } from '@/components/ui/native-select';
import { Switch } from '@/components/ui/switch';
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group';
import { useOwner } from '@/features/auth/use-owner';
import { NumberSetting, SaveRow } from '@/features/trends/components/setting-field';
import {
  DAY_LABELS,
  describeSchedule,
  formatUtcTime,
  nextRunAt,
  SCHEDULE_CATCHUP_MINUTES,
} from '@/features/trends/controls';
import { useTrendDraft } from '@/features/trends/use-trend-draft';
import { formatMinutes } from '@/lib/format';

const FIELDS = [
  'schedule_enabled',
  'schedule_hour_utc',
  'schedule_minute_utc',
  'schedule_days',
  'max_video_age_days',
] as const;

const HOURS = Array.from({ length: 24 }, (_, i) => i);
// Five-minute granularity. The dispatcher wakes once a minute, so anything
// finer would be a precision the schedule cannot actually keep.
const MINUTES = [0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55];

/**
 * When runs happen, and how recent a video has to be to count.
 *
 * This card is what replaced a `terraform apply`. The daily run was a
 * `cron(0 6 * * ? *)` in Terraform pointed straight at ECS; it is now these
 * columns, read every minute by `dispatch_trend_runs`.
 */
export function TrendScheduleCard() {
  const { isOwner } = useOwner();
  const { draft, set, dirty, isPending, error, isSaving, blockedReason, onSave } = useTrendDraft(FIELDS);

  const noDays = draft?.schedule_days.length === 0;
  const next = draft ? nextRunAt(draft) : null;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">When runs happen</CardTitle>
        <CardDescription>
          The daily trend run, and what still counts as recent. Times are UTC — the same clock the pipeline, the run
          history and the logs use.
        </CardDescription>
      </CardHeader>

      <CardContent className="space-y-6">
        {error && <p className="text-destructive text-sm">Could not load settings: {error.message}</p>}
        {isPending && <p className="text-muted-foreground text-sm">Loading…</p>}

        {draft && (
          <>
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div className="space-y-1">
                <Label htmlFor="schedule-enabled">Run automatically</Label>
                <p className="text-muted-foreground text-xs">
                  Pausing stops the daily run only. You can still start one yourself from the queue, which is how to
                  test a change without waiting for tomorrow.
                </p>
              </div>
              <Switch
                id="schedule-enabled"
                checked={draft.schedule_enabled}
                disabled={!isOwner || isSaving}
                onCheckedChange={(next) => set({ schedule_enabled: next })}
              />
            </div>

            <div className="space-y-2">
              <Label htmlFor="schedule-hour">Time of day</Label>
              <div className="flex items-center gap-2">
                <NativeSelect
                  id="schedule-hour"
                  aria-label="Hour, UTC"
                  className="w-20"
                  value={String(draft.schedule_hour_utc)}
                  disabled={!isOwner || isSaving || !draft.schedule_enabled}
                  onChange={(e) => set({ schedule_hour_utc: Number(e.target.value) })}
                >
                  {HOURS.map((h) => (
                    <NativeSelectOption key={h} value={h}>
                      {String(h).padStart(2, '0')}
                    </NativeSelectOption>
                  ))}
                </NativeSelect>
                <span className="text-muted-foreground">:</span>
                <NativeSelect
                  aria-label="Minute, UTC"
                  className="w-20"
                  value={String(draft.schedule_minute_utc)}
                  disabled={!isOwner || isSaving || !draft.schedule_enabled}
                  onChange={(e) => set({ schedule_minute_utc: Number(e.target.value) })}
                >
                  {MINUTES.map((m) => (
                    <NativeSelectOption key={m} value={m}>
                      {String(m).padStart(2, '0')}
                    </NativeSelectOption>
                  ))}
                </NativeSelect>
                <span className="text-muted-foreground text-sm">UTC</span>
              </div>
              <p className="text-muted-foreground text-xs">
                Your browser is currently {browserOffset()}, so{' '}
                {formatUtcTime(draft.schedule_hour_utc, draft.schedule_minute_utc)} UTC is{' '}
                {inLocalTime(draft.schedule_hour_utc, draft.schedule_minute_utc)} for you today.
              </p>
            </div>

            <div className="space-y-2">
              <Label>Days</Label>
              <ToggleGroup
                type="multiple"
                variant="outline"
                className="flex-wrap"
                value={draft.schedule_days.map(String)}
                disabled={!isOwner || isSaving || !draft.schedule_enabled}
                onValueChange={(days) => set({ schedule_days: days.map(Number).sort((a, b) => a - b) })}
              >
                {DAY_LABELS.map((label, day) => (
                  <ToggleGroupItem key={label} value={String(day)} aria-label={label}>
                    {label}
                  </ToggleGroupItem>
                ))}
              </ToggleGroup>
              {noDays ? (
                <p className="text-destructive text-xs">
                  Pick at least one day. A schedule that is on but can never fire looks exactly like a broken dispatcher
                  — switch “Run automatically” off instead.
                </p>
              ) : (
                <p className="text-muted-foreground text-xs">
                  {describeSchedule(draft)} {next && <>Next run {next.toUTCString().replace('GMT', 'UTC')}.</>}
                </p>
              )}
            </div>

            <NumberSetting
              field="max_video_age_days"
              label="Only count videos posted in the last"
              value={draft.max_video_age_days}
              disabled={!isOwner || isSaving}
              onChange={(days) => set({ max_video_age_days: days })}
              help={
                <>
                  Anything older is rejected before it is scored. There was previously no limit at all, so a
                  two-year-old video with a good ratio counted as a trend. This is also the cheapest filter there is —
                  it runs before the scout looks up an author’s history, so tightening it makes runs shorter.
                </>
              }
            />

            <p className="text-muted-foreground text-xs">
              A run that misses its slot — because one was still going, or the dispatcher was down — starts late if it
              can, up to {formatMinutes(SCHEDULE_CATCHUP_MINUTES)} afterwards. Past that the slot is abandoned rather
              than fired at some unrelated hour.
            </p>

            <SaveRow
              isOwner={isOwner}
              dirty={dirty}
              isSaving={isSaving}
              blocked={noDays ? 'Pick at least one day, or switch the schedule off.' : blockedReason}
              onSave={onSave}
            />
          </>
        )}
      </CardContent>
    </Card>
  );
}

/** "UTC+2", from the browser's own offset. */
function browserOffset(): string {
  const minutes = -new Date().getTimezoneOffset();
  if (minutes === 0) return 'on UTC';
  const sign = minutes > 0 ? '+' : '−';
  const hours = Math.floor(Math.abs(minutes) / 60);
  const rest = Math.abs(minutes) % 60;
  return `UTC${sign}${hours}${rest ? `:${String(rest).padStart(2, '0')}` : ''}`;
}

/**
 * The same instant in the reader's own timezone.
 *
 * The setting is UTC and stays UTC -- one clock for the schedule, the run rows
 * and the logs. This line is the concession that nobody plans their day in it.
 */
function inLocalTime(hour: number, minute: number): string {
  const now = new Date();
  const utc = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate(), hour, minute));
  return utc.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
}
